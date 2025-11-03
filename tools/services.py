import glob
from collections import defaultdict
import functools
import decimal
from typing import List

import pyzipper
from core import PATIENT_CATEGORY_MASK_MALE, PATIENT_CATEGORY_MASK_FEMALE, PATIENT_CATEGORY_MASK_ADULT, \
    PATIENT_CATEGORY_MASK_MINOR
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection
from itertools import chain

from contribution.models import Premium
from core.utils import filter_validity
from django.db.models import Manager, Prefetch
from django.db.models.query_utils import Q
from django.http import JsonResponse
from import_export.results import Result

from tools.constants import (
    STRATEGY_INSERT,
    STRATEGY_INSERT_UPDATE_DELETE,
    STRATEGY_UPDATE,
)
from tools.apps import ToolsConfig
import datetime

from insuree.models import Family, Insuree, InsureePolicy
from medical.models import Diagnosis, Item, Service, ItemOrService
from location.models import Location, HealthFacility, LocationManager, UserDistrict
from medical_pricelist.models import ServicesPricelist, ItemsPricelist
from core.models.user import ClaimAdmin, InteractiveUser
from claim.models import Claim, Feedback, FeedbackPrompt
from policy.models import Policy
from policy.services import update_insuree_policies
from .utils import dictfetchall, sanitize_xml, dmy_format_sql
from .models import Extract
import logging
from dataclasses import dataclass
import simplejson as json
import tempfile
import pyminizip
import zipfile
import sqlite3
import os
from xml.etree import ElementTree

import csv
import io
from django.db import transaction
from django.contrib.auth import get_user_model
from core.models import UserRole, Role
from location.models import Location, UserDistrict

User = get_user_model()


logger = logging.getLogger(__name__)

# It's not great to convert decimals to float but keeping it in string would
# mean updating the mobile app.
sqlite3.register_adapter(decimal.Decimal, lambda d: float(d))


class InvalidXMLError(ValueError):
    pass


@dataclass
class UploadResult:
    errors: List
    sent: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0


@dataclass
class UploadSimpleDataContext:
    """Represents the required information for uploading data from an XML file.

    Attributes
    ----------
    parsed_entries : List[dict]
        List of entries parsed from the XML file.
    parsing_errors : List[str]
        List of errors that happened during the entries parsing.
    object_manager : Manager
        Model Manager class of the object type that is going to be uploaded.
    log_string_sg : str
        Representation of the object type that is going to be uploaded, singular form.
    log_string_pl : str
        Representation of the object type that is going to be uploaded, plural form.
    strategy : str
        The requested strategy for the data upload.
    dry_run : bool
        Determines whether this is a dry run (test run) or not.
    """
    parsed_entries: List
    parsing_errors: List
    object_manager: Manager
    log_string_sg: str
    log_string_pl: str
    strategy: str = STRATEGY_INSERT
    dry_run: bool = False


def load_diagnoses_xml(xml):
    result = []
    errors = []
    root = xml.getroot()

    for elm in root.findall("Diagnosis"):
        try:
            code = get_xml_element(elm, "DiagnosisCode")
            name = get_xml_element(elm, "DiagnosisName")
        except:
            errors.append("Diagnosis has no code or no name")
            continue

        if any([res["code"].lower() == code.lower() for res in result]):
            errors.append(f"'{code}' is already present in the list")
        elif len(code) > 6:
            errors.append(f"Code cannot be longer than 6 characters: '{code}'")
        elif len(name) > 255:
            errors.append(f"Name cannot be longer than 255 characters: '{name}'")
        else:
            result.append(dict(code=code, name=name))

    return result, errors


# added string literals for CSVs (again, CSVs must have a special way to be handled...)
VALID_PATIENT_CATEGORY_INPUTS = [0, 1, "0", "1"]


def parse_xml_items(xml):
    """Parses medical.Items in an XML file.

    This function parses all the mandatory fields of a medical.Item
    and calls `parse_optional_item_fields` for parsing the optional fields.

    This function does not create medical.Items, but dictionaries with all fields and values
    that can be used to create items.

    This function checks that the data matches the various field constraints specified in the medical.Item model.
    If some data do not match these constraints, if a mandatory field is missing or if there is any error
    while the data is being parsed, an error message is added and the currently parsed item is discarded.

    Parameters
    ----------
    xml : xml.etree.ElementTree.ElementTree
        The parsed XML file.

    Returns
    ------
    result : list[dict]
        A list of dictionaries that represent each parsed item.

    errors : list[str]
        The list of errors. An empty list means that there was no error.
    """
    result = []
    errors = []
    root = xml.getroot()

    for elm in root.findall("Item"):
        try:
            # Item mandatory fields
            code = get_xml_element(elm, "ItemCode")
            name = get_xml_element(elm, "ItemName")
            item_type = get_xml_element(elm, "ItemType").upper()
            price = get_xml_element_float(elm, "ItemPrice")
            care_type = get_xml_element(elm, "ItemCareType").upper()
            # The xxx_cat fields = Item.patient_category
            adult_cat = get_xml_element_int(elm, "ItemAdultCategory")
            minor_cat = get_xml_element_int(elm, "ItemMinorCategory")
            male_cat = get_xml_element_int(elm, "ItemMaleCategory")
            female_cat = get_xml_element_int(elm, "ItemFemaleCategory")

        except InvalidXmlInt as parsing_ex:
            errors.append(f"Item '{code}': patient categories are invalid. Please use '0' for no or '1' for yes")
            continue
        except InvalidXmlFloat as parsing_ex:
            errors.append(f"Item '{code}': price is invalid. Please use '.' "
                          f"as decimal separator, without any currency symbol.")
            continue
        except AttributeError as missing_value_ex:
            errors.append(
                f"Item is missing one of the following fields: code, name, type, price, care type, "
                f"male category, female category, adult category or minor category.")
            continue

        categories = [adult_cat, minor_cat, male_cat, female_cat]
        # No parsing error - now checking the model constraints
        if any([res["code"].lower() == code.lower() for res in result]):
            errors.append(f"Item '{code}': exists multiple times in the list")
        elif len(code) < 1 or len(code) > 6:
            errors.append(f"Item '{code}': code is invalid. Must be between 1 and 6 characters")
        elif len(name) < 1 or len(name) > 100:
            errors.append(f"Item '{code}': name is invalid ('{name}'). Must be between 1 and 100 characters")
        elif item_type not in Item.TYPE_VALUES:
            errors.append(f"Item '{code}': type is invalid ('{item_type}'). "
                          f"Must be one of the following: {Item.TYPE_VALUES}")
        elif care_type not in ItemOrService.CARE_TYPE_VALUES:
            errors.append(f"Item '{code}': care type is invalid ('{care_type}'). "
                          f"Must be one of the following: {ItemOrService.CARE_TYPE_VALUES}")
        elif any([cat not in VALID_PATIENT_CATEGORY_INPUTS for cat in categories]):
            errors.append(f"Item '{code}': patient categories are invalid. "
                          f"Must be one of the following: {VALID_PATIENT_CATEGORY_INPUTS}")
        else:
            # No constraint error found
            optional_fields, optional_error = parse_optional_item_fields(elm, code)

            if optional_error:
                errors.append(optional_error)
            else:
                # No error found in the optional fields either, the item can be safely uploaded

                # Using masks to calculate the SmallInteger value that is going to be stored for patient_category
                category = 0
                if male_cat:
                    category = category | PATIENT_CATEGORY_MASK_MALE
                if female_cat:
                    category = category | PATIENT_CATEGORY_MASK_FEMALE
                if adult_cat:
                    category = category | PATIENT_CATEGORY_MASK_ADULT
                if minor_cat:
                    category = category | PATIENT_CATEGORY_MASK_MINOR

                result.append(dict(code=code, name=name, type=item_type, price=price, care_type=care_type,
                                   patient_category=category, **optional_fields))

    return result, errors


marker = object()  # Used to mark missing values in get_xml_element


def get_xml_element(elm, element_name, default=marker):
    """Gets the text of an XML element, stripping it.

    Parameters
    ----------
    elm : xml.etree.ElementTree.Element
        The XML element.
    element_name : str
        The name of the XML element.
    default : object
        The default value to return if the element is missing. If unspecified, the exception will bubble up.

    Returns
    -------
    str
        The text of the XML element.
    """
    element = elm.find(element_name)
    if default == marker:
        return element.text.strip()
    else:
        return element.text.strip() if element is not None and element.text is not None else default


class InvalidXmlInt(ValueError):
    """Exception raised when an XML element is not a valid integer."""
    pass


def get_xml_element_int(elm, element_name, default=marker):
    element_text = get_xml_element(elm, element_name, default)
    try:
        if element_text is None:
            return None
        return int(element_text)
    except ValueError as exc:
        raise InvalidXmlInt(f"Invalid integer value for {element_name}: {element_text}") from exc


class InvalidXmlFloat(ValueError):
    """Exception raised when an XML element is not a valid float."""
    pass


def get_xml_element_float(elm, element_name, default=marker):
    element_text = get_xml_element(elm, element_name, default)
    try:
        if element_text is None:
            return None
        return float(element_text)
    except ValueError as exc:
        raise InvalidXmlFloat(f"Invalid float value for {element_name}: {element_text}") from exc


def parse_optional_item_fields(elm, code):
    """Parses optional medical.Item fields in an Element.

    This function parses all the optional fields of a medical.Item.

    If any parsing error or constraint error is found in the optional fields,
    the returned `error_message` value will not be an empty string. In that case,
    the returned `optional_values` value should be disregarded.

    Parameters
    ----------
    elm : xml.etree.ElementTree.Element
        The Element that contains the optional fields.

    code : str
        The item's code.

    Returns
    ------
    optional_values : list[dict]
        A dictionary that represents the item's optional values. If `error_message` is not
        an empty string, this value should be disregarded.

    error_message : str
        An optional error message. An empty string means that there was no error.
    """
    optional_values = {}
    error_message = ""
    try:
        quantity = get_xml_element_float(elm, "ItemQuantity", None)
        if quantity is not None:
            optional_values["quantity"] = quantity
        frequency = get_xml_element_int(elm, "ItemFrequency", None)
        if frequency is not None:
            optional_values["frequency"] = frequency
        package = get_xml_element(elm, "ItemPackage", None)
        if package is not None:
            if len(package) < 1 or len(package) > 255:
                error_message = f"Item '{code}': package is invalid ('{package}'). " \
                                f"Must be between 1 and 255 characters"
            else:
                optional_values["package"] = package

        return optional_values, error_message

    except InvalidXmlInt as parsing_ex:
        error_message = f"Item '{code}': frequency is invalid. Please enter a non decimal number of days."
    except InvalidXmlFloat as parsing_ex:
        error_message = f"Item '{code}': quantity is invalid. Please use '.' as decimal separator."

    return optional_values, error_message


def parse_xml_services(xml):
    """Parses medical.Services in an XML file.

    This function parses all the mandatory fields of a medical.Service
    and calls `parse_optional_service_fields` for parsing the optional fields.

    This function does not create medical.Services, but dictionaries with all fields and values
    that can be used to create services.

    This function checks that the data matches the various field constraints specified in the medical.Service model.
    If some data do not match these constraints, if a mandatory field is missing or if there is any error
    while the data is being parsed, an error message is added and the currently parsed service is discarded.

    ParametersFeedbackPrompt
    ----------
    xml : xml.etree.ElementTree.ElementTree
        The parsed XML file.

    Returns
    ------
    result : list[dict]
        A list of dictionaries that represent each parsed service.

    errors : list[str]
        The list of errors. An empty list means that there was no error.
    """
    result = []
    errors = []
    root = xml.getroot()

    for elm in root.findall("Service"):
        try:
            # Item mandatory fields
            code = get_xml_element(elm, "ServiceCode")
            name = get_xml_element(elm, "ServiceName")
            service_type = get_xml_element(elm, "ServiceType").upper()
            level = get_xml_element(elm, "ServiceLevel").upper()
            price = get_xml_element_float(elm, "ServicePrice")
            care_type = get_xml_element(elm, "ServiceCareType").upper()
            # The xxx_cat fields = Item.patient_category
            adult_cat = get_xml_element_int(elm, "ServiceAdultCategory")
            minor_cat = get_xml_element_int(elm, "ServiceMinorCategory")
            male_cat = get_xml_element_int(elm, "ServiceMaleCategory")
            female_cat = get_xml_element_int(elm, "ServiceFemaleCategory")

        except InvalidXmlInt as parsing_ex:
            errors.append(f"Service '{code}': patient categories are invalid. Please use '0' for no or '1' for yes")
            continue
        except InvalidXmlFloat as parsing_ex:
            errors.append(f"Service '{code}': price is invalid. Please use '.' "
                          f"as decimal separator, without any currency symbol.")
            continue
        except AttributeError as missing_value_ex:
            errors.append(
                f"Service is missing one of the following fields: code, name, type, level, price, care type, "
                f"male category, female category, adult category or minor category.")
            continue

        categories = [adult_cat, minor_cat, male_cat, female_cat]
        # No parsing error - now checking the model constraints
        if any([res["code"].lower() == code.lower() for res in result]):
            errors.append(f"Service '{code}': exists multiple times in the list")
        elif len(code) < 1 or len(code) > 6:
            errors.append(f"Service '{code}': code is invalid. Must be between 1 and 6 characters")
        elif len(name) < 1 or len(name) > 100:
            errors.append(f"Service '{code}': name is invalid ('{name}'). Must be between 1 and 100 characters")
        elif service_type not in Service.TYPE_VALUES:
            errors.append(f"Service '{code}': type is invalid ('{service_type}'). "
                          f"Must be one of the following: {Service.TYPE_VALUES}")
        elif level not in Service.LEVEL_VALUES:
            errors.append(f"Service '{code}': level is invalid ('{level}'). "
                          f"Must be one of the following: {Service.LEVEL_VALUES}")
        elif care_type not in ItemOrService.CARE_TYPE_VALUES:
            errors.append(f"Service '{code}': care type is invalid ('{care_type}'). "
                          f"Must be one of the following: {ItemOrService.CARE_TYPE_VALUES}")
        elif any([cat not in VALID_PATIENT_CATEGORY_INPUTS for cat in categories]):
            errors.append(f"Service '{code}': patient categories are invalid. "
                          f"Must be one of the following: {VALID_PATIENT_CATEGORY_INPUTS}")
        else:
            # No constraint error found
            optional_fields, optional_error = parse_optional_service_fields(elm, code)

            if optional_error:
                errors.append(optional_error)
            else:
                # No error found in the optional fields either, the service can be safely uploaded

                # Using masks to calculate the SmallInteger value that is going to be stored for patient_category
                category = 0
                if male_cat:
                    category = category | PATIENT_CATEGORY_MASK_MALE
                if female_cat:
                    category = category | PATIENT_CATEGORY_MASK_FEMALE
                if adult_cat:
                    category = category | PATIENT_CATEGORY_MASK_ADULT
                if minor_cat:
                    category = category | PATIENT_CATEGORY_MASK_MINOR

                result.append(dict(code=code, name=name, type=service_type, level=level, price=price,
                                   care_type=care_type, patient_category=category, **optional_fields))

    return result, errors


def parse_optional_service_fields(elm, code):
    """Parses optional medical.Service fields in an Element.

    This function parses all the optional fields of a medical.Service.

    If any parsing error or constraint error is found in the optional fields,
    the returned `error_message` value will not be an empty string. In that case,
    the returned `optional_values` value should be disregarded.

    Parameters
    ----------
    elm : xml.etree.ElementTree.Element
        The Element that contains the optional fields.

    code : str
        The service's code.

    Returns
    ------
    optional_values : list[dict]
        A dictionary that represents the Service's optional values. If `error_message` is not
        an empty string, this value should be disregarded.

    error_message : str
        An optional error message. An empty string means that there was no error.
    """
    optional_values = {}
    error_message = ""
    try:
        frequency = get_xml_element_int(elm, "ServiceFrequency", None)
        if frequency is not None:
            optional_values["frequency"] = frequency

        raw_category = get_xml_element(elm, "ServiceCategory", None)
        if raw_category is not None:
            category = raw_category.upper()
            if category not in Service.CATEGORY_VALUES:
                error_message = f"Service '{code}': category is invalid ('{category}'). " \
                                f"Must be one of the following: {Service.CATEGORY_VALUES}"
            else:
                optional_values["category"] = category

        return optional_values, error_message

    except ValueError as parsing_ex:
        error_message = f"Service '{code}': frequency is invalid. Please enter a non decimal number of days."

        return optional_values, error_message


def upload_diagnoses(user, xml, strategy=STRATEGY_INSERT, dry_run=False):
    raw_diagnoses, errors = load_diagnoses_xml(xml)
    context = UploadSimpleDataContext(
        strategy=strategy,
        dry_run=dry_run,
        parsed_entries=raw_diagnoses,
        parsing_errors=errors,
        object_manager=Diagnosis.objects,
        log_string_sg="Diagnosis",
        log_string_pl="diagnoses",
    )
    return upload_simple_data(user, context)


def upload_items(user, xml, strategy=STRATEGY_INSERT, dry_run=False):
    """Uploads an XML file containing medical.Item entries.

    There are 4 strategies for uploading Items:
        - INSERT: inserts all the entries that do not exist yet
        - UPDATE: updates all the entries that already exist
        - INSERT_UPDATE: inserts all the entries that do not exist yet and updates the others
        - INSERT_UPDATE_DELETE: inserts all the entries that do not exist yet, updates the ones that
        already exist and deletes the remaining ones

    This function can make dry runs to test the XML file upload and check if there are any errors.

    Parameters
    ----------
    user : core.models.User
        The User that requested the data upload.

    xml : xml.etree.ElementTree.ElementTree
        The parsed XML file.

    strategy : str
        The requested strategy for the data upload.

    dry_run : bool
        Determines whether this is a dry run (test run) or not.

    Returns
    ------
    UploadResult
        A structure that represents the upload process result, with the number of
        entries received, the number of created/updated/deleted Items and the list of errors.
    """
    raw_items, errors = parse_xml_items(xml)
    context = UploadSimpleDataContext(
        strategy=strategy,
        dry_run=dry_run,
        parsed_entries=raw_items,
        parsing_errors=errors,
        object_manager=Item.objects,
        log_string_sg="Item",
        log_string_pl="items",
    )
    return upload_simple_data(user, context)


def upload_simple_data(user, context):
    """Uploads various types of data.

    This function can process any type of "simple" data import (medical.Item, medical.Service
    and medical.Diagnosis) that do not require extra processing, such as location.Location.

    This function will create new entries, update existing ones and/or delete existing ones.


    Parameters
    ----------
    user : core.models.User
        The User that requested the data upload.

    context : UploadSimpleDataContext
        The upload context containing all the necessary information about the upload.

    Returns
    ------
    result : UploadResult
        A structure that represents the upload process result, with the number of
        entries received, the number of created/updated/deleted entries and the list of errors.
    """
    logger.info("Uploading %s with strategy=%s & dry_run=%s", context.log_string_pl,
                context.strategy, context.dry_run)

    result = UploadResult(errors=context.parsing_errors)
    ids = []
    # Fetches the valid DB entries that already exist (with the same codes as the ones in the XML file)
    db_entries = {
        x.code: x
        for x in context.object_manager.filter(
            code__in=[x["code"] for x in context.parsed_entries], *filter_validity()
        )
    }

    for entry in context.parsed_entries:
        logger.debug("Processing %s...", entry['code'])
        existing = db_entries.get(entry["code"], None)
        result.sent += 1
        ids.append(entry["code"])

        if existing and context.strategy == STRATEGY_INSERT:
            result.errors.append(f"{context.log_string_sg} '{existing.code}' already exists")
            continue
        elif not existing and context.strategy == STRATEGY_UPDATE:
            result.errors.append(f"{context.log_string_sg} '{entry['code']}' does not exist")
            continue

        if context.strategy == STRATEGY_INSERT:
            if not context.dry_run:
                context.object_manager.create(audit_user_id=user.id_for_audit, **entry)
            result.created += 1

        else:
            if existing:
                if not context.dry_run:
                    existing.save_history()
                    [setattr(existing, key, entry[key]) for key in entry]
                    existing.validity_from = datetime.datetime.now()
                    existing.save()
                result.updated += 1
            else:
                if not context.dry_run:
                    context.object_manager.create(audit_user_id=user.id_for_audit, **entry)
                result.created += 1

    if context.strategy == STRATEGY_INSERT_UPDATE_DELETE:
        # Fetches all the entries whose code is not in the XML file -> the ones that should be deleted
        qs = context.object_manager.filter(~Q(code__in=ids)).filter(validity_to__isnull=True)
        result.deleted = len(qs)
        logger.info("Deleted %s %s", result.deleted, context.log_string_pl)
        if not context.dry_run:
            qs.update(validity_to=datetime.datetime.now(), audit_user_id=user.id_for_audit)

    logger.debug("Finished processing of %s: %s", context.log_string_pl, result)
    return result


def upload_services(user, xml, strategy=STRATEGY_INSERT, dry_run=False):
    """Uploads an XML file containing medical.Service entries.

    There are 4 strategies for uploading Services:
        - INSERT: inserts all the entries that do not exist yet
        - UPDATE: updates all the entries that already exist
        - INSERT_UPDATE: inserts all the entries that do not exist yet and updates the others
        - INSERT_UPDATE_DELETE: inserts all the entries that do not exist yet, updates the ones that
        already exist and deletes the remaining ones

    This function can make dry runs to test the XML file upload and check if there are any errors.

    Parameters
    ----------
    user : core.models.User
        The User that requested the data upload.

    xml : xml.etree.ElementTree.ElementTree
        The parsed XML file.

    strategy : str
        The requested strategy for the data upload.

    dry_run : bool
        Determines whether this is a dry run (test run) or not.

    Returns
    ------
    UploadResult
        A structure that represents the upload process result, with the number of
        entries received, the number of created/updated/deleted Services and the list of errors.
    """
    raw_services, errors = parse_xml_services(xml)
    context = UploadSimpleDataContext(
        strategy=strategy,
        dry_run=dry_run,
        parsed_entries=raw_services,
        parsing_errors=errors,
        object_manager=Service.objects,
        log_string_sg="Service",
        log_string_pl="services",
    )
    return upload_simple_data(user, context)


def load_locations_xml(xml):
    result = defaultdict(list)
    errors = []
    ids = []
    root = xml.getroot()

    regions = root.find("Regions").findall("Region")
    districts = root.find("Districts").findall("District")
    villages = root.find("Villages").findall("Village")
    municipalities = root.find("Municipalities").findall("Municipality")
    all_locations = chain(regions, districts, villages, municipalities)
    for elm in all_locations:
        data = {}
        try:
            if elm.tag == "Region":
                data["type"] = "R"
                data["code"] = get_xml_element(elm, "RegionCode")
                data["name"] = get_xml_element(elm, "RegionName")
            elif elm.tag == "District":
                data["type"] = "D"
                data["parent"] = get_xml_element(elm, "RegionCode")
                data["code"] = get_xml_element(elm, "DistrictCode")
                data["name"] = get_xml_element(elm, "DistrictName")
            elif elm.tag == "Municipality":
                data["type"] = "W"
                data["parent"] = get_xml_element(elm, "DistrictCode")
                data["code"] = get_xml_element(elm, "MunicipalityCode")
                data["name"] = get_xml_element(elm, "MunicipalityName")
            elif elm.tag == "Village":
                data["type"] = "V"
                data["parent"] = get_xml_element(elm, "MunicipalityCode")
                data["code"] = get_xml_element(elm, "VillageCode")
                data["name"] = get_xml_element(elm, "VillageName")
                data["male_population"] = get_xml_element(elm, "MalePopulation")
                data["female_population"] = get_xml_element(elm, "FemalePopulation")
                data["other_population"] = get_xml_element(elm, "OtherPopulation")
                data["families"] = get_xml_element(elm, "Families")
        except ValueError as exc:
            logger.exception(exc)
            errors.append(f"A field is missing for {elm}")
            continue
        if not data["code"]:
            errors.append("Location has no code")
        if data["code"].lower() in ids:
            errors.append(f"Code '{data['code']}' already exists in the list")
            continue
        else:
            ids.append(data["code"].lower())
            result[data["type"]].append(data)

    return result, errors


@functools.lru_cache(None)
def get_parent_location(code):
    return Location.objects.filter(code=code, *filter_validity()).first()


def __chunk_list(l, size=1000):
    return (l[index:index + size] for index in range(0, len(l), size))


def upload_locations(user, xml, strategy=STRATEGY_INSERT, dry_run=False):
    logger.info(f"Uploading locations with strategy={strategy} & dry_run={dry_run}")
    try:
        locations, errors = load_locations_xml(xml)
    except Exception as exc:
        raise InvalidXMLError("XML file is invalid.") from exc
    result = UploadResult(errors=errors)

    ids = [x["code"] for x in chain.from_iterable(locations.values())]
    existing_locations = {}
    for ids_chunk in __chunk_list(ids):
        existing_locations.update({
            loc.code: loc for loc in Location.objects.filter(code__in=ids_chunk, *filter_validity())
        })

    get_parent_location.cache_clear()

    for locations in locations.values():
        for loc in locations:
            result.sent += 1
            existing = existing_locations.get(loc["code"], None)

            if existing and strategy == STRATEGY_INSERT:
                result.errors.append(f"{existing.code} already exists")
                continue
            elif not existing and strategy == STRATEGY_UPDATE:
                result.errors.append(f"{loc['code']} does not exist")
                continue

            if loc.get("parent", None):
                parent_code = loc["parent"]
                del loc["parent"]

                parent = get_parent_location(parent_code)
                if not parent:
                    result.errors.append(f"Parent {parent_code} does not exist")
                    continue
                loc["parent"] = parent

            if strategy == STRATEGY_INSERT or not existing:
                if not dry_run:
                    location = Location.objects.create(audit_user_id=user.id_for_audit, **loc)
                    if location.type == 'D':
                        UserDistrict.objects.get_or_create(
                            user=user.i_user,
                            location=location,
                            audit_user_id=user.id_for_audit,
                        )
                result.created += 1
            elif existing:
                if not dry_run:
                    existing.save_history()
                    [setattr(existing, key, loc[key]) for key in loc]
                    existing.save()
                result.updated += 1

    return result


HF_FIELDS_MAP = {
    "LegalForm": "legal_form_id",
    "Level": "level",
    "SubLevel": "sub_level_id",
    "Code": "code",
    "Name": "name",
    "Address": "address",
    "DistrictCode": "district_code",
    "Phone": "phone",
    "Fax": "fax",
    "Email": "email",
    "CareType": "care_type",
    "AccountCode": "acc_code",
    "ItemsPricelistName": "items_pricelist_name",
    "ServicesPricelistName": "services_pricelist_name",
}


def load_health_facilities_xml(xml):
    result = []
    errors = []
    root = xml.getroot()

    for elm in root.find("HealthFacilityDetails").findall("HealthFacility"):
        data = {}
        for field in elm.iter():
            if field.tag in HF_FIELDS_MAP:
                text_value = field.text.strip() if field.text else None
                if text_value != "" and text_value is not None:
                    data[HF_FIELDS_MAP[field.tag]] = text_value

        if not (data.get("code") and data.get("name")):
            errors.append("Health facility has no code or no name defined: %s" % data)
            continue

        if not data.get("legal_form_id"):
            errors.append(
                "Health facility '%s' has no legal form defined" % data["code"]
            )
            continue

        if not data.get("level"):
            errors.append("Health facility '%s' has no level defined" % data["code"])
            continue

        if not data.get("care_type"):
            errors.append(
                "Health facility '%s' has no care type defined" % data["code"]
            )
            continue

        if not data.get("level"):
            errors.append("Health facility '%s' has no legal form" % data["code"])
            continue

        result.append(data)

    return result, errors


@functools.lru_cache(None)
def get_pricelist(name, type):
    if type == "services":
        return ServicesPricelist.objects.filter(name=name, *filter_validity()).first()
    elif type == "items":
        return ItemsPricelist.objects.filter(name=name, *filter_validity()).first()


def upload_health_facilities(user, xml, strategy=STRATEGY_INSERT, dry_run=False):
    get_parent_location.cache_clear()

    logger.info(
        "Uploading health facilities with strategy=%s & dry_run=%s", strategy, dry_run
    )
    try:
        raw_health_facilities, errors = load_health_facilities_xml(xml)
    except Exception as exc:
        raise InvalidXMLError("XML file is invalid.") from exc

    result = UploadResult(errors=errors)
    db_health_facilities = {
        x.code: x
        for x in HealthFacility.objects.filter(
            code__in=[x["code"] for x in raw_health_facilities], *filter_validity()
        )
    }
    for facility in raw_health_facilities:
        logger.debug("Processing facility: %s" % facility)
        existing = db_health_facilities.get(facility["code"], None)
        result.sent += 1

        if existing and strategy == STRATEGY_INSERT:
            result.errors.append(f"Health facility '{existing.code}' already exists")
            continue
        elif not existing and strategy == STRATEGY_UPDATE:
            result.errors.append(f"Health facility '{facility['code']}' does not exist")
            continue

        facility["location"] = get_parent_location(facility.get("district_code", None))
        if not facility["location"]:
            result.errors.append(
                f"Location '{facility['district_code']}'' does not exist"
            )
            continue

        if "services_pricelist_name" in facility:
            facility["services_pricelist_id"] = get_pricelist(
                facility.pop("services_pricelist_name"), "services"
            ).id
        else:
            # if we want to remove pricelist through updating
            facility["services_pricelist_id"] = None
        if "items_pricelist_name" in facility:
            facility["items_pricelist_id"] = get_pricelist(
                facility.pop("items_pricelist_name"), "items"
            ).id
        else:
            facility["items_pricelist_id"] = None

        try:
            if strategy == STRATEGY_INSERT:
                if not dry_run:
                    HealthFacility.objects.create(
                        offline=False, audit_user_id=user.id_for_audit, **facility
                    )
                result.created += 1

            else:
                if existing:
                    if not dry_run:
                        existing.save_history()
                        [setattr(existing, key, facility[key]) for key in facility]
                        existing.save()
                    result.updated += 1
                else:
                    if not dry_run:
                        existing = HealthFacility.objects.create(
                            offline=False, audit_user_id=user.id_for_audit, **facility
                        )
                    result.created += 1
        except Exception as exc:
            logger.exception(exc)
            result.errors.append(
                "Cannot create or update health facility : %s" % facility["code"]
            )

    logger.debug(f"Finished processing of health facilities: {result}")
    return result


def create_master_data_export(user):
    queries = {
        "confirmationTypes": """SELECT "ConfirmationTypeCode", "ConfirmationType", "SortOrder", "AltLanguage" FROM "tblConfirmationTypes";""",
        "controls": """SELECT "FieldName", "Adjustibility" FROM "tblControls";""",
        "education": """SELECT "EducationId", "Education", "SortOrder", "AltLanguage" FROM "tblEducations";""",
        # and not educations
        "familyTypes": """SELECT "FamilyTypeCode", "FamilyType", "SortOrder", "AltLanguage" FROM "tblFamilyTypes";""",
        "hf": """SELECT "HfID", "HFCode", "HFName", "LocationId", "HFLevel" FROM "tblHF" WHERE "ValidityTo" IS NULL;""",
        "identificationTypes": """SELECT "IdentificationCode", "IdentificationTypes", "SortOrder", "AltLanguage" FROM "tblIdentificationTypes";""",
        "languages": """SELECT "LanguageCode", "LanguageName", "SortOrder" FROM "tblLanguages";""",
        "locations": """SELECT "LocationId", "LocationCode", "LocationName", "ParentLocationId", "LocationType" FROM "tblLocations" WHERE "ValidityTo" IS NULL AND NOT("LocationName"='Funding' OR "LocationCode"='FR' OR "LocationCode"='FD' OR "LocationCode"='FW' OR "LocationCode"='FV')""",
        "officers": """SELECT "OfficerID", "OfficerUUID", "Code", "LastName", "OtherNames", "Phone", "LocationId", "OfficerIDSubst", TO_CHAR("WorksTo", 'yyyy-MM-dd') worksTo FROM "tblOfficer" WHERE "ValidityTo" IS NULL;"""
        if connection.vendor == "postgresql" else
        """SELECT "OfficerID", "OfficerUUID", "Code", "LastName", "OtherNames", "Phone", "LocationId", "OfficerIDSubst", FORMAT("WorksTo", 'yyyy-MM-dd') worksTo FROM "tblOfficer" WHERE "ValidityTo" IS NULL;""",
        "payers": """SELECT "PayerID", "PayerName", "LocationId" FROM "tblPayer" WHERE "ValidityTo" IS NULL;""",
        "products": """SELECT "ProdID", "ProductCode", "ProductName", "LocationId", "InsurancePeriod", TO_CHAR("DateFrom", 'yyyy-MM-dd')dateFrom, TO_CHAR("DateTo", 'yyyy-MM-dd')dateTo, "ConversionProdID" , "LumpSum", "MemberCount", "PremiumAdult", "PremiumChild", "RegistrationLumpSum", "RegistrationFee", "GeneralAssemblyLumpSum", "GeneralAssemblyFee", "StartCycle1", "StartCycle2", "StartCycle3", "StartCycle4", "GracePeriodRenewal", "MaxInstallments", "WaitingPeriod", "Threshold", "RenewalDiscountPerc", "RenewalDiscountPeriod", "AdministrationPeriod", "EnrolmentDiscountPerc", "EnrolmentDiscountPeriod", "GracePeriod" FROM "tblProduct" WHERE "ValidityTo" IS NULL"""
        if connection.vendor == "postgresql" else
        """SELECT "ProdID", "ProductCode", "ProductName", "LocationId", "InsurancePeriod", FORMAT("DateFrom", 'yyyy-MM-dd')dateFrom, FORMAT("DateTo", 'yyyy-MM-dd')dateTo, "ConversionProdID" , "LumpSum", "MemberCount", "PremiumAdult", "PremiumChild", "RegistrationLumpSum", "RegistrationFee", "GeneralAssemblyLumpSum", "GeneralAssemblyFee", "StartCycle1", "StartCycle2", "StartCycle3", "StartCycle4", "GracePeriodRenewal", "MaxInstallments", "WaitingPeriod", "Threshold", "RenewalDiscountPerc", "RenewalDiscountPeriod", "AdministrationPeriod", "EnrolmentDiscountPerc", "EnrolmentDiscountPeriod", "GracePeriod" FROM "tblProduct" WHERE "ValidityTo" IS NULL""",
        "professions": """SELECT "ProfessionId", "Profession", "SortOrder", "AltLanguage" FROM "tblProfessions";""",
        "relations": """SELECT "RelationId", "Relation", "SortOrder", "AltLanguage" FROM "tblRelations";""",
        "phoneDefaults": """SELECT "RuleName", "RuleValue" FROM "tblIMISDefaultsPhone";""",
        "genders": """SELECT "Code", "Gender", "AltLanguage", "SortOrder" FROM "tblGender";""",
    }

    results = {}

    with connection.cursor() as cursor:
        for key, query in queries.items():
            cursor.execute(query)
            results[key] = dictfetchall(cursor)

    with tempfile.TemporaryDirectory() as tmp_dir_name:
        master_data_file_path = os.path.join(tmp_dir_name, "MasterData.txt")
        with open(master_data_file_path, "w") as file:
            file.write(json.dumps(results))

        zip_file = tempfile.NamedTemporaryFile(
            "wb",
            prefix=f"master_data_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            suffix=".zip",
            delete=False,
        )
        # We close it directly since the only thing we want is to have a temporary file that will not be deleted
        zip_file.close()

        pyminizip.compress(
            master_data_file_path,
            "",
            zip_file.name,
            ToolsConfig.get_master_data_password(),
            5,
        )

        return zip_file


def create_officer_feedbacks_export(user, officer):
    """
    SELECT F.ClaimId,F.OfficerId,O.Code OfficerCode, I.CHFID, I.LastName, I.OtherNames, HF.HFCode, HF.HFName,C.ClaimCode,CONVERT(NVARCHAR(10),C.DateFrom,103)DateFrom, CONVERT(NVARCHAR(10),C.DateTo,103)DateTo,O.Phone, CONVERT(NVARCHAR(10),F.FeedbackPromptDate,103)FeedbackPromptDate"
    FROM tblFeedbackPrompt F INNER JOIN tblOfficer O ON F.OfficerId = O.OfficerId"
    INNER JOIN tblClaim C ON F.ClaimId = C.ClaimId"
    INNER JOIN tblInsuree I ON C.InsureeId = I.InsureeId"
    INNER JOIN tblHF HF ON C.HFID = HF.HFID"
    WHERE F.ValidityTo Is NULL AND O.ValidityTo IS NULL"
    AND O.Code = @OfficerCode"
    AND C.FeedbackStatus = 4"

    """

    prompts = FeedbackPrompt.objects.filter(*filter_validity())\
        .filter(officer_id=officer.id, claim__feedback_status=Claim.FEEDBACK_SELECTED)\
        .select_related('claim',
                        'claim__insuree',
                        'claim__health_facility')
    results = []
    format_date = '%d-%m-%Y'
    for p in prompts:
        results.append({
            "ClaimID": p.claim_id,
            "OfficerID": officer.id,
            "OfficerCode": officer.code,
            "CHFID": p.claim.insuree.chf_id,
            "LastName": p.claim.insuree.last_name,
            "OtherNames": p.claim.insuree.other_names,
            "HFCode": p.claim.health_facility.code,
            "HFName": p.claim.health_facility.name,
            "ClaimCode": p.claim.code,
            "DateFrom": p.claim.date_from.strftime(format_date) if p.claim.date_from else None,
            "DateTo": p.claim.date_to.strftime(format_date) if p.claim.date_to else None,
            "Phone": officer.phone,
            "FeedbackPromptDate": p.feedback_prompt_date.strftime(format_date) if p.feedback_prompt_date else None
        })

    with tempfile.TemporaryDirectory() as tmp_dir_name:
        file_name = f"feedbaks_{officer.code}.txt"
        file_path = os.path.join(tmp_dir_name, file_name)
        with open(file_path, "w") as file:
            file.write(json.dumps(results))

        zip_file = tempfile.NamedTemporaryFile(
            "wb",
            prefix=f"feedbacks_{officer.code}_{datetime.datetime.now().isoformat()}",
            suffix=".zip",
            delete=False,
        )

        zf = zipfile.ZipFile(zip_file, "w")
        zf.write(file_path, file_name)
        zf.close()
        return zip_file


def create_officer_renewals_export(user, officer):
    from policy.models import PolicyRenewal
    format_date = '%d-%m-%Y'
    renewals = PolicyRenewal.objects.filter(
        new_officer=officer, *filter_validity()
    ).select_related(
        "policy",
        "insuree",
        "insuree__family",
        "insuree__family__location",
    )

    results = []

    for renewal in renewals:
        results.append(
            {
                "RenewalId": renewal.id,
                "PolicyId": renewal.policy_id,
                "OfficerId": officer.id,
                "OfficerCode": officer.code,
                "CHFID": renewal.insuree.chf_id,
                "LastName": renewal.insuree.last_name,
                "OtherNames": renewal.insuree.other_names,
                "ProductCode": renewal.new_product.code,
                "ProductName": renewal.new_product.name,
                "ProdId": renewal.new_product.id,
                "VillageName": renewal.insuree.family.location.name,
                "FamilyId": renewal.insuree.family_id,
                "EnrollDate": renewal.renewal_date.strftime(format_date),
                "PolicyStage": "R",
                "PolicyValue": renewal.policy.value,
            }
        )

    with tempfile.TemporaryDirectory() as tmp_dir_name:
        file_name = f"renewals_{officer.code}.txt"
        file_path = os.path.join(tmp_dir_name, file_name)
        with open(file_path, "w") as file:
            file.write(json.dumps(results))

        zip_file = tempfile.NamedTemporaryFile(
            "wb",
            prefix=f"renewals_{officer.code}_{datetime.datetime.now().isoformat()}",
            suffix=".zip",
            delete=False,
        )

        zf = zipfile.ZipFile(zip_file, "w")
        zf.write(file_path, file_name)
        zf.close()
        return zip_file


def get_phone_extract_data(location_id):
    """uspPhoneExtract"""
    with connection.cursor() as cur:
        sql = """
            DECLARE @ret int;
            EXEC @ret = [dbo].[uspPhoneExtract] @LocationId = %s;
            SELECT @ret;
        """

        cur.execute(sql, (location_id,))
        # We have to take the second result set. That's the one that contains the results
        cur.nextset()
        cur.nextset()
        if cur.description is None:
            return
        return dictfetchall(cur)


def get_controls():
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT "FieldName", "Adjustibility", "Usage" FROM "tblControls";"""
        )
        return cursor.fetchall()  # Yes, the typo in 'Adjustibility' is on purpose.


def create_phone_extract_db(location_id, with_insuree=False):
    location = Location.objects.get(id=location_id, *filter_validity())
    if not location:
        raise ValueError(f"Location {location_id} does not exist")

    with tempfile.TemporaryDirectory() as tmp_dir:
        filename = f"phone_extract_{location.code}.db3"
        db_file_path = os.path.join(tmp_dir, filename)

        db_con = sqlite3.connect(db_file_path)

        db_con.executescript(
            """
            CREATE TABLE tblPolicyInquiry(CHFID text,Photo BLOB, InsureeName Text, DOB Text, Gender Text, ProductCode Text, ProductName Text, ExpiryDate Text, Status Text, DedType Int, Ded1 Int, Ded2 Int, Ceiling1 Int, Ceiling2 Int);
            CREATE TABLE tblReferences([Code] Text, [Name] Text, [Type] Text, [Price] REAL);
            CREATE TABLE tblControls([FieldName] Text, [Adjustibility] Text, [Usage] Text);
            CREATE TABLE tblClaimAdmins([Code] Text, [Name] Text);
        """
        )

        if with_insuree:
            rows = get_phone_extract_data(location_id) or []
            with db_con:
                db_con.executemany(
                    """
                    INSERT INTO tblPolicyInquiry(CHFID, Photo, InsureeName, DOB, Gender, ProductCode, ProductName, ExpiryDate, Status, DedType, Ded1, Ded2, Ceiling1, Ceiling2)
                    VALUES (:CHFID, :PhotoPath, :InsureeName, :DOB, :Gender, :ProductCode, :ProductName, :ExpiryDate, :Status, :DedType, :Ded1, :Ded2, :Ceiling1, :Ceiling2)
                """,
                    rows,
                )

        # References
        with db_con:
            # Medical Services
            db_con.executemany(
                "INSERT INTO tblReferences(Code, Name, Type, Price) VALUES (?, ?, 'S', ?)",
                Service.objects.filter(*filter_validity()).values_list(
                    "code", "name", "price"
                ),
            )

            # Medical Items
            db_con.executemany(
                "INSERT INTO tblReferences(Code, Name, Type, Price) VALUES (?, ?, 'I', ?)",
                Item.objects.filter(*filter_validity())
                .values_list("code", "name", "price")
                .all(),
            )

            # Medical Diagnosis
            db_con.executemany(
                "INSERT INTO tblReferences(Code, Name, Type, Price) VALUES (?, ?, 'D', 0)",
                Diagnosis.objects.filter(*filter_validity())
                .values_list("code", "name")
                .all(),
            )

        # Controls
        with db_con:
            db_con.executemany(
                "INSERT INTO tblControls (FieldName, Adjustibility, Usage) VALUES (?, ?, ?)",
                # Yes, the typo in 'Adjustibility' is on purpose.
                get_controls(),
            )

        from django.db.models.functions import Concat
        from django.db.models import CharField, Value as V

        # Claim Admins
        admins = (
            ClaimAdmin.objects.filter(
                health_facility__location_id=location_id,
                health_facility__validity_to__isnull=True,
                *filter_validity(),
            )
            .annotate(
                name=Concat(
                    "last_name", V(" "), "other_names", output_field=CharField()
                )
            )
            .values_list("code", "name")
        )

        with db_con:
            db_con.executemany(
                "INSERT INTO tblClaimAdmins (Code, Name) VALUES (?, ?)", admins
            )

        db_con.close()
        return open(db_file_path, "rb")


def create_phone_extract(user, location_id, with_insuree=False):
    file = create_phone_extract_db(location_id, with_insuree=with_insuree)
    filename = os.path.basename(file.name)
    extract = Extract(
        location_id=location_id,
        type=1,
        audit_user_id=user.id_for_audit,
        direction=0,
        filename=filename,
    )

    extract.stored_file.save(filename, file)

    return extract


def upload_claim(user, xml):
    logger.info(f"Uploading claim with user {user.id}")

    if settings.ROW_SECURITY:
        logger.info("Check that user can upload claims in claims' health facilities")
        hf_code = xml.find("Claim").find("Details").find("HFCode").text
        hfs = LocationManager().build_user_location_filter_query(
            user._u, queryset=HealthFacility.filter_queryset().filter(code=hf_code), loc_types=['D'])
        if len(hfs) < 1:
            raise InvalidXMLError(
                f"User cannot upload claims for health facility {hf_code}"
            )

    with connection.cursor() as cursor:
        cursor.execute(
            """
            DECLARE @ret int;
            EXEC @ret = [dbo].[uspUpdateClaimFromPhone] @XML = %s, @ByPassSubmit = 1;
            SELECT @ret;
        """,
            (ElementTree.tostring(xml),),
        )
        # We have to take the second result set. That's the one that contains the results
        cursor.nextset()
        cursor.nextset()
        if cursor.description is None:
            return
        res = cursor.fetchone()[0]

        if res == 0:
            return True
        elif res == 1:
            raise InvalidXMLError("Health facility invalid.")
        elif res == 2:
            raise InvalidXMLError("Duplicated claim code")
        elif res == 3:
            raise InvalidXMLError("Unknown insuree number")
        elif res == 4:
            raise InvalidXMLError("Invalid end date")
        elif res == 5:
            raise InvalidXMLError("Unknown diagnosis code")
        elif res == 7:
            raise InvalidXMLError("Unknown medical item code")
        elif res == 8:
            raise InvalidXMLError("Unknown medical service code")
        elif res == 9:
            raise InvalidXMLError("Unknown claim admin code")
        elif res == -1:
            raise InvalidXMLError("Unknown error occurred")


def open_offline_archive(archive: str, password: str = None):
    """
    Offline extracts are XML or JSON files, sometimes with pictures zipped with a password protection and then named
    .RAR. This method extracts such archives into a temporary directory
    """
    temp_folder = tempfile.mkdtemp(prefix="offline_archive")
    password = password if password else ToolsConfig.get_master_data_password()
    password = ")(#$1HsD"
    with pyzipper.AESZipFile(archive) as zf:
        zf.setpassword(str.encode(password))
        zf.extractall(path=temp_folder)
    return temp_folder


def get_or_create_insuree_from_xml(xml, audit_user_id, chf_id=None, family_id=None):
    # TODO use get() for missing field safety
    return Insuree.objects.get_or_create(
        validity_to=None,
        chf_id=chf_id if chf_id else xml.get("CHFID"),
        defaults={
            # "InsureeId": xml.get("InsureeId"),
            "chf_id": xml.get("CHFID"),
            "last_name": xml.get("LastName"),
            "other_names": xml.get("OtherNames"),
            "dob": xml.get("DOB"),
            "gender_id": xml.get("Gender"),
            "marital": xml.get("Marital"),
            "head": xml.get("isHead"),
            # "identification_number": xml.get("IdentificationNumber"),  # TODO ???
            "phone": xml.get("Phone"),
            # "photo_path": xml.get("PhotoPath"),  # TODO handle separately
            "card_issued": xml.get("CardIssued"),
            "relationship_id": xml.get("Relationship"),
            "profession_id": xml.get("Profession"),
            "education_id": xml.get("Education"),
            "email": xml.get("Email"),
            "type_of_id_id": xml.get("TypeOfId"),
            "health_facility_id": xml.get("HFID"),
            "current_address": xml.get("CurrentAddress"),
            "geolocation": xml.get("GeoLocation"),
            "current_village_id": xml.get("CurVillage"),
            "offline": xml.get("isOffline"),
            # "vulnerability": xml.get("Vulnerability"),  # TODO Ignored by stored proc, no field in tblInsuree
            "audit_user_id": audit_user_id,
            "family_id": family_id,
        }
    )


def get_or_create_family_from_xml(xml, audit_user_id, head_insuree_id=None):
    if head_insuree_id:
        head_id = head_insuree_id
    else:
        head_id = Insuree.objects.get(
            validity_to__isnull=True,
            chf_id=xml.get("HOFCHFID") if xml.get("HOFCHFID") else xml.get("CHFID")
        ).id

    return Family.objects.get_or_create(
        validity_to=None,
        head_insuree__chf_id=head_id,
        defaults={
            # "family_id": xml.get("FamilyId"),
            "head_insuree_id": head_id,
            "location_id": xml.get("LocationId"),
            # "hofchfid": xml.get("HOFCHFID"), # not stored in the family in DB
            "poverty": xml.get("Poverty"),
            "family_type_id": xml.get("FamilyType"),
            "address": xml.get("FamilyAddress"),
            "ethnicity": xml.get("Ethnicity"),
            "confirmation_no": xml.get("ConfirmationNo"),
            "confirmation_type_id": xml.get("ConfirmationType"),
            "is_offline": xml.get("isOffline"),
            "audit_user_id": audit_user_id,
        }
    )


def get_or_create_policy_from_xml(xml, audit_user_id, family_id):
    return Policy.objects.get_or_create(
        validity_to=None,
        family_id=family_id,
        product_id=xml.get("ProdId"),
        defaults={
            "enroll_date": xml.get("EnrollDate"),
            "start_date": xml.get("StartDate"),
            "effective_date": xml.get("EffectiveDate"),
            "expiry_date": xml.get("ExpiryDate"),
            "status": xml.get("PolicyStatus"),
            "value": xml.get("PolicyValue"),
            "officer_id": xml.get("OfficerId"),
            "stage": xml.get("PolicyStage"),
            "offline": xml.get("isOffline"),
            # "control_number": xml.get("ControlNumber"),  # Not sure what it should map to ?
            "audit_user_id": audit_user_id,
        }
    )


def get_or_create_premium_from_xml(xml, audit_user_id, policy_id):
    is_photo_fee = xml.get("isPhotoFee", None)
    if is_photo_fee is not None:
        is_photo_fee = str(is_photo_fee).lower() == "true"
    return Premium.objects.get_or_create(
        validity_to=None,
        policy_id=policy_id,
        payer_id=xml.get("PayerId"),
        pay_date=xml.get("PayDate"),
        defaults={
            "amount": xml.get("Amount"),
            "receipt": xml.get("Receipt"),
            "pay_type": xml.get("PayType"),
            "is_photo_fee": is_photo_fee,
            "is_offline": xml.get("isOffline"),
            "audit_user_id": audit_user_id,
        }
    )


def get_or_create_insuree_policy_from_xml(xml, audit_user_id, policy_id, insurees):
    # We already have the actual policy_id but we need to find the actual ID of the insuree
    insuree = insurees.get(xml.get("InsureeId", None), None)
    if not insuree or "new_id" not in insuree:
        logger.warning("Could not find insuree id %s", xml.get("InsureeId"))
    return InsureePolicy.objects.get_or_create(
        validity_to=None,
        policy_id=policy_id,
        insuree_id=insuree.get("new_id"),
        effective_date=xml.get("EffectiveDate"),
        defaults={"audit_user_id": audit_user_id},
    )


def element_to_dict(element):
    return {x.tag: x.text for x in element}


def load_enrollment_xml(xml_file):
    """
    Loads an enrollment XML file into a bunch of dicts for easier processing
    """
    logger.debug("Processing file %s", xml_file)
    xml = sanitize_xml(xml_file)
    logger.debug(xml.getroot())
    file_info = xml.find("FileInfo")
    file_user_id = file_info.find("UserId")
    file_officer_id = file_info.find("OfficerId")

    # The XML has an array but we'll use a dict by CHFID for convenience
    # Duplicates will be handled by the procedure anyway
    families = {}
    for family_element in xml.find("Families").findall("Family"):
        family = element_to_dict(family_element)
        # TODO add more validation on the XML
        families[family["FamilyId"]] = family
    logger.debug("Loaded %s families from XML", len(families))
    insurees = {}
    for insuree_element in xml.find("Insurees").findall("Insuree"):
        insuree = element_to_dict(insuree_element)
        # TODO add more validation on the XML
        insurees[insuree["InsureeId"]] = insuree
    policies = {}
    for policy_element in xml.find("Policies").findall("Policy"):
        policy = element_to_dict(policy_element)
        # TODO add more validation on the XML
        policies[policy["PolicyId"]] = policy
    premiums = {}
    for premium_element in xml.find("Premiums").findall("Premium"):
        premium = element_to_dict(premium_element)
        # TODO add more validation on the XML
        premiums[premium["PremiumId"]] = premium
    insuree_policies = {}
    for insuree_policy_element in xml.find("InsureePolicies").findall("InsureePolicy"):
        insuree_policy = element_to_dict(insuree_policy_element)
        # TODO add more validation on the XML
        insuree_policies[insuree_policy["PolicyId"]] = insuree_policy

    return file_user_id, file_officer_id, families, insurees, policies, premiums, insuree_policies


def upload_enrollments(archive, user):
    """
    This method loads an Offline XML/ZIP/RAR archive from a file.
    These archives are usually named .RAR but they're actually .ZIP with a password and AES encryption.
    The AES encryption is not supported by the stock ZIP library of Python.
    The data is then stored separately between insurees, families, policies etc, so we'll do our best to recompose the
    relations.
    :param user: User object from authentication
    :param archive: file to read
    """
    logger.info(f"Uploading enrollments with user {user.id}")

    archive_dir = open_offline_archive(archive)
    for xml_file in glob.glob(os.path.join(archive_dir, "*.xml")):
        # TODO check consistency of IDs
        file_user_id, file_officer_id, families, insurees, policies, premiums, insuree_policies = \
            load_enrollment_xml(xml_file)

        for family_id, xml_family in families.items():
            # First, identify or create the head of family
            xml_head = insurees[xml_family["InsureeId"]]
            if not xml_head:
                # TODO
                logger.warning("Processing file %s, could not find insuree %s for family %s", None, None, None)
                continue

            head, head_created = get_or_create_insuree_from_xml(xml_head, user.id_for_audit)
            xml_head["new_id"] = head.id

            # TODO: verify that the family has a corresponding insuree
            # TODO: verify that the family doesn't already exist (that the insuree number would be present already)
            db_family, db_family_created = get_or_create_family_from_xml(xml_family, user.id_for_audit)
            xml_family["new_id"] = db_family.id
            head.family = db_family
            head.save()

            # Now that we have the head of the family, insert the other insurees of the family
            for insuree in insurees.values():
                if insuree["FamilyId"] == xml_family["FamilyId"] and "new_id" not in insuree:
                    db_insuree, db_insuree_created = get_or_create_insuree_from_xml(insuree, user.id_for_audit,
                                                                                    db_family.id)
                    insuree["new_id"] = db_insuree.id
                    logger.debug("Created family %s member %s (%s)", db_family.id, db_insuree.chf_id,
                                 db_insuree.other_names)

            for policy in policies.values():
                if policy["FamilyId"] == xml_family["FamilyId"] and "new_id" not in policy:
                    db_policy, db_policy_created = get_or_create_policy_from_xml(policy, user.id_for_audit,
                                                                                 db_family.id)
                    policy["new_id"] = db_policy.id
                    logger.debug("Created policy for family %s", db_family.id)

                    # We are processing only the premiums for the policy we just created
                    for premium in premiums.values():
                        if premium["PolicyId"] == policy["PolicyId"] and "new_id" not in premium:
                            db_premium, db_premium_created = get_or_create_premium_from_xml(premium, user.id_for_audit,
                                                                                            db_policy.id)
                            premium["new_id"] = db_premium.id
                            logger.debug("Created premium for policy %s, value=%s", db_premium.policy_id,
                                         db_premium.amount)

                    for insuree_policy in insuree_policies.values():
                        if insuree_policy["PolicyId"] == policy["PolicyId"] and "new_id" not in insuree_policy:
                            db_ip, db_ip_created = get_or_create_insuree_policy_from_xml(
                                insuree_policy, user.id_for_audit, db_policy.id, insurees)
                            insuree_policy["new_id"] = db_ip.id
                            logger.debug("Created insuree_policy for policy %s, insuree %s", db_ip.policy_id,
                                         db_ip.insuree_id)

            # TODO check that there are no leftovers
            logger.debug("Family %s imported successfully", family_id)

    logger.debug("Done loading enrollment archive")


def upload_renewals(archive, user):
    """
    This method loads an encrypted archive of renewals.
    Unlike the enrollments, the renewals are provided both as XML and JSON, so we'll use the latter.
    It's stored as one renewal per file. No array or anything inside.
    """
    logger.info(f"Uploading renewals with user {user.id}")
    renewed_policies = []
    failed_renewals = []

    archive_dir = open_offline_archive(archive)
    for json_file in glob.glob(os.path.join(archive_dir, "*.json")):
        with open(json_file) as f:
            renewal = json.load(f)
            policy = renewal.get("Policy")
            if not policy:
                logger.warning("Empty renewal in %s", json_file)
                continue
            db_policy = Policy.objects.filter(
                family__head_insuree__chf_id=policy.get("CHFID"),
                product__code=policy.get("ProductCode"),
                validity_to__isnull=True,
            ).first()
            if not db_policy:
                logger.warning("Policy renewal without existing policy")
                failed_renewals.append(policy)
                continue
            db_policy.save_history()
            # TODO save with:
            # RenewalId, Officer, CHFID, ReceiptNo, ProductCode, Amount, Date, Discontinue, PayerId
            db_policy.status = Policy.STATUS_ACTIVE
            db_policy.save()
            update_insuree_policies(db_policy, user)
            renewed_policies.append(policy)


def upload_feedbacks(archive, user):
    """
    This method loads an encrypted archive of renewals.
    """
    logger.info(f"Uploading feedback with user {user.id}")
    feedback_saved = []
    failed_feedback = []

    archive_dir = open_offline_archive(archive)
    for json_file in glob.glob(os.path.join(archive_dir, "*.json")):
        with open(json_file) as f:
            feedback = json.load(f)
            db_claim = Claim.objects.filter(
                id=feedback.get("ClaimId"),
                insuree__chf_id=feedback.get("CHFID"),
                validity_to__isnull=True,
            ).first()
            if not db_claim:
                logger.warning("Claim feedback without existing claim")
                failed_feedback.append(feedback)
                continue
            answers = feedback.get("Answers")
            if answers is None or len(answers) != 5:
                logger.warning("Claim feedback has an Answers field of length %s, expecting 5", len(answers))
                failed_feedback.append(feedback)
                continue
            (care_rendered, payment_asked, drug_prescribed, drug_received, assessment) = answers

            # TODO migrate this to a Claim service
            db_feedback, db_feedback_created = Feedback.objects.get_or_create(
                claim=db_claim,
                validity_to=None,
                defaults={
                    care_rendered: care_rendered,
                    payment_asked: payment_asked,
                    drug_prescribed: drug_prescribed,
                    drug_received: drug_received,
                    assessment: assessment,
                    feedback_date: feedback.get("Date"),
                    audit_user_id: user.id_for_audit,
                }
            )
            if db_feedback_created:
                db_claim.save_history()
                from core.utils import TimeUtils
                db_claim.validity_from = TimeUtils.now()
                db_claim.feedback = db_feedback
                db_claim.feedback_status = Claim.FEEDBACK_DELIVERED
                db_claim.feedback_available = True
                db_claim.save()
            feedback_saved.append(feedback)


def validate_imported_item_row(row):
    # TODO : refactor this function and the code used in validating XML uploads
    categories = [
        row.get("adult_cat", 1),
        row.get("minor_cat", 1),
        row.get("male_cat", 1),
        row.get("female_cat", 1)
    ]
    if len(row["code"]) < 1 or len(row["code"]) > 6:
        raise ValidationError(f"Item '{row['code']}': code is invalid. Must be between 1 and 6 characters")
    elif len(row["name"]) < 1 or len(row["name"]) > 100:
        raise ValidationError(f"Item '{row['code']}': name is invalid. Must be between 1 and 100 characters")
    elif row["type"] not in Item.TYPE_VALUES:
        raise ValidationError(
            f"Item '{row['code']}': type is invalid. Must be one of the following: {Item.TYPE_VALUES}")
    elif row["care_type"] not in ItemOrService.CARE_TYPE_VALUES:
        raise ValidationError(
            f"Item '{row['code']}': care type is invalid. Must be one of the following: {ItemOrService.CARE_TYPE_VALUES}")
    elif any([cat not in VALID_PATIENT_CATEGORY_INPUTS for cat in categories]):
        raise ValidationError(
            f"Item '{row['code']}': patient categories are invalid. Must be one of the following: [0, 1]")
    elif "package" in row and (row["package"] is not None) and len(row["package"]) > 255:
        raise ValidationError(f"Item '{row['code']}': package is invalid. Must be maximum 255 characters")
    return


def validate_imported_service_row(row):
    # TODO : refactor this function and the code used in validating XML uploads
    categories = [row["adult_cat"], row["minor_cat"], row["male_cat"], row["female_cat"]]
    if len(row["code"]) < 1 or len(row["code"]) > 6:
        raise ValidationError(f"Service '{row['code']}': code is invalid. Must be between 1 and 6 characters")
    elif len(row["name"]) < 1 or len(row["name"]) > 100:
        raise ValidationError(f"Service '{row['code']}': name is invalid. Must be between 1 and 100 characters")
    elif row["type"] not in Service.TYPE_VALUES:
        raise ValidationError(
            f"Service '{row['code']}': type is invalid. Must be one of the following: {Service.TYPE_VALUES}")
    elif row["level"] not in Service.LEVEL_VALUES:
        raise ValidationError(
            f"Service '{row['code']}': level is invalid. Must be one of the following: {Service.LEVEL_VALUES}")
    elif row["care_type"] not in ItemOrService.CARE_TYPE_VALUES:
        raise ValidationError(
            f"Service '{row['code']}': care type is invalid. Must be one of the following: {ItemOrService.CARE_TYPE_VALUES}")
    elif any([cat not in VALID_PATIENT_CATEGORY_INPUTS for cat in categories]):
        raise ValidationError(
            f"Service '{row['code']}': patient categories are invalid. Must be one of the following: [0, 1]")
    elif "category" in row and \
            row["category"] is not None and \
            len(row["category"]) and \
            row["category"] not in Service.CATEGORY_VALUES:
        raise ValidationError(
            f"Service '{row['code']}': category is invalid. Must be one of the following: {Service.CATEGORY_VALUES}")
    return


def return_upload_result_json(success=True, xml_result: UploadResult = None, other_types_result: Result = None,
                              other_types_errors=None):
    """Returns a JSON structure containing the result of a data upload.

    The function's purpose is to normalize the different upload process results (XML and other types)
    in order to provide the frontend with a single data structure.

    This function can only be called with a `xml_result` or a `other_types_result` parameter. If both are provided,
    or none, this function will raise an exception.


    Parameters
    ----------
    success : bool
        Represents the upload success.

    xml_result: UploadResult
        Represents an XML upload result.

    other_types_result: Result
        Represents the upload results made with the django-import-export plugin.

    other_types_errors: List
        Represents the list of errors that happened during data upload with the plugin.


    Returns
    ------
    JsonResponse
        A JSON structure that represents the result of a data upload.


    Raises
    ------
    RuntimeError
        If both `xml_result` and `other_types_result` are provided, or none of them.
    """
    if xml_result is not None and other_types_result is not None:
        raise RuntimeError("You cannot provide two different types of upload result")

    if xml_result is None and other_types_result is None:
        raise RuntimeError("You must provide one type of upload result")

    response_data = {
        "success": success,
    }

    if xml_result is not None:
        response_data["data"] = {
            "sent": xml_result.sent,
            "created": xml_result.created,
            "updated": xml_result.updated,
            "deleted": xml_result.deleted,
            "skipped": 0,
            "invalid": 0,
            "failed": 0,
        }
        response_data["errors"] = xml_result.errors
    elif other_types_result is not None:
        response_data["data"] = {
            "sent": other_types_result.total_rows,
            "created": other_types_result.totals["new"],
            "updated": other_types_result.totals["update"],
            "deleted": other_types_result.totals["delete"],
            "skipped": other_types_result.totals["skip"],
            "invalid": other_types_result.totals["invalid"],
            "failed": other_types_result.totals["error"],
        }
        response_data["errors"] = other_types_errors

    return JsonResponse(data=response_data)
