"""
disparo_multi.py — Multi-BM bulk send orchestrator.

Flow:
  1. build_pool()    : read + project + merge + dedup N files into one list of
                       canonical dicts {__phone__, field1, field2, ...}
  2. allocate()      : assign pool slices to BMs up to each BM's quota;
                       strip last-selected BMs when pool < total capacity
  3. start_batch()   : fire one DisparoJob child per included BM (reuses the
                       existing engine with preloaded_rows)
  4. batch_status()  : aggregate child states into one summary
  5. batch_stop()    : propagate stop to all live children
"""

import json
import os
import threading
import uuid

from .disparar_service import (
    start_disparo_job,
    sent_log_path,
    _read_rows,
    get_live_state,
    request_stop,
    csvs_dir,
)
from .meta import get_phone_messaging_limit
from ..json_store import load_user_bms, patch_snapshot
from ..config import Config

# ── tier map ───────────────────────────────────────────────────────────────────

TIER_VALUES: dict[str, int] = {
    "TIER_250":    250,
    "TIER_1K":     1_000,
    "TIER_10K":    10_000,
    "TIER_100K":   100_000,
}

GLOBAL_BATCH_BUDGET = 200   # max total concurrent requests across all children


def tier_to_int(tier: str | None) -> int | None:
    if not tier:
        return None
    return TIER_VALUES.get(tier)          # None for UNLIMITED / unknown


# ── in-memory batch registry ───────────────────────────────────────────────────
# {batch_id: {user_id, children:[{job_id, waba_id, name, quota}], stripped, leftover, pool_size}}
_live_batches: dict[str, dict] = {}
_BATCH_LOCK = threading.Lock()


def _batch_sidecar_path(user_id: int, batch_id: str) -> str:
    base = os.path.join(os.getcwd(), "instance", "users", str(user_id))
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"batch_{batch_id}.json")


def _save_batch_sidecar(user_id: int, batch_id: str, data: dict) -> None:
    path = _batch_sidecar_path(user_id, batch_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_batch_sidecar(user_id: int, batch_id: str) -> dict | None:
    path = _batch_sidecar_path(user_id, batch_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── pool builder ───────────────────────────────────────────────────────────────

def build_pool(user_id: int, files_spec: list, skip_log: bool) -> list:
    """
    files_spec: list of dicts:
      {filename, has_header, phone_col, field_map: {canonical_key: raw_col_name}}

    Returns a list of canonical dicts: {__phone__: str, <canonical_key>: str, ...}
    Deduped by literal __phone__ string; empties dropped; sent_log filtered (unless skip_log).
    """
    # Load sent log once
    already_sent: set = set()
    if not skip_log:
        sp = sent_log_path(user_id)
        if os.path.exists(sp):
            with open(sp, "r", encoding="utf-8") as f:
                already_sent = {ln.strip() for ln in f if ln.strip()}

    seen_phones: set = set()
    pool: list = []

    for spec in files_spec:
        path = os.path.join(csvs_dir(user_id), spec["filename"])
        if not os.path.exists(path):
            continue
        has_header = spec.get("has_header", True)
        phone_col = spec["phone_col"]
        field_map: dict = spec.get("field_map", {})  # {canonical: raw_col}

        rows = _read_rows(path, has_header=has_header)
        for row in rows:
            phone = str(row.get(phone_col, "")).strip()
            if not phone:
                continue
            if phone in seen_phones:
                continue
            if phone in already_sent:
                continue
            seen_phones.add(phone)
            canonical = {"__phone__": phone}
            for cf, raw_col in field_map.items():
                canonical[cf] = str(row.get(raw_col, "")).strip()
            pool.append(canonical)

    return pool


# ── allocator ──────────────────────────────────────────────────────────────────

def allocate(wabas_spec: list, pool_size: int, overage_pct: float) -> dict:
    """
    wabas_spec: list (in selection order) of:
      {waba_id, name, phone_number_id, token, tier_str}

    Returns:
      {
        assignments: [{waba_id, name, phone_number_id, token, quota, start, end}],
        stripped:    [{waba_id, name, tier_str}],
        leftover:    int,
        total_capacity: int,
        pool_size: int,
        error: str | None   # "insufficient_leads" when even the first BM can't be filled
      }
    """
    multiplier = 1.0 + overage_pct / 100.0

    # Build quotas for each BM (skip unlimited / unknown tiers)
    bm_quotas = []
    for spec in wabas_spec:
        t = tier_to_int(spec.get("tier_str"))
        if t is None:
            continue
        quota = int(t * multiplier)
        bm_quotas.append({**spec, "quota": quota})

    # Find longest prefix that fits in pool_size (whole quotas only)
    cumulative = 0
    included = []
    stripped = []
    for bm in bm_quotas:
        if cumulative + bm["quota"] <= pool_size:
            included.append({**bm, "start": cumulative, "end": cumulative + bm["quota"]})
            cumulative += bm["quota"]
        else:
            stripped.append({"waba_id": bm["waba_id"], "name": bm["name"], "tier_str": bm.get("tier_str")})

    total_capacity = sum(b["quota"] for b in included)
    leftover = pool_size - total_capacity

    error = None
    if not included and bm_quotas:
        error = "insufficient_leads"

    return {
        "assignments": included,
        "stripped": stripped,
        "leftover": leftover,
        "total_capacity": total_capacity,
        "pool_size": pool_size,
        "error": error,
    }


# ── tier resolver ──────────────────────────────────────────────────────────────

def _resolve_tier(user_id: int, waba_id: str, token: str) -> str | None:
    """Return tier string from snapshot; fetch + cache from Meta if missing."""
    bms = load_user_bms(user_id)
    entry = bms.get(str(waba_id)) or {}
    snap = entry.get("snapshot") or {}
    tier = snap.get("messaging_limit_tier")
    if tier:
        return tier

    # Lazy fetch — try first phone number id
    phone_numbers = snap.get("phone_numbers") or []
    phone_id = (phone_numbers[0].get("id") if phone_numbers else None) or entry.get("phone_number_id", "")
    if not phone_id or not token:
        return None

    fetched = get_phone_messaging_limit(Config.META_API_VERSION, token, phone_id)
    if fetched:
        patch_snapshot(user_id, waba_id, messaging_limit_tier=fetched)
    return fetched


# ── batch start ────────────────────────────────────────────────────────────────

def start_batch(app, user_id: int,
                wabas_spec: list,
                files_spec: list,
                template_mode: str,         # "same" | "different"
                templates_cfg: dict | list, # same→dict, different→list[{waba_id,...}]
                overage_pct: float,
                max_workers: int,
                skip_log: bool) -> dict:
    """
    Returns:
      {batch_id, children, stripped, leftover, pool_size, error}
    On allocation error: {error: "insufficient_leads" | "no_valid_bms"}
    """
    # Resolve tiers for each BM in wabas_spec
    resolved_wabas = []
    for spec in wabas_spec:
        tier = _resolve_tier(user_id, spec["waba_id"], spec.get("token", ""))
        t_int = tier_to_int(tier)
        if t_int is None:
            continue   # skip unlimited / unknown
        resolved_wabas.append({**spec, "tier_str": tier})

    if not resolved_wabas:
        return {"error": "no_valid_bms"}

    # Build merged lead pool
    pool = build_pool(user_id, files_spec, skip_log)

    # Allocate
    alloc = allocate(resolved_wabas, len(pool), overage_pct)
    if alloc["error"]:
        return alloc

    # Compute per-child worker count within global budget
    n_children = len(alloc["assignments"])
    if max_workers == 0:
        # MAX mode: cap each child so total ≤ GLOBAL_BATCH_BUDGET, min 1
        child_workers = max(1, GLOBAL_BATCH_BUDGET // n_children)
    else:
        child_workers = max(1, min(max_workers, GLOBAL_BATCH_BUDGET // max(1, n_children)))

    batch_id = str(uuid.uuid4())[:8]
    children = []

    for bm in alloc["assignments"]:
        waba_id = bm["waba_id"]
        phone_number_id = bm["phone_number_id"]
        token = bm.get("token", "")

        # Resolve template + param_map for this BM
        if template_mode == "same":
            # Shared param_map; per-BM name/language (names vary across BMs)
            param_map = templates_cfg.get("param_map", [])
            bm_cfg = (templates_cfg.get("per_bm") or {}).get(waba_id, {})
            template_name = bm_cfg.get("template_name", "")
            template_language = bm_cfg.get("template_language", "en")
        else:
            # templates_cfg is a list; find entry for this waba_id
            cfg = next((c for c in templates_cfg if c.get("waba_id") == waba_id), {})
            template_name = cfg.get("template_name", "")
            template_language = cfg.get("template_language", "en")
            param_map = cfg.get("param_map", [])

        rows_slice = pool[bm["start"]:bm["end"]]

        job_id = start_disparo_job(
            app=app,
            user_id=user_id,
            csv_filename=f"batch_{batch_id}_{waba_id}",
            phone_col="__phone__",
            phone_number_id=phone_number_id,
            token=token,
            template_name=template_name,
            template_language=template_language,
            param_map=param_map,
            max_workers=child_workers,
            skip_log=skip_log,
            waba_id=waba_id,
            has_header=True,
            max_leads=0,
            preloaded_rows=rows_slice,
        )

        children.append({
            "job_id": job_id,
            "waba_id": waba_id,
            "name": bm.get("name", waba_id),
            "quota": bm["quota"],
        })

    batch_data = {
        "user_id": user_id,
        "children": children,
        "stripped": alloc["stripped"],
        "leftover": alloc["leftover"],
        "pool_size": alloc["pool_size"],
    }

    with _BATCH_LOCK:
        _live_batches[batch_id] = batch_data

    _save_batch_sidecar(user_id, batch_id, batch_data)

    return {
        "batch_id": batch_id,
        "children": children,
        "stripped": alloc["stripped"],
        "leftover": alloc["leftover"],
        "pool_size": alloc["pool_size"],
        "error": None,
    }


# ── batch status ───────────────────────────────────────────────────────────────

def batch_status(user_id: int, batch_id: str) -> dict | None:
    """Aggregate child job states. Returns None if batch not found."""
    data = _live_batches.get(batch_id) or _load_batch_sidecar(user_id, batch_id)
    if not data:
        return None

    from .. import db
    from ..models import DisparoJob
    import flask

    totals = {"total": 0, "sent": 0, "failed": 0, "skipped": 0}
    children_out = []
    any_running = False
    any_error = False

    for child in data.get("children", []):
        job_id = child["job_id"]
        live = get_live_state(job_id)
        if live:
            st = live
            any_running = any_running or live["status"] == "running"
            any_error = any_error or live["status"] == "error"
        else:
            # Try DB
            try:
                with flask.current_app.app_context():
                    job = db.session.get(DisparoJob, job_id)
            except RuntimeError:
                job = None
            if job:
                st = {
                    "status": job.status,
                    "total": job.total,
                    "sent": job.sent,
                    "failed": job.failed,
                    "skipped": job.skipped,
                    "last_message": job.last_message,
                }
                any_error = any_error or job.status == "error"
            else:
                st = {"status": "unknown", "total": 0, "sent": 0,
                      "failed": 0, "skipped": 0, "last_message": ""}

        for k in ("total", "sent", "failed", "skipped"):
            totals[k] += st.get(k, 0)

        children_out.append({
            "job_id": job_id,
            "waba_id": child["waba_id"],
            "name": child["name"],
            "quota": child["quota"],
            "status": st.get("status", "unknown"),
            "sent": st.get("sent", 0),
            "failed": st.get("failed", 0),
            "skipped": st.get("skipped", 0),
            "total": st.get("total", 0),
            "last_message": st.get("last_message", ""),
        })

    processed = totals["sent"] + totals["failed"]
    countable = max(1, totals["total"] - totals["skipped"])
    pct = round(processed / countable * 100)

    if any_running:
        overall_status = "running"
    elif any_error:
        overall_status = "error"
    elif all(c["status"] in ("done", "stopped", "error") for c in children_out):
        overall_status = "done"
    else:
        overall_status = "running"

    return {
        "batch_id": batch_id,
        "status": overall_status,
        "pool_size": data.get("pool_size", 0),
        "leftover": data.get("leftover", 0),
        "stripped": data.get("stripped", []),
        "pct": pct,
        **totals,
        "children": children_out,
    }


# ── batch stop ─────────────────────────────────────────────────────────────────

def batch_stop(user_id: int, batch_id: str) -> bool:
    data = _live_batches.get(batch_id) or _load_batch_sidecar(user_id, batch_id)
    if not data:
        return False
    for child in data.get("children", []):
        request_stop(child["job_id"])
    return True
