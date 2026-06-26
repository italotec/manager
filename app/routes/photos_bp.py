"""Blueprint: Photos — saved profile-picture library + bulk apply to WABA phone numbers."""
from __future__ import annotations

import io
import os
import uuid

from flask import Blueprint, jsonify, request, send_file
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ..models import PhotoModel, db
from ..json_store import photos_dir

bp = Blueprint("photos", __name__, url_prefix="/photos")

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB input cap (Pillow will shrink the output)


def _process_image(data: bytes) -> bytes:
    """Center-crop to square, resize to 640×640, return JPEG bytes."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # honour EXIF rotation
    img = img.convert("RGB")

    # Center-square crop
    w, h = img.size
    side = min(w, h)
    left   = (w - side) // 2
    top    = (h - side) // 2
    img    = img.crop((left, top, left + side, top + side))

    img = img.resize((640, 640), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


@bp.route("/list")
@login_required
def list_photos():
    photos = (
        PhotoModel.query
        .filter_by(user_id=current_user.id)
        .order_by(PhotoModel.created_at.desc())
        .all()
    )
    return jsonify({"photos": [p.to_dict() for p in photos]})


@bp.route("/upload", methods=["POST"])
@login_required
def upload_photo():
    file = request.files.get("photo")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Nenhum arquivo enviado."}), 400

    mime = file.mimetype or ""
    if mime not in _ALLOWED_MIME:
        return jsonify({"ok": False, "error": f"Tipo de arquivo não suportado: {mime}"}), 400

    raw = file.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        return jsonify({"ok": False, "error": "Arquivo muito grande (máx. 10 MB)."}), 400

    try:
        jpeg_bytes = _process_image(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erro ao processar imagem: {e}"}), 400

    name = request.form.get("name", "").strip()
    if not name:
        original = secure_filename(file.filename or "foto")
        name = os.path.splitext(original)[0] or "foto"

    filename = f"{uuid.uuid4().hex}.jpg"
    dest = os.path.join(photos_dir(current_user.id), filename)
    with open(dest, "wb") as f:
        f.write(jpeg_bytes)

    photo = PhotoModel(user_id=current_user.id, name=name, filename=filename)
    db.session.add(photo)
    db.session.commit()

    return jsonify({"ok": True, "photo": photo.to_dict()})


@bp.route("/<int:photo_id>/file")
@login_required
def serve_photo(photo_id: int):
    photo = PhotoModel.query.filter_by(id=photo_id, user_id=current_user.id).first_or_404()
    path = os.path.join(photos_dir(current_user.id), photo.filename)
    if not os.path.exists(path):
        return jsonify({"error": "Arquivo não encontrado"}), 404
    return send_file(path, mimetype="image/jpeg")


@bp.route("/delete", methods=["POST"])
@login_required
def delete_photos():
    data = request.get_json(silent=True) or {}
    ids = data.get("photo_ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "Nenhuma foto selecionada."}), 400

    photos = PhotoModel.query.filter(
        PhotoModel.user_id == current_user.id,
        PhotoModel.id.in_(ids)
    ).all()

    pdir = photos_dir(current_user.id)
    for p in photos:
        path = os.path.join(pdir, p.filename)
        try:
            os.remove(path)
        except OSError:
            pass
        db.session.delete(p)

    db.session.commit()
    return jsonify({"ok": True, "deleted": len(photos)})


@bp.route("/bulk-apply-start", methods=["POST"])
@login_required
def bulk_apply_start():
    from ..services.photo_service import start_photo_job

    data = request.get_json(silent=True) or {}
    photo_id = data.get("photo_id")
    waba_ids = data.get("waba_ids") or []

    if not photo_id:
        return jsonify({"ok": False, "error": "Selecione uma foto."}), 400
    if not isinstance(waba_ids, list) or not waba_ids:
        return jsonify({"ok": False, "error": "Selecione pelo menos 1 WABA."}), 400

    photo = PhotoModel.query.filter_by(id=photo_id, user_id=current_user.id).first()
    if not photo:
        return jsonify({"ok": False, "error": "Foto não encontrada."}), 404

    try:
        job_id = start_photo_job(current_user.id, photo_id, waba_ids)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/job/<int:job_id>")
@login_required
def job_status(job_id: int):
    from ..services.photo_service import get_job
    state = get_job(job_id)
    if state is None:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(state)


@bp.route("/job/<int:job_id>/stop", methods=["POST"])
@login_required
def stop_job(job_id: int):
    from ..services.photo_service import request_stop
    request_stop(job_id)
    return jsonify({"ok": True})
