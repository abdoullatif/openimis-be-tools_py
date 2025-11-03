from .constants import (
    STRATEGY_INSERT,
    STRATEGY_INSERT_UPDATE,
    STRATEGY_INSERT_UPDATE_DELETE,
    STRATEGY_UPDATE,
)
from rest_framework_xml.renderers import XMLRenderer

from rest_framework import serializers

from core.utils import PATIENT_CATEGORY_MASK_ADULT, PATIENT_CATEGORY_MASK_MALE, PATIENT_CATEGORY_MASK_MINOR, \
    PATIENT_CATEGORY_MASK_FEMALE
from location.apps import DEFAULT_CFG as LOCATION_DEFAULT_CFG


class FileSerializer(serializers.Serializer):
    file = serializers.FileField(required=True)


class UploadSerializer(FileSerializer):
    dry_run = serializers.BooleanField()
    strategy = serializers.ChoiceField(
        choices=(
            (STRATEGY_INSERT, "Insert only"),
            (STRATEGY_UPDATE, "Update only"),
            (STRATEGY_INSERT_UPDATE, "Insert and Update"),
        ),
        required=True,
    )

class DeletableUploadSerializer(UploadSerializer):
    strategy = serializers.ChoiceField(
        choices=(
            (STRATEGY_INSERT, "Insert only"),
            (STRATEGY_UPDATE, "Update only"),
            (STRATEGY_INSERT_UPDATE, "Insert and Update"),
            (STRATEGY_INSERT_UPDATE_DELETE, "Insert, Update & Delete"),
        ),
        required=True,
    )


def format_health_facility(health_facility):
    return {
        "district_code": health_facility.location.code,
        "district_name": health_facility.location.name,
        "code": health_facility.code,
        "name": health_facility.name,
        "legal_form": health_facility.legal_form.code
        if health_facility.legal_form
        else None,
        "fax": health_facility.fax,
        "address": health_facility.address,
        "email": health_facility.email,
        "phone": health_facility.phone,
        "account_code": health_facility.acc_code,
        "level": health_facility.level,
        "sub_level": health_facility.sub_level.code
        if health_facility.sub_level
        else None,
        "items_pricelist_name": health_facility.items_pricelist.name
        if health_facility.items_pricelist
        else None,
        "services_pricelist_name": health_facility.services_pricelist.name
        if health_facility.services_pricelist
        else None,
        "care_type": health_facility.care_type,
    }


class CustomXMLRenderer(XMLRenderer):
    item_tag_name = None

    def _capitalize_key(self, key):
        return "".join(x.capitalize() or "_" for x in key.split("_"))

    def _to_xml(self, xml, data):
        if isinstance(data, (list, tuple)):
            for item in data:
                if self.item_tag_name:
                    xml.startElement(self.item_tag_name, {})
                self._to_xml(xml, item)
                if self.item_tag_name:
                    xml.endElement(self.item_tag_name)
        elif isinstance(data, dict):
            for key, value in data.items():
                xml.startElement(self._capitalize_key(key), {})
                self._to_xml(xml, value)
                xml.endElement(self._capitalize_key(key))
        else:
            super()._to_xml(xml, data)


class LocationsXMLRenderer(CustomXMLRenderer):
    root_tag_name = "Locations"


class HealthFacilitiesXMLRenderer(CustomXMLRenderer):
    root_tag_name = "HealthFacilities"
    item_tag_name = "HealthFacility"


class DiagnosesXMLRenderer(CustomXMLRenderer):
    root_tag_name = "Diagnoses"
    item_tag_name = "Diagnosis"


class ItemsXMLRenderer(CustomXMLRenderer):
    root_tag_name = "Items"
    item_tag_name = "Item"


class ServicesXMLRenderer(CustomXMLRenderer):
    root_tag_name = "Services"
    item_tag_name = "Service"


def format_location(location):
    if location.type == "R":
        return {"region": {"region_code": location.code, "region_name": location.name}}
    elif location.type == "D":
        return {
            "district": {
                "region_code": location.parent.code,
                "district_code": location.code,
                "district_name": location.name,
            }
        }
    elif location.type == LOCATION_DEFAULT_CFG['location_types'][2]:
        return {
            "municipality": {
                "district_code": location.parent.code,
                "municipality_code": location.code,
                "municipality_name": location.name,
            }
        }
    elif location.type == "V":
        return {
            "village": {
                "municipality_code": location.parent.code,
                "village_code": location.code,
                "village_name": location.name,
                "male_population": location.male_population or 0,
                "female_population": location.female_population or 0,
                "other_population": location.other_population or 0,
                "families": location.families or 0,
            }
        }


def format_diagnosis(diagnosis):
    return {"diagnosis_code": diagnosis.code, "diagnosis_name": diagnosis.name}


def format_items(item):
    """
    Formats a medical.Item object for an XML export.
    This function lists and formats all the medical.Item fields that will be exported.
    """
    pat_cat = item.patient_category
    adult_cat = pat_cat & PATIENT_CATEGORY_MASK_ADULT
    minor_cat = pat_cat & PATIENT_CATEGORY_MASK_MINOR
    male_cat = pat_cat & PATIENT_CATEGORY_MASK_MALE
    female_cat = pat_cat & PATIENT_CATEGORY_MASK_FEMALE
    return {
        "item_code": item.code,
        "item_name": item.name,
        "item_type": item.type,
        "item_price": item.price,
        "item_care_type": item.care_type,
        "item_male_category": male_cat,
        "item_female_category": female_cat if not female_cat else 1,
        "item_adult_category": adult_cat if not adult_cat else 1,
        "item_minor_category": minor_cat if not minor_cat else 1,
        "item_package": item.package,
        "item_quantity": item.quantity,
        "item_frequency ": item.frequency,
    }


def format_services(service):
    """
    Formats a medical.Service object for an XML export.
    This function lists and formats all the medical.Service fields that will be exported.
    """
    pat_cat = service.patient_category
    adult_cat = pat_cat & PATIENT_CATEGORY_MASK_ADULT
    minor_cat = pat_cat & PATIENT_CATEGORY_MASK_MINOR
    male_cat = pat_cat & PATIENT_CATEGORY_MASK_MALE
    female_cat = pat_cat & PATIENT_CATEGORY_MASK_FEMALE
    return {
        "service_code": service.code,
        "service_name": service.name,
        "service_type": service.type,
        "service_level": service.level,
        "service_price": service.price,
        "service_care_type": service.care_type,
        "service_male_category": male_cat,
        "service_female_category": female_cat if not female_cat else 1,
        "service_adult_category": adult_cat if not adult_cat else 1,
        "service_minor_category": minor_cat if not minor_cat else 1,
        "service_category": service.category,
        "service_frequency": service.frequency,
    }
