from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from ..json_store import load_user_bms
from .prosperidade import get_sales_statistics, get_balance, request_withdraw

_SP = ZoneInfo("America/Sao_Paulo")


def compute_bm_metrics(start_ts: int, end_ts: int, user_id: int) -> dict:
    """Aggregate BM analytics for a single user for [start_ts, end_ts] (epoch seconds)."""
    from .meta import get_waba_analytics
    from flask import current_app

    api_version = current_app.config["META_API_VERSION"]

    bms = load_user_bms(user_id) or {}
    entries = [(wid, data) for wid, data in bms.items() if isinstance(data, dict)]

    wabas_disparadas = 0
    for _wid, data in entries:
        snap = (data.get("snapshot") or {})
        d_at = snap.get("disparou_at")
        if d_at and start_ts <= d_at <= end_ts:
            wabas_disparadas += 1

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
        futures = {executor.submit(_fetch, wid, data): wid for wid, data in entries}
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
        "waba_count": len(entries),
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


def build_resumir_report() -> str:
    from flask import current_app
    from .info_refresh import _get_report_user

    admin = _get_report_user()
    if not admin:
        return (
            "⚠️ *Saque*\n\n"
            "Nenhum administrador configurou a chave da Prosperidade Payments.\n"
            "Acesse *Minha Conta* e salve sua API Key para ativar este comando."
        )

    balance, err = get_balance(admin.prosperidade_api_key)
    if err or not balance:
        return f"❌ Erro ao consultar saldo: {err or 'resposta vazia'}"

    available_cents = balance.get("availableBalance") or 0
    available_reais = available_cents / 100

    if available_cents == 0:
        return (
            "ℹ️ *Saque*\n\n"
            "Saldo disponível é R$ 0,00. Nenhuma operação realizada."
        )

    bank_account_id = current_app.config["WITHDRAW_BANK_ACCOUNT_ID"]
    password = current_app.config["WITHDRAW_PASSWORD"]

    result, err = request_withdraw(admin.prosperidade_api_key, available_cents, bank_account_id, "TED", password)
    if err or not result:
        return f"❌ Erro ao solicitar saque: {err or 'resposta vazia'}"

    # amount field in response is in cents — divide by 100 to display R$
    amount_brl = _fmt_brl((result.get("amount") or available_cents) / 100)
    status = result.get("status") or "—"

    return (
        f"💸 *Saque solicitado com sucesso!*\n\n"
        f"💰 Valor: *R$ {amount_brl}*\n"
        f"🏦 Tipo: TED\n"
        f"📌 Status: {status}\n\n"
        f"✅ O saque foi registrado e está em processamento."
    )


def build_info_report() -> str:
    from ..models import User, InfoSnapshot
    from .info_refresh import _get_report_user, refresh_snapshot

    _start_ts, end_ts, label = _today_window()

    admin = _get_report_user()

    if not admin:
        return (
            "⚠️ *Bot /info*\n\n"
            "Nenhum administrador configurou a chave da Prosperidade Payments.\n"
            "Acesse *Minha Conta* e salve sua API Key para ativar este relatório."
        )

    today = datetime.now(_SP).strftime("%Y-%m-%d")

    snap = (
        InfoSnapshot.query
        .filter_by(user_id=admin.id, day=today)
        .order_by(InfoSnapshot.id.desc())
        .first()
    )
    if snap is None:
        # Cache miss (e.g. first call of the day) — compute synchronously
        refresh_snapshot()
        snap = (
            InfoSnapshot.query
            .filter_by(user_id=admin.id, day=today)
            .order_by(InfoSnapshot.id.desc())
            .first()
        )

    if snap:
        bms_disparadas  = snap.bms_disparadas
        total_sent      = snap.total_sent
        total_delivered = snap.total_delivered
    else:
        bms_disparadas = total_sent = total_delivered = 0

    # Financial data — always live
    start_ts, _, _ = _today_window()
    start_str = datetime.fromtimestamp(start_ts, tz=_SP).strftime("%Y-%m-%dT%H:%M:%S")
    end_str   = datetime.fromtimestamp(end_ts,   tz=_SP).strftime("%Y-%m-%dT%H:%M:%S")
    stats, err = get_sales_statistics(admin.prosperidade_api_key, start_str, end_str)

    if err or not stats:
        finance_block = "💰 Faturamento: _erro ao buscar dados_\n📈 Taxa de conversão: —\n🧮 Média por BM: —"
    else:
        # Values are in CENTS — divide by 100
        received_cents = (stats.get("amountPixSales") or 0) + (stats.get("amountCreditCardSales") or 0)
        faturamento = received_cents / 100.0
        conversion = stats.get("conversionRate") or 0
        media = faturamento / bms_disparadas if bms_disparadas > 0 else 0

        finance_block = (
            f"💰 Faturamento: *R$ {_fmt_brl(faturamento)}*\n"
            f"📈 Taxa de conversão: *{conversion:.1f}%*\n"
            f"🧮 Média por BM: *R$ {_fmt_brl(media)}*"
        )

    return (
        f"📊 *Relatório de Hoje* — {label}\n\n"
        f"🚀 BMs disparadas: *{bms_disparadas}*\n"
        f"📨 Enviados: *{total_sent}*\n"
        f"✅ Entregues: *{total_delivered}*\n\n"
        f"{finance_block}"
    )
