"""Background job: bulk-apply a saved profile photo to all phone numbers in selected WABAs."""
from __future__ import annotations

import threading
from typing import Optional

_live_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_job_counter = 0
_counter_lock = threading.Lock()


def _next_job_id() -> int:
    global _job_counter
    with _counter_lock:
        _job_counter += 1
        return _job_counter


def get_job(job_id: int) -> Optional[dict]:
    return _live_jobs.get(job_id)


def request_stop(job_id: int):
    with _jobs_lock:
        if job_id in _live_jobs:
            _live_jobs[job_id]["stop_requested"] = True


def _run_job(app, job_id: int, file_bytes: bytes, tasks: list[dict]):
    """
    `tasks` is a list of WABA-level dicts:
        {waba_id, waba_name, token, phone_numbers: [{id, display_phone_number}]}

    For each WABA:
      1. upload_resumable once → get handle
      2. set_profile_picture for each phone number
    """
    from ..services.meta import get_app_id, upload_resumable, set_profile_picture
    from ..config import Config

    state = _live_jobs[job_id]
    state["status"] = "running"

    upload_ver = Config.META_UPLOAD_API_VERSION
    fallback_app_id = Config.META_APP_ID

    with app.app_context():
        for task in tasks:
            if state.get("stop_requested"):
                break

            waba_id   = task["waba_id"]
            waba_name = task["waba_name"]
            token     = task["token"]
            phones    = task["phone_numbers"]

            # ── Step 1: get app_id and upload the photo once per WABA token ──
            app_id = get_app_id(upload_ver, token, fallback=fallback_app_id)
            if not app_id:
                # Record failure for all phones in this WABA
                with _jobs_lock:
                    for ph in phones:
                        state["done"]   += 1
                        state["failed"] += 1
                        state["results"].append({
                            "waba_id":   waba_id,
                            "waba_name": waba_name,
                            "name":      ph.get("display_phone_number", ph["id"]),
                            "ok":        False,
                            "msg":       "Não foi possível obter o App ID para o token desta WABA.",
                        })
                continue

            handle, upload_err = upload_resumable(upload_ver, app_id, token, file_bytes)
            if upload_err or not handle:
                with _jobs_lock:
                    for ph in phones:
                        state["done"]   += 1
                        state["failed"] += 1
                        state["results"].append({
                            "waba_id":   waba_id,
                            "waba_name": waba_name,
                            "name":      ph.get("display_phone_number", ph["id"]),
                            "ok":        False,
                            "msg":       f"Erro no upload da foto: {upload_err}",
                        })
                continue

            # ── Step 2: apply handle to each phone number ──
            for ph in phones:
                if state.get("stop_requested"):
                    break

                phone_id    = ph["id"]
                phone_label = ph.get("display_phone_number", phone_id)

                ok, err = set_profile_picture(upload_ver, token, phone_id, handle)

                with _jobs_lock:
                    state["done"] += 1
                    if ok:
                        state["success"] += 1
                        state["results"].append({
                            "waba_id":   waba_id,
                            "waba_name": waba_name,
                            "name":      phone_label,
                            "ok":        True,
                            "msg":       "",
                        })
                    else:
                        state["failed"] += 1
                        state["results"].append({
                            "waba_id":   waba_id,
                            "waba_name": waba_name,
                            "name":      phone_label,
                            "ok":        False,
                            "msg":       err or "Erro desconhecido",
                        })

    state["status"] = "stopped" if state.get("stop_requested") else "done"


def start_photo_job(user_id: int, photo_id: int, waba_ids: list[str]) -> int:
    """Build task list and launch background daemon. Returns job_id."""
    from ..models import PhotoModel
    from ..json_store import load_user_bms, photos_dir
    from ..services.meta import get_phone_numbers
    from ..config import Config
    from flask import current_app
    import os

    photo = PhotoModel.query.filter_by(id=photo_id, user_id=user_id).first()
    if not photo:
        raise ValueError("Foto não encontrada.")

    photo_path = os.path.join(photos_dir(user_id), photo.filename)
    if not os.path.exists(photo_path):
        raise ValueError("Arquivo da foto não encontrado no servidor.")

    with open(photo_path, "rb") as f:
        file_bytes = f.read()

    bms = load_user_bms(user_id)

    tasks: list[dict] = []
    total_phones = 0

    for waba_id in waba_ids:
        entry = bms.get(str(waba_id))
        if not isinstance(entry, dict):
            continue
        token = (entry.get("token") or "").strip()
        if not token:
            continue
        snap = entry.get("snapshot", {}) or {}
        waba_name = snap.get("waba_name") or waba_id

        # Enumerate phone numbers live so we have fresh ids
        phones, _ = get_phone_numbers(Config.META_API_VERSION, token, waba_id)
        if not phones:
            continue

        phone_list = [{"id": p["id"], "display_phone_number": p.get("display_phone_number", p["id"])}
                      for p in phones]
        total_phones += len(phone_list)
        tasks.append({
            "waba_id":      waba_id,
            "waba_name":    waba_name,
            "token":        token,
            "phone_numbers": phone_list,
        })

    job_id = _next_job_id()
    state = {
        "job_id":         job_id,
        "status":         "queued",
        "total":          total_phones,
        "done":           0,
        "success":        0,
        "failed":         0,
        "results":        [],
        "stop_requested": False,
    }
    with _jobs_lock:
        _live_jobs[job_id] = state

    app = current_app._get_current_object()
    t = threading.Thread(
        target=_run_job,
        args=(app, job_id, file_bytes, tasks),
        daemon=True,
    )
    t.start()
    return job_id
