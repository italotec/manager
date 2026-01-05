import time
import traceback
from flask import current_app
from .. import db
from ..models import User, BalanceTx, Job
from ..json_store import load_user_bms, save_user_bms
from .sms24h import sms24h_get_number, sms24h_get_status, sms24h_cancel
from .meta import (
    get_waba_name,
    add_phone_number,
    request_code,
    verify_code,
    register_number,
)

COUNTRY_CODE_MAP = {
    "73": "55",
    "6": "62",
}

def _only_digits(s: str) -> str:
    if s is None:
        return ""
    return "".join(c for c in str(s) if c.isdigit())

def normalize_verified_name(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
        parts = [str(x).strip() for x in value if str(x).strip() and str(x).strip().lower() != "none"]
        return (parts[0] if parts else "").strip()
    if isinstance(value, dict):
        for k in ("name", "verified_name", "display_name", "business_name"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    s = str(value).strip()
    if s.startswith("[") and s.endswith("]") and "," in s:
        q1 = s.find('"')
        if q1 != -1:
            q2 = s.find('"', q1 + 1)
            if q2 != -1:
                candidate = s[q1 + 1:q2].strip()
                if candidate:
                    return candidate
    return s

def _job_update(job: Job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    db.session.commit()

def _update_bms_entry(user_id: int, waba_id: str, patch: dict):
    bms = load_user_bms(user_id)
    key = str(waba_id).strip()
    if key not in bms or not isinstance(bms.get(key), dict):
        return False
    entry = bms[key]
    entry.update(patch)
    bms[key] = entry
    save_user_bms(user_id, bms)
    return True

def _append_debug(user_id: int, waba_id: str, msg: str):
    bms = load_user_bms(user_id)
    key = str(waba_id).strip()
    if key not in bms or not isinstance(bms.get(key), dict):
        # If the key doesn't exist yet, nothing to append.
        return
    entry = bms[key]
    debug = entry.get("last_add_phone_debug", [])
    if not isinstance(debug, list):
        debug = []
    debug.append(msg[:1200])
    debug = debug[-30:]  # keep last 30
    entry["last_add_phone_debug"] = debug
    bms[key] = entry
    save_user_bms(user_id, bms)

def _set_error(user_id: int, waba_id: str, msg: str):
    # Always try to write the error if the entry exists
    ok = _update_bms_entry(user_id, waba_id, {"last_add_phone_error": msg[:900]})
    return ok

def _has_balance_for_otp(user_id: int) -> tuple[bool, str]:
    cost = int(current_app.config["OTP_COST_CENTS"])
    u = db.session.get(User, user_id)
    if not u:
        return False, "Usuário não encontrado"
    if u.balance_cents < cost:
        return False, f"Saldo insuficiente. Necessário R$ {cost/100:.2f} para iniciar."
    return True, "OK"

def _debit_otp(user_id: int, waba_id: str, phone_number_id: str):
    cost = int(current_app.config["OTP_COST_CENTS"])
    u = db.session.get(User, user_id)
    if not u:
        return False, "Usuário não encontrado"
    if u.balance_cents < cost:
        return False, "Saldo insuficiente para debitar OTP"

    u.balance_cents -= cost
    tx = BalanceTx(
        user_id=user_id,
        amount_cents=-cost,
        reason=f"OTP recebido (R$ {cost/100:.2f})",
        waba_id=str(waba_id),
        phone_number_id=str(phone_number_id or "")
    )
    db.session.add(tx)
    db.session.commit()
    return True, "OTP debitado"

def process_one_waba_add_phone(user_id: int, waba_id: str, job_id: int) -> bool:
    """
    Returns True if completed OK (or saved phone_id in terms-not-accepted case),
    False on any failure. ALWAYS tries to write last_add_phone_error/debug to bms.json.
    """
    job = db.session.get(Job, job_id)
    if not job:
        return False

    waba_id = str(waba_id).strip()

    try:
        _job_update(job, last_message="Inicializando...")
        # Load bms first (so we can log errors into it)
        bms = load_user_bms(user_id)

        if not bms:
            _job_update(job, last_message="bms.json vazio")
            return False

        data = bms.get(waba_id)
        if not isinstance(data, dict):
            # If the key isn't present, we can't write inside it.
            _job_update(job, last_message=f"WABA {waba_id} não encontrado no bms.json")
            return False

        # reset error/debug every run
        _update_bms_entry(user_id, waba_id, {"last_add_phone_error": "", "last_add_phone_debug": []})
        _append_debug(user_id, waba_id, f"START user_id={user_id} waba_id={waba_id} job_id={job_id}")

        token = (data.get("token") or "").strip()
        if not token:
            _set_error(user_id, waba_id, "Token vazio no bms.json")
            _append_debug(user_id, waba_id, "ABORT token vazio")
            _job_update(job, last_message="Token vazio")
            return False

        # ✅ Pre-check saldo
        ok_bal, msg_bal = _has_balance_for_otp(user_id)
        _append_debug(user_id, waba_id, f"balance_check ok={ok_bal} msg={msg_bal}")
        if not ok_bal:
            _set_error(user_id, waba_id, msg_bal)
            _job_update(job, last_message=msg_bal)
            return False

        existing_phone = str(data.get("phone_number_id") or "").strip()
        if existing_phone:
            _append_debug(user_id, waba_id, f"SKIP existing phone_number_id={existing_phone}")
            _job_update(job, last_message="Já possui phone_number_id. Pulando.")
            return True

        otp_received = bool(data.get("otp_received", False))
        otp_received_at = int(data.get("otp_received_at") or 0)
        lock_hours = int(current_app.config["OTP_LOCK_HOURS"])
        if otp_received and otp_received_at:
            elapsed = time.time() - float(otp_received_at)
            if elapsed < lock_hours * 3600:
                remaining_min = int(((lock_hours * 3600) - elapsed) // 60)
                msg = f"Cooldown OTP ativo ({lock_hours}h). Faltam ~{remaining_min} min."
                _set_error(user_id, waba_id, msg)
                _append_debug(user_id, waba_id, f"ABORT cooldown elapsed={elapsed}")
                _job_update(job, last_message=msg)
                return False
            _update_bms_entry(user_id, waba_id, {
                "pending_phone_number_id": "",
                "sms24h_activation_id": "",
                "sms24h_full_phone": "",
                "otp_received": False,
                "otp_received_at": 0,
            })
            _append_debug(user_id, waba_id, "cooldown expired -> cleared pending fields")

        api_version = current_app.config["META_API_VERSION"]
        country = str(current_app.config["COUNTRY"])
        operator = str(current_app.config["OPERATOR"])
        service = str(current_app.config["SERVICE"])
        code_method = str(current_app.config["CODE_METHOD"])
        language = str(current_app.config["LANGUAGE"])
        max_wait = int(current_app.config["TEMPO_MAX_ESPERA_OTP"])
        max_attempts = int(current_app.config["MAX_TENTATIVAS_POR_WABA"])
        proxies = current_app.config.get("PROXIES_RAW") or []

        cc = COUNTRY_CODE_MAP.get(country)
        if not cc:
            msg = f"COUNTRY {country} sem CC mapeado"
            _set_error(user_id, waba_id, msg)
            _append_debug(user_id, waba_id, f"ABORT {msg}")
            _job_update(job, last_message=msg)
            return False

        # Get verified name
        _job_update(job, last_message="Obtendo nome do WABA (verified_name)...")
        raw = get_waba_name(api_version, token, waba_id)
        if isinstance(raw, tuple) and len(raw) == 2:
            verified_name, err = raw
            _append_debug(user_id, waba_id, f"get_waba_name tuple err={err} name={verified_name}")
            if err:
                msg = f"get_waba_name: {err}"
                _set_error(user_id, waba_id, msg)
                _job_update(job, last_message=msg)
                return False
        else:
            verified_name = raw
            _append_debug(user_id, waba_id, f"get_waba_name raw={verified_name}")

        verified_name = normalize_verified_name(verified_name)
        _append_debug(user_id, waba_id, f"verified_name normalized='{verified_name}'")
        if not verified_name:
            msg = "verified_name vazio"
            _set_error(user_id, waba_id, msg)
            _job_update(job, last_message=msg)
            return False

        # Main attempts
        for attempt in range(1, max_attempts + 1):
            proxy_str = proxies[(attempt - 1) % len(proxies)] if proxies else None
            _job_update(job, last_message=f"Tentativa {attempt}/{max_attempts}: comprando número...")

            activation_id, full_phone = sms24h_get_number(
                api_key=current_app.config["SMS24H_API_KEY"],
                base_url=current_app.config["SMS24H_BASE_URL"],
                service=service,
                country=country,
                operator=operator,
                proxy_str=proxy_str
            )
            _append_debug(user_id, waba_id, f"sms24h_get_number -> activation_id={activation_id} full_phone={full_phone}")

            if not activation_id:
                _append_debug(user_id, waba_id, "sms24h_get_number failed (no activation_id)")
                continue

            if not str(full_phone).startswith(cc):
                _append_debug(user_id, waba_id, f"Phone does not start with CC {cc}: {full_phone} -> cancel")
                sms24h_cancel(current_app.config["SMS24H_API_KEY"], current_app.config["SMS24H_BASE_URL"], activation_id, proxy_str)
                continue

            local_number = str(full_phone)[len(cc):]

            _job_update(job, last_message="Adicionando número no WABA...")
            r_add = add_phone_number(api_version, token, waba_id, cc, local_number, verified_name, proxy_str)
            _append_debug(user_id, waba_id, f"add_phone_number status={r_add.status_code} body={r_add.text[:900]}")

            if r_add.status_code != 200:
                sms24h_cancel(current_app.config["SMS24H_API_KEY"], current_app.config["SMS24H_BASE_URL"], activation_id, proxy_str)
                _append_debug(user_id, waba_id, "add_phone_number failed -> canceled activation")
                continue

            phone_id = (r_add.json() or {}).get("id")
            if not phone_id:
                msg = "Meta não retornou phone_id no add_phone"
                _set_error(user_id, waba_id, msg)
                _job_update(job, last_message=msg)
                return False

            _update_bms_entry(user_id, waba_id, {
                "pending_phone_number_id": str(phone_id),
                "sms24h_activation_id": str(activation_id),
                "sms24h_full_phone": str(full_phone),
                "otp_received": False,
                "otp_received_at": 0,
            })

            _job_update(job, last_message="Solicitando OTP (request_code)...")
            r_req = request_code(api_version, token, phone_id, code_method, language, proxy_str)
            _append_debug(user_id, waba_id, f"request_code status={r_req.status_code} body={r_req.text[:900]}")

            if r_req.status_code != 200:
                msg = f"request_code falhou: {r_req.text[:900]}"
                _set_error(user_id, waba_id, msg)
                sms24h_cancel(current_app.config["SMS24H_API_KEY"], current_app.config["SMS24H_BASE_URL"], activation_id, proxy_str)
                continue

            _job_update(job, last_message="Aguardando OTP (SMS24h)...")
            start = time.time()
            otp_code = None

            while time.time() - start < max_wait:
                st = sms24h_get_status(
                    api_key=current_app.config["SMS24H_API_KEY"],
                    base_url=current_app.config["SMS24H_BASE_URL"],
                    activation_id=activation_id,
                    proxy_str=proxy_str
                )

                if st.startswith("STATUS_OK"):
                    raw_code = st.split(":", 1)[1] if ":" in st else ""
                    otp_code = _only_digits(raw_code)
                    _append_debug(user_id, waba_id, f"sms24h STATUS_OK raw='{raw_code}' normalized='{otp_code}'")
                    if otp_code:
                        break

                elif st == "STATUS_CANCEL":
                    _append_debug(user_id, waba_id, "sms24h STATUS_CANCEL")
                    otp_code = None
                    break

                time.sleep(12)

            if not otp_code:
                _append_debug(user_id, waba_id, "OTP timeout -> cancel activation")
                _job_update(job, last_message="Sem OTP → cancelando (reembolso).")
                sms24h_cancel(current_app.config["SMS24H_API_KEY"], current_app.config["SMS24H_BASE_URL"], activation_id, proxy_str)
                continue

            _update_bms_entry(user_id, waba_id, {"otp_received": True, "otp_received_at": int(time.time())})

            ok, msg = _debit_otp(user_id, waba_id, phone_id)
            _append_debug(user_id, waba_id, f"DEBIT otp -> ok={ok} msg={msg}")
            if not ok:
                msg2 = f"OTP chegou mas {msg}"
                _set_error(user_id, waba_id, msg2)
                _job_update(job, last_message=msg2)
                return False

            _job_update(job, last_message="Verificando OTP (verify_code)...")
            r_ver = verify_code(api_version, token, phone_id, otp_code, proxy_str)
            _append_debug(user_id, waba_id, f"verify_code status={r_ver.status_code} body={r_ver.text[:900]}")

            if r_ver.status_code != 200:
                msg = f"verify_code falhou: {r_ver.text[:900]}"
                _set_error(user_id, waba_id, msg)
                _job_update(job, last_message="OTP recebido, mas verify_code falhou. Veja last_add_phone_error.")
                return False

            _job_update(job, last_message="Registrando número (register)...")
            r_reg = register_number(api_version, token, phone_id, pin="123456", proxy_str=proxy_str)
            _append_debug(user_id, waba_id, f"register status={r_reg.status_code} body={r_reg.text[:900]}")

            if r_reg.status_code == 200:
                _update_bms_entry(user_id, waba_id, {
                    "phone_number_id": str(phone_id),
                    "pending_phone_number_id": "",
                    "sms24h_activation_id": "",
                    "sms24h_full_phone": "",
                    "otp_received": False,
                    "otp_received_at": 0,
                    "last_add_phone_error": "",
                })
                _job_update(job, last_message="Número registrado com sucesso!")
                return True

            msg = f"register falhou: {r_reg.text[:900]}"
            _set_error(user_id, waba_id, msg)
            _job_update(job, last_message="verify ok, mas register falhou. Veja last_add_phone_error.")
            return False

        msg = "Falha após tentativas máximas"
        _set_error(user_id, waba_id, msg)
        _job_update(job, last_message=msg)
        return False

    except Exception as e:
        tb = traceback.format_exc()
        # We try to write into bms.json if entry exists
        _set_error(user_id, waba_id, f"EXCEPTION: {type(e).__name__}: {e}")
        try:
            _append_debug(user_id, waba_id, "EXCEPTION TRACEBACK:\n" + tb)
        except Exception:
            pass
        _job_update(job, last_message=f"EXCEPTION: {type(e).__name__}: {e}")
        return False
