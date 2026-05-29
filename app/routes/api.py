from flask import Blueprint, request, jsonify
from ..models import User
from ..json_store import ensure_user_bms_file, upsert_waba
from ..services.meta import subscribe_waba_webhook
from ..config import Config

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _authenticate():
    """Extract and validate the API key from request headers.

    Returns (user, None) on success or (None, response, status_code) on failure.
    Accepts: Authorization: Bearer <key>  OR  X-API-Key: <key>
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        key = auth_header[7:].strip()
    else:
        key = request.headers.get("X-API-Key", "").strip()

    if not key:
        return None, jsonify({"ok": False, "error": "Missing API key. Send Authorization: Bearer <key> or X-API-Key: <key>."}), 401

    user = User.query.filter_by(api_key=key).first()
    if not user:
        return None, jsonify({"ok": False, "error": "Invalid API key."}), 401

    if user.is_banned:
        return None, jsonify({"ok": False, "error": "Account banned."}), 403

    return user, None


@bp.route("/business-managers", methods=["POST"])
def add_business_manager():
    """Register a Business Manager (WABA) for the authenticated user.

    Body (JSON): { "waba_id": "...", "token": "..." }
    Returns 201 on success.
    """
    user, err = _authenticate()
    if err:
        return err

    body = request.get_json(silent=True)
    if not body:
        return jsonify({"ok": False, "error": "Request body must be valid JSON with Content-Type: application/json."}), 400

    waba_id = str(body.get("waba_id") or "").strip()
    token = str(body.get("token") or "").strip()

    if not waba_id:
        return jsonify({"ok": False, "error": "waba_id is required."}), 400
    if not token:
        return jsonify({"ok": False, "error": "token is required."}), 400

    ensure_user_bms_file(user.id)
    upsert_waba(user.id, waba_id=waba_id, token=token)

    webhook_ok, webhook_err = subscribe_waba_webhook(Config.META_API_VERSION, token, waba_id)

    return jsonify({
        "ok": True,
        "waba_id": waba_id,
        "webhook_subscribed": webhook_ok,
        "webhook_error": webhook_err,
    }), 201
