import csv
import io
import logging
from django.db import transaction
from django.core.exceptions import ValidationError
from django.apps import apps
from core.models import InteractiveUser
from location.models import Location
from core.apps import CoreConfig
from django.utils.translation import gettext as _
from core.services.userServices import (
    create_or_update_interactive_user,
    create_or_update_core_user,
)

logger = logging.getLogger(__file__)


class UserImportService:
    """
    Service d’importation d’utilisateurs depuis un fichier CSV.
    Gère les stratégies : INSERT, UPDATE, INSERT_UPDATE, INSERT_UPDATE_DELETE.
    """

    REQUIRED_FIELDS = ["username", "email", "first_name", "last_name"]

    @classmethod
    def import_users(cls, file, delimiter=",", dry_run=False, user=None, strategy="INSERT_UPDATE"):
        """
        Colonnes CSV :
        username,email,first_name,last_name,password,phone_number,language,roles,regions,districts
        """
        report = {"sent": 0, "created": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": []}

        # Lecture sécurisée du fichier
        decoded_file = file.read().decode("utf-8-sig").strip()
        reader = csv.DictReader(io.StringIO(decoded_file), delimiter=delimiter)

        # Vérifie les permissions de création
        if user and not user.has_perms(CoreConfig.gql_mutation_create_users_perms):
            raise ValidationError(_("INCORRECT_CREDENTIALS"))

        Role = apps.get_model("core", "Role")
        UserDistrict = apps.get_model("location", "UserDistrict")

        try:
            with transaction.atomic():
                usernames_in_file = set()

                for line_no, row in enumerate(reader, start=2):
                    report["sent"] += 1
                    try:
                        # Validation des champs obligatoires
                        missing = [f for f in cls.REQUIRED_FIELDS if not row.get(f)]
                        if missing:
                            raise ValidationError(
                                _("Colonnes manquantes : ") + ", ".join(missing)
                            )

                        username = row["username"].strip()
                        email = row["email"].strip()
                        code = (row.get("code") or "").strip()
                        first_name = row["first_name"].strip()
                        last_name = row["last_name"].strip()
                        phone = (row.get("phone_number") or "").strip()
                        language = (row.get("language") or "fr").lower().strip()
                        password = (row.get("password") or "").strip() or None
                        usernames_in_file.add(username)

                        existing_user = InteractiveUser.objects.filter(
                            login_name=username, validity_to__isnull=True
                        ).first()

                        # Application de la stratégie
                        if strategy == "INSERT" and existing_user:
                            report["skipped"] += 1
                            continue
                        if strategy == "UPDATE" and not existing_user:
                            report["skipped"] += 1
                            continue

                        data = {
                            "username": username,
                            "login_name": username,
                            "email": email,
                            "other_names": first_name,
                            "last_name": last_name,
                            "phone": phone,
                            "code": code,
                            "language": language,
                            "password": password,
                        }

                        # Rôles
                        roles_raw = row.get("roles", "")
                        role_names = [r.strip() for r in roles_raw.split(";") if r.strip()]
                        role_ids = list(
                            Role.objects.filter(name__in=role_names).values_list("id", flat=True)
                        )
                        data["roles"] = role_ids

                        # Districts
                        district_names = [
                            d.strip()
                            for d in (row.get("districts") or "").split(";")
                            if d.strip()
                        ]
                        district_ids = list(
                            Location.objects.filter(name__in=district_names, type="D")
                            .values_list("id", flat=True)
                        )
                        data["districts"] = district_ids

                        # Municipalities
                        municipality_names = [
                            d.strip()
                            for d in (row.get("municipalities") or "").split(";")
                            if d.strip()
                        ]
                        municipality_ids = list(
                            Location.objects.filter(name__in=municipality_names, type="W")
                            .values_list("id", flat=True)
                        )
                        data["municipalities"] = municipality_ids


                        # Régions (optionnel)
                        region_names = [
                            r.strip()
                            for r in (row.get("regions") or "").split(";")
                            if r.strip()
                        ]
                        _ = list(
                            Location.objects.filter(name__in=region_names, type="R")
                            .values_list("id", flat=True)
                        )

                        # Création / mise à jour
                        i_user_obj, created = create_or_update_interactive_user(
                            user_id=None,
                            data=data,
                            audit_user_id=(getattr(user, "id_for_audit", 1) or 1),
                            connected=True,
                        )

                        create_or_update_core_user(
                            user_uuid=None,
                            username=username,
                            i_user=i_user_obj,
                        )

                        if created:
                            report["created"] += 1
                        else:
                            report["updated"] += 1

                    except ValidationError as e:
                        logger.warning(f"Ligne {line_no}: {e}")
                        report["errors"].append(f"Ligne {line_no}: {', '.join(e.messages)}")
                    except Exception as e:
                        logger.exception(f"Erreur ligne {line_no}: {e}")
                        report["errors"].append(f"Ligne {line_no}: {str(e)}")

                # DELETE strategy
                if strategy == "INSERT_UPDATE_DELETE":
                    users_to_delete = InteractiveUser.objects.filter(
                        validity_to__isnull=True
                    ).exclude(login_name__in=usernames_in_file)
                    count_deleted = users_to_delete.count()
                    for u in users_to_delete:
                        u.delete_history()
                    report["deleted"] = count_deleted
                    logger.info(f"{count_deleted} utilisateurs supprimés (stratégie DELETE)")

                # Dry-run
                if dry_run:
                    transaction.set_rollback(True)
                    logger.info("Transaction annulée (dry_run=True)")

        except Exception as e:
            logger.exception(f"Erreur globale import utilisateurs: {e}")
            report["errors"].append(str(e))

        report["success"] = len(report["errors"]) == 0
        return report
