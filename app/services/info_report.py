from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from ..json_store import load_user_bms
from .prosperidade import get_sales_statistics

_SP = ZoneInfo("America/Sao_Paulo")


def compute_bm_metrics(start_ts: int, end_ts: int) -> dict:
    """Aggregate BM analytics across ALL users for [start_ts, end_ts] (epoch seconds)."""
    from ..models import User
    from .meta import get_waba_analytics
    from flask import current_app

    api_version = current_app.config["META_API_VERSION"]

    all_entries: list[tuple[str, dict]] = []
    wabas_disparadas = 0

    for user in User.query.all():
        bms = load_user_bms(user.id) or {}
        for wid, data in bms.items():
            if not isinstance(data, dict):
                continue
            snap = (data.get("snapshot") or {})
            d_at = snap.get("disparou_at")
            if d_at and start_ts <= d_at <= end_ts:
                wabas_disparadas += 1
            all_entries.append((wid, data))

    total_sent = 0
    total_delivered = 0
    errors: list[str] = []

    def _fetch(wid: str, data: dict):
        token = (data.get("token") or "").strip()
        if not token:
            return 0, 0, None
        analytics, err = get_waba_analytics(api_version, token, wid, start_ts, end_ts)
        if err:
            return 0, 0, f"{wid}: {err}"
        points = (analytics or {}).get("data_points") or []
        return (
            sum(p.get("sent", 0) for p in points),
            sum(p.get("delivered", 0) for p in points),
            None,
        )

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch, wid, data): wid for wid, data in all_entries}
        for future in as_completed(futures):
            try:
                sent, delivered, err = future.result()
                total_sent += sent
                total_delivered += delivered
                if err:
                    errors.append(err)
            except Exception as exc:
                errors.append(str(exc))

    return {
        "wabas_disparadas": wabas_disparadas,
        "total_sent": total_sent,
        "total_delivered": total_delivered,
        "waba_count": len(all_entries),
        "errors": errors,
    }


def _today_window() -> tuple[int, int, str]:
    """Return (start_ts, end_ts, label_dd_mm) for today in America/Sao_Paulo."""
    now = datetime.now(_SP)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        int(start.timestamp()),
        int(now.timestamp()),
        now.strftime("%d/%m"),
    )


def _fmt_brl(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0,00"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build_info_report() -> str:
    from ..models import User

    start_ts, end_ts, label = _today_window()

    # Find admin with a Prosperidade key set
    admin = User.query.filter_by(is_admin=True).filter(
        User.prosperidade_api_key.isnot(None),
        User.prosperidade_api_key != "",
    ).first()

    if not admin:
        return (
            "⚠️ *Bot /info*\n\n"
            "Nenhum administrador configurou a chave da Prosperidade Payments.\n"
            "Acesse *Minha Conta* e salve sua API Key para ativar este relatório."
        )

    # BM metrics
    bm = compute_bm_metrics(start_ts, end_ts)

    # Financial data
    start_str = datetime.fromtimestamp(start_ts, tz=_SP).strftime("%Y-%m-%dT%H:%M:%S")
    end_str = datetime.fromtimestamp(end_ts, tz=_SP).strftime("%Y-%m-%dT%H:%M:%S")
    stats, err = get_sales_statistics(admin.prosperidade_api_key, start_str, end_str)

    if err or not stats:
        finance_block = f"💰 Faturamento: _erro ao buscar dados_\n📈 Taxa de conversão: —\n🧮 Média por BM: —"
    else:
        revenue = stats.get("totalRevenue", 0) or 0
        conversion = stats.get("conversionRate", 0) or 0
        wabas = bm["wabas_disparadas"] or 1  # avoid ZeroDivisionError
        media = revenue / wabas if bm["wabas_disparadas"] > 0 else 0

        finance_block = (
            f"💰 Faturamento: *R$ {_fmt_brl(revenue)}*\n"
            f"📈 Taxa de conversão: *{conversion}%*\n"
            f"🧮 Média por BM: *R$ {_fmt_brl(media)}*"
        )

    return (
        f"📊 *Relatório de Hoje* — {label}\n\n"
        f"🚀 BMs disparadas: *{bm['wabas_disparadas']}*\n"
        f"📨 Enviados: *{bm['total_sent']}*\n"
        f"✅ Entregues: *{bm['total_delivered']}*\n\n"
        f"{finance_block}"
    )
