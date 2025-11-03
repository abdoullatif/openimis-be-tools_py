from django.views.decorators.csrf import csrf_exempt

from . import views
from django.urls import path


urlpatterns = [
    path("registers/download_locations", views.download_locations),
    path("registers/upload_locations", views.upload_locations),
    path("registers/download_healthfacilities", views.download_health_facilities),
    path("registers/upload_healthfacilities", views.upload_health_facilities),
    path("registers/download_diagnoses", views.download_diagnoses),
    path("registers/user_template", views.download_user_template),
    path("imports/user_import", views.upload_users),
    path("registers/upload_diagnoses", views.upload_diagnoses),
    path("registers/download_items", views.download_items),
    path("registers/upload_items", views.upload_items),
    path("registers/download_services", views.download_services),
    path("registers/upload_services", views.upload_services),
    path("exports/items", views.export_items),
    path("imports/items", views.import_items),
    path("exports/services", views.export_services),
    path("imports/services", views.import_services),
    path("extracts/download_master_data", csrf_exempt(views.download_master_data)),
    path("extracts/download_phone_extract", views.download_phone_extract),
    path("extracts/download_renewals", csrf_exempt(views.download_renewals)),
    path("extracts/download_feedbacks", csrf_exempt(views.download_feedbacks)),
    path("extracts/upload_claims", csrf_exempt(views.upload_claims)),
    path("extracts/upload_enrollments", csrf_exempt(views.upload_enrollments)),
    path("extracts/upload_renewals", csrf_exempt(views.upload_renewals)),
    path("extracts/upload_feedbacks", csrf_exempt(views.upload_feedbacks)),
]
