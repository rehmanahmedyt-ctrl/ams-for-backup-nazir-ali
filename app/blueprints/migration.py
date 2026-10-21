"""Old Data Migration — analyze on a clone, then append atomically."""
from __future__ import annotations

import json

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from app.services.old_data_merge import OldDataMergeError
from app.services.old_data_workflow import (
    MAX_UPLOAD_BYTES,
    analyze_file_storage,
    apply_manifest,
    list_manifests,
    load_manifest,
)

# Keep the internal blueprint name for endpoint compatibility with existing
# bookmarks/templates. The public product name and canonical URL are new.
bp = Blueprint("legacy_migration", __name__)


def allowed() -> bool:
    return (
        getattr(current_user, "role", None) in {"admin", "root"}
        or bool(getattr(current_user, "can_import_export", False))
    )


def guard():
    if not allowed():
        abort(403)


def _actor() -> str:
    return str(getattr(current_user, "username", None) or "admin")


@bp.route("/old-data-migration/", methods=["GET"])
@login_required
def dashboard():
    guard()
    return render_template("old_data_migration.html", runs=list_manifests())


@bp.route("/old-data-migration/analyze", methods=["POST"])
@login_required
def analyze():
    guard()
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES + 5 * 1024 * 1024:
        flash("Upload refused: the old database is larger than the 1 GiB limit.", "danger")
        return redirect(url_for("legacy_migration.dashboard"))
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose an old AMS .amsdb or .db file.", "danger")
        return redirect(url_for("legacy_migration.dashboard"))
    try:
        manifest = analyze_file_storage(upload, _actor())
    except OldDataMergeError as exc:
        flash(f"Old data analysis refused: {exc}", "danger")
        return redirect(url_for("legacy_migration.dashboard"))
    except Exception as exc:
        flash(f"Old data analysis failed safely; no current rows changed: {exc}", "danger")
        return redirect(url_for("legacy_migration.dashboard"))
    flash(
        "Safety analysis passed on a separate database copy. Review the report "
        "before appending.",
        "success",
    )
    return redirect(url_for("legacy_migration.preview", token=manifest["token"]))


@bp.route("/old-data-migration/run/<token>", methods=["GET"])
@login_required
def preview(token):
    guard()
    try:
        manifest = load_manifest(token)
    except OldDataMergeError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("legacy_migration.dashboard"))
    return render_template("old_data_migration_preview.html", manifest=manifest)


@bp.route("/old-data-migration/run/<token>/apply", methods=["POST"])
@login_required
def apply(token):
    guard()
    if request.form.get("confirm_append") != "yes":
        flash("Confirm that you reviewed the verified append report.", "warning")
        return redirect(url_for("legacy_migration.preview", token=token))
    try:
        manifest = apply_manifest(token, _actor())
    except OldDataMergeError as exc:
        flash(f"Import refused safely: {exc}", "danger")
        return redirect(url_for("legacy_migration.preview", token=token))
    except Exception as exc:
        flash(
            f"Import failed and was rolled back; current business rows remain unchanged: {exc}",
            "danger",
        )
        return redirect(url_for("legacy_migration.preview", token=token))
    report = manifest.get("report") or {}
    flash(
        f"Old data appended safely: {report.get('rows_inserted_total', 0)} records "
        f"created, {report.get('rows_already_imported', 0)} already imported, "
        "0 existing business rows modified or deleted.",
        "success",
    )
    return redirect(url_for("legacy_migration.preview", token=token))


@bp.route("/old-data-migration/run/<token>/report.json", methods=["GET"])
@login_required
def report(token):
    guard()
    try:
        manifest = load_manifest(token)
    except OldDataMergeError as exc:
        return str(exc), 404
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    from io import BytesIO

    return send_file(
        BytesIO(payload),
        as_attachment=True,
        download_name=f"old_data_migration_{token}.json",
        mimetype="application/json",
        max_age=0,
    )


# Backward-compatible public route. No old write endpoint is retained: stale
# template-import POSTs are redirected to the new fail-closed workflow.
@bp.route("/legacy-migration/", methods=["GET"])
@login_required
def legacy_dashboard_redirect():
    guard()
    return redirect(url_for("legacy_migration.dashboard"), code=301)


@bp.route("/legacy-migration/<path:unused>", methods=["GET", "POST"])
@login_required
def legacy_route_redirect(unused):
    guard()
    flash("Legacy Data Migration was replaced by the safer Old Data Migration.", "info")
    return redirect(url_for("legacy_migration.dashboard"), code=303)
