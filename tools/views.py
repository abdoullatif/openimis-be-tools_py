import io
import logging
import os
import tempfile
import xml.etree.ElementTree as ET

from core.models import Officer
from core.security import checkUserWithRights
from core.utils import filter_validity
from django.core.exceptions import PermissionDenied
from django.db.models.query_utils import Q
from django.http import HttpResponse
from django.http.response import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from import_export.resources import ModelResource
from tablib import Dataset

from location.models import HealthFacility, Location
from medical.models import Diagnosis, Item, Service
from location.apps import DEFAULT_CFG as LOCATION_DEFAULT_CFG
from rest_framework.decorators import api_view, permission_classes, renderer_classes

from rest_framework.response import Response
from . import serializers, services, utils
from .apps import ToolsConfig
from .resources import ItemResource, ServiceResource
from .services import return_upload_result_json
from .user_import_service import UserImportService

logger = logging.getLogger(__name__)


import base64
from .serializers import FileSerializer


@api_view(["GET"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_locations_perms,
        )
    ]
)
@renderer_classes([serializers.LocationsXMLRenderer])
def download_locations(request):
    data = {"Regions": [], "Districts": [], "Municipalities": [], "Villages": []}

    for region in Location.objects.filter(~Q(code="FR"), type="R", *filter_validity()):
        data["Regions"].append(serializers.format_location(region))
        children = Location.objects.children(region.id)
        for child in children:
            if child.type == "D":
                data["Districts"].append(serializers.format_location(child))
            elif child.type == LOCATION_DEFAULT_CFG['location_types'][2]:
                data["Municipalities"].append(serializers.format_location(child))
            elif child.type == "V":
                data["Villages"].append(serializers.format_location(child))

    return Response(
        data,
        content_type="text/xml",
        headers={"Content-Disposition": "attachment; filename=locations.xml"},
    )


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_locations_perms,
        )
    ]
)
def upload_locations(request):
    serializer = serializers.UploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file = serializer.validated_data.get("file")
    dry_run = serializer.validated_data.get("dry_run")
    strategy = serializer.validated_data.get("strategy")

    try:
        logger.info(f"Uploading locations (dry_run={dry_run}, strategy={strategy})...")
        xml = utils.sanitize_xml(file)
        result = services.upload_locations(
            request.user, xml=xml, strategy=strategy, dry_run=dry_run
        )
        logger.info(f"Locations upload completed: {result}")
        return return_upload_result_json(xml_result=result)
    except ET.ParseError as exc:
        logger.error(exc)
        return Response(
            {
                "success": False,
                "error": "Malformed XML",
            }
        )


@api_view(["GET"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_health_facilities_perms,
        )
    ]
)
@renderer_classes([serializers.HealthFacilitiesXMLRenderer])
def download_health_facilities(request):
    queryset = HealthFacility.objects.filter(*filter_validity())
    data = {
        "health_facility_details": [
            serializers.format_health_facility(hf) for hf in queryset
        ]
    }
    return Response(
        data=data,
        content_type="text/xml",
        headers={"Content-Disposition": "attachment; filename=health_facilities.xml"},
    )


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_health_facilities_perms,
        )
    ]
)
def upload_health_facilities(request):
    serializer = serializers.UploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file = serializer.validated_data.get("file")
    dry_run = serializer.validated_data.get("dry_run")
    strategy = serializer.validated_data.get("strategy")

    try:
        logger.info(
            f"Uploading health facilities (dry_run={dry_run}, strategy={strategy})..."
        )
        xml = utils.sanitize_xml(file)
        result = services.upload_health_facilities(
            request.user, xml=xml, strategy=strategy, dry_run=dry_run
        )
        logger.info(f"Health facilities upload completed: {result}")
        return return_upload_result_json(xml_result=result)
    except ET.ParseError as exc:
        logger.error(exc)
        return Response(
            {
                "success": False,
                "error": "Malformed XML",
            }
        )
    except Exception as exc:
        logger.error(exc)
        return Response(
            {
                "success": False,
                "error": str(exc),
            }
        )



@api_view(["GET"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_diagnoses_perms,
        )
    ]
)
@renderer_classes([serializers.DiagnosesXMLRenderer])
def download_diagnoses(request):
    queryset = Diagnosis.objects.filter(*filter_validity())
    data = [serializers.format_diagnosis(hf) for hf in queryset]
    return Response(
        data=data,
        headers={"Content-Disposition": "attachment; filename=diagnoses.xml"},
    )


@api_view(["GET"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_items_perms,
        )
    ]
)
@renderer_classes([serializers.ItemsXMLRenderer])
def download_items(request):
    """Downloads all medical.Item objects in an XML file.

    The list of exported fields can be modified in serializers.format_items().

    Calling this function is restricted to some users, see ToolsConfig.

    Parameters
    ----------
    request : rest_framework.request.Request
        The request that is asking to download items.

    Returns
    ----------
    rest_framework.response.Response
        The requested data in an XML file.

    """
    queryset = Item.objects.filter(*filter_validity())
    data = [serializers.format_items(item) for item in queryset]
    return Response(
        data=data,
        headers={"Content-Disposition": "attachment; filename=items.xml"},
    )


@api_view(["GET"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_services_perms,
        )
    ]
)
@renderer_classes([serializers.ServicesXMLRenderer])
def download_services(request):
    queryset = Service.objects.filter(*filter_validity())
    data = [serializers.format_services(service) for service in queryset]
    return Response(
        data=data,
        headers={"Content-Disposition": "attachment; filename=services.xml"},
    )


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_diagnoses_perms,
        )
    ]
)
def upload_diagnoses(request):
    serializer = serializers.DeletableUploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file = serializer.validated_data.get("file")
    dry_run = serializer.validated_data.get("dry_run")
    strategy = serializer.validated_data.get("strategy")

    try:
        logger.info(f"Uploading diagnoses (dry_run={dry_run}, strategy={strategy})...")
        xml = utils.sanitize_xml(file)
        result = services.upload_diagnoses(
            request.user, xml=xml, strategy=strategy, dry_run=dry_run
        )
        logger.info(f"Diagnoses upload completed: {result}")
        return return_upload_result_json(xml_result=result)
    except ET.ParseError as exc:
        logger.error(exc)
        return Response(
            {
                "success": False,
                "error": "Malformed XML",
            }
        )


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_items_perms,
        )
    ]
)
def upload_items(request):
    """Uploads an XML file containing medical.Item entries.

    Calling this function is restricted to some users, see ToolsConfig.

    Parameters
    ----------
    request : rest_framework.request.Request
        The request containing all data.

    Returns
    ------
    JsonResponse
        Represents the number of entries received, with the number of created/updated/deleted
        medical.Item objects, as well as the list of errors that have occurred while each entry was processed.

    Raises
    ------
    ET.ParseError
        If the structure of the XML file is invalid.
    """
    serializer = serializers.DeletableUploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file = serializer.validated_data.get("file")
    dry_run = serializer.validated_data.get("dry_run")
    strategy = serializer.validated_data.get("strategy")

    try:
        logger.info("Uploading medical items (dry_run=%s, strategy=%s)...", dry_run, strategy)
        xml = utils.sanitize_xml(file)
        result = services.upload_items(
            request.user, xml=xml, strategy=strategy, dry_run=dry_run
        )
        logger.info("Medical items upload completed: %s", result)
        return return_upload_result_json(xml_result=result)
    except ET.ParseError as exc:
        logger.error(exc)
        return Response(
            {
                "success": False,
                "error": "Malformed XML",
            }
        )


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_services_perms,
        )
    ]
)
def upload_services(request):
    """Uploads an XML file containing medical.Service entries.

    Calling this function is restricted to some users, see ToolsConfig.

    Parameters
    ----------
    request : rest_framework.request.Request
        The request containing all data.

    Returns
    ------
    JsonResponse
        Represents the number of entries received, with the number of created/updated/deleted
        medical.Service objects, as well as the list of errors that have occurred while each entry was processed.

    Raises
    ------
    ET.ParseError
        If the structure of the XML file is invalid.
    """
    serializer = serializers.DeletableUploadSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file = serializer.validated_data.get("file")
    dry_run = serializer.validated_data.get("dry_run")
    strategy = serializer.validated_data.get("strategy")

    try:
        logger.info("Uploading medical services (dry_run=%s, strategy=%s)...", dry_run, strategy)
        xml = utils.sanitize_xml(file)
        result = services.upload_services(
            request.user, xml=xml, strategy=strategy, dry_run=dry_run
        )
        logger.info("Medical services upload completed: %s", result)
        return return_upload_result_json(xml_result=result)
    except ET.ParseError as exc:
        logger.error(exc)
        return Response(
            {
                "success": False,
                "error": "Malformed XML",
            }
        )


@api_view(["GET"])
def download_master_data(request):
    if not request.user.has_perms(ToolsConfig.extracts_master_data_perms):
        raise PermissionDenied(_("unauthorized"))

    export_file = services.create_master_data_export(request.user)

    response = FileResponse(
        open(export_file.name, "rb"),
        as_attachment=True,
        filename=os.path.basename(export_file.name),
        content_type="application/zip",
    )
    return response


@api_view(["GET"])
def download_phone_extract(request):
    if not request.user.has_perms(ToolsConfig.extracts_phone_extract_perms):
        raise PermissionDenied(_("unauthorized"))

    location_id = request.GET.get("location")
    with_insuree = request.GET.get("with_insuree", False)

    if not location_id:
        return Response(status=400, data={"error": "Location must be provided."})

    extract = services.create_phone_extract(request.user, location_id, with_insuree)

    return FileResponse(
        extract.stored_file,
        as_attachment=True,
        filename=os.path.basename(extract.stored_file.name),
    )


@api_view(["GET"])
def download_feedbacks(request):
    if not request.user.has_perms(ToolsConfig.extracts_officer_feedbacks_perms):
        raise PermissionDenied(_("unauthorized"))

    officer_id = request.GET.get("officer_id")
    officer = get_object_or_404(Officer, id=officer_id, *filter_validity())

    export_file = services.create_officer_feedbacks_export(request.user, officer)

    response = FileResponse(
        open(export_file.name, "rb"),
        as_attachment=True,
        filename=os.path.basename(export_file.name),
        content_type="application/zip",
    )

    return response

@api_view(["GET"])
def download_renewals(request):
    if not request.user.has_perms(ToolsConfig.extracts_officer_renewals_perms):
        raise PermissionDenied(_("unauthorized"))

    officer_id = request.GET.get("officer_id")
    officer = get_object_or_404(Officer, id=officer_id, *filter_validity())

    export_file = services.create_officer_renewals_export(request.user, officer)
    response = FileResponse(
        open(export_file.name, "rb"),
        as_attachment=True,
        filename=os.path.basename(export_file.name),
        content_type="application/zip",
    )

    return response


@require_http_methods(["POST"])
def upload_claims(request):
    if not request.user.has_perms(ToolsConfig.extracts_upload_claims_perms):
        raise PermissionDenied(_("unauthorized"))

    if not request.FILES:
        return JsonResponse({"error": "No file provided"}, status=400)

    errors = []
    for filename in request.FILES:
        try:
            logger.info(f"Processing claim in {filename}")
            xml = utils.sanitize_xml(request.FILES[filename])
            services.upload_claim(request.user, xml)
        except (utils.ParseError, services.InvalidXMLError) as exc:
            logger.exception(exc)
            errors.append(f"File '{filename}' is not a valid XML")
            continue
        except Exception as exc:
            logger.exception(exc)
            errors.append("An unknown error occurred.")
            continue

    return JsonResponse({"success": len(errors) == 0, "errors": errors})


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_locations_perms,
        )
    ]
)
# @require_http_methods(["POST"])
def upload_enrollments(request):
    if not request.user.has_perms(ToolsConfig.extracts_upload_claims_perms):  # TODO enrollment perms
        raise PermissionDenied(_("unauthorized"))

    return _process_upload(request, services.upload_enrollments)


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_locations_perms,
        )
    ]
)
# @require_http_methods(["POST"])
def upload_renewals(request):
    if not request.user.has_perms(ToolsConfig.extracts_upload_claims_perms):  # TODO enrollment perms
        raise PermissionDenied(_("unauthorized"))

    return _process_upload(request, services.upload_renewals)


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_locations_perms,
        )
    ]
)
# @require_http_methods(["POST"])
def upload_feedbacks(request):
    if not request.user.has_perms(ToolsConfig.extracts_upload_claims_perms):  # TODO enrollment perms
        raise PermissionDenied(_("unauthorized"))

    return _process_upload(request, services.upload_feedbacks)


def _process_upload(request, process_method):
    if not request.FILES:
        return JsonResponse({"error": "No file provided"}, status=400)

    errors = []
    for filename, uploaded_file in request.FILES.items():
        try:
            # The file is often in memory, the ZIP needs it as a real file path
            with tempfile.NamedTemporaryFile(mode='wb+', suffix=".zip") as tmp_file:
                logger.info(f"Processing renewals in {str(uploaded_file)}")
                for chunk in uploaded_file.chunks():
                    tmp_file.write(chunk)
                tmp_file.seek(0)
                process_method(tmp_file.name, request.user)
        except (utils.ParseError, services.InvalidXMLError) as exc:
            logger.exception(exc)
            errors.append(f"File '{filename}' is not a valid XML")
            continue
        except Exception as exc:
            logger.exception(exc)
            errors.append("An unknown error occurred.")
            return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse({"success": len(errors) == 0, "errors": errors})


# List of supported import/export formats so far
XLS = "xls"
XLSX = "xlsx"
CSV = "csv"
JSON = "json"
SUPPORTED_FORMATS = [XLS, XLSX, CSV, JSON]

# other types: https://stackoverflow.com/a/50860387
CONTENT_TYPES = {
    XLS: "application/vnd.ms-excel",
    CSV: "text/csv",
    JSON: "application/json",
    XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@api_view(["GET"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_items_perms,
        )
    ]
)
def export_items(request):
    export_format = request.GET.get("file_format", "unknown")
    if export_format in SUPPORTED_FORMATS:
        return process_export_items(request.user, export_format)
    else:
        return JsonResponse({"error": "Unknown export format."}, status=400)


def process_export_items(user, data_type):
    logger.info("User (audit id %s) requested export of medical items in %s", user.id_for_audit, data_type)
    item_resource = ItemResource(user=user)
    query_set = Item.objects.filter(*filter_validity()).order_by("code")
    dataset = item_resource.export(query_set)
    datasets = {
        XLS: dataset.xls,
        CSV: dataset.csv,
        JSON: dataset.json,
        XLSX: dataset.xlsx,
    }
    response = HttpResponse(datasets[data_type], content_type=CONTENT_TYPES[data_type])
    response['Content-Disposition'] = f'attachment; filename="items.{data_type}"'
    return response


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_items_perms,
        )
    ]
)
def import_items(request):
    serializer = serializers.FileSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file = serializer.validated_data.get("file")
    logger.info("User (audit id %s) requested import of medical items",request.user.id_for_audit)

    item_resource = ItemResource(user=request.user)
    dataset = Dataset()

    if CONTENT_TYPES[CSV] == file.content_type:
        dataset.load(file.read().decode(), format="csv")  # the CSV file must be read differently, otherwise it crashes
    else:
        dataset.load(file.read())

    return process_import_items_services(item_resource, dataset)


@api_view(["GET"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_services_perms,
        )
    ]
)
def export_services(request):
    export_format = request.GET.get("file_format", "unknown")
    if export_format in SUPPORTED_FORMATS:
        return process_export_services(request.user, export_format)
    else:
        return JsonResponse({"error": "Unknown export format."}, status=400)


def process_export_services(user, data_type):
    logger.info("User (audit id %s) requested export of medical services in %s", user.id_for_audit, data_type)
    service_resource = ServiceResource(user)
    query_set = Service.objects.filter(*filter_validity()).order_by("code")
    dataset = service_resource.export(query_set)
    datasets = {
        XLS: dataset.xls,
        CSV: dataset.csv,
        JSON: dataset.json,
        XLSX: dataset.xlsx,
    }
    response = HttpResponse(datasets[data_type], content_type=CONTENT_TYPES[data_type])
    response['Content-Disposition'] = f'attachment; filename="services.{data_type}"'
    return response


@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_services_perms,
        )
    ]
)
def import_services(request):
    serializer = serializers.FileSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    file = serializer.validated_data.get("file")
    logger.info("User (audit id %s) requested import of medical services", request.user.id_for_audit)

    service_resource = ServiceResource(request.user)
    dataset = Dataset()

    if CONTENT_TYPES[CSV] == file.content_type:
        dataset.load(file.read().decode(), format="csv")  # the CSV file must be read differently, otherwise it crashes
    else:
        dataset.load(file.read())

    return process_import_items_services(service_resource, dataset)


def process_import_items_services(resource: ModelResource, dataset: Dataset):
    result = resource.import_data(dataset, dry_run=True)  # Test the data import

    success = True
    errors = []
    logger.info("Import results: total rows received=%s - detail=%s", result.total_rows, result.totals)

    if not result.has_errors() and not result.has_validation_errors():
        resource.import_data(dataset, dry_run=False)  # Actually import now
    else:
        success = False
        if result.has_validation_errors():
            for invalid_row in result.invalid_rows:
                logger.error("Invalid row n°%s - %s", invalid_row.number, invalid_row.error.message)
                errors.append(f"row ({invalid_row.number}) - {invalid_row.error.message}")
        if result.has_errors():
            for index, row_error in result.row_errors():
                logger.error("Error row n°%s", index)
                for error in row_error:
                    logger.error("Traceback: %s", error.traceback)
                    errors.append(f"row ({index}) - {error.error}")

    logger.info("End of import process")
    return return_upload_result_json(success=success, other_types_result=result, other_types_errors=errors)

def download_user_template(request):
    """
    Retourne un modèle CSV pour l’import d’utilisateurs
    """
    template = (
        "code,username,email,first_name,last_name,password,phone_number,language,roles,regions,districts,municipalities\n"
        "123,admin,admin@example.com,Admin,System,Admin123!,22377000000,fr,ADMIN,Kayes;Mopti,Kayes Cercle;Youwarou,Youwarou\n"
        "234,moussa,moussa@example.com,Moussa,Traore,Moussa@123,22376000000,en,OFFICER,Segou,Segou Cercle,Segou\n"
        "345,fatou,fatou@example.com,Fatou,Diallo,Fatou@123,22378000000,fr,USER,Gao;Kidal,Kidal\n"
    )
    response = HttpResponse(template, content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="user_import_template.csv"'
    return response

@api_view(["POST"])
@permission_classes(
    [
        checkUserWithRights(
            ToolsConfig.registers_locations_perms,
        )
    ]
)
def upload_users(request):
    """
    Upload d'un fichier CSV d'utilisateurs (création/mise à jour interactive)
    avec stratégie d'import (INSERT, UPDATE, INSERT_UPDATE, INSERT_UPDATE_DELETE).
    """
    file = request.FILES.get("file")
    dry_run = request.data.get("dry_run", "false").lower() == "true"
    strategy = request.data.get("strategy", "INSERT_UPDATE").upper()

    if not file:
        return Response({"success": False, "error": "Aucun fichier fourni."}, status=400)

    try:
        logger.info(f"Import utilisateurs démarré (dry_run={dry_run}, strategy={strategy})")

        # Appel du service
        report = UserImportService.import_users(
            file=file,
            dry_run=dry_run,
            delimiter=",",
            user=request.user,
            strategy=strategy,
        )

        logger.info(f"Import utilisateurs terminé : {report}")

        # Conversion pour return_upload_result_json
        class Result:
            total_rows = report.get("sent", 0)
            totals = {
                "new": report.get("created", 0),
                "update": report.get("updated", 0),
                "delete": report.get("deleted", 0),
                "skip": report.get("skipped", 0),
                "invalid": len(report.get("errors", [])),
                "error": len(report.get("errors", [])),
            }

        return return_upload_result_json(
            success=report.get("success", False),
            other_types_result=Result(),
            other_types_errors=report.get("errors", []),
        )

    except Exception as exc:
        logger.exception("Erreur lors de l'import des utilisateurs : %s", exc)
        return Response(
            {"success": False, "error": str(exc), "errors": [str(exc)]},
            status=500,
        )
