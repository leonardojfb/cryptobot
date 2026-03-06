#!/usr/bin/env python3
# tg_bot.py — Telegram bot (python-telegram-bot v20+)
# Fixes aplicados:
#   - /notifs comando y menu de preferencias con botones toggle
#   - Header de autenticación corregido (x-secret)
#   - api_get() para endpoints GET (/status)
#   - cmd_propose usa formato plano correcto y parsea respuesta anidada
#   - /proposal/accept y /proposal/reject usan campo "proposal_id"
#   - ui:status y ui:help editan el mensaje en lugar de enviar uno nuevo
#   - cz50 implementa cierre parcial real vía /close_pct
#   - Todos los comandos registrados en build_app()

import os
import re
import json
import time
import html
import logging
import asyncio
from typing import Optional, Dict, Any, Tuple

import httpx
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ----------------------------------
# Config & helpers
# ----------------------------------

load_dotenv()
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_BASE       = (os.getenv("API_BASE") or "http://127.0.0.1:8000").rstrip("/")
SIGNAL_SECRET  = os.getenv("SIGNAL_SECRET", "").strip()
DEFAULT_SYMBOL = os.getenv("DEFAULT_SYMBOL", "BTCUSDT").upper()

# Para Windows (evita errores de event loop)
try:
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("tg_bot")

HTTP_TIMEOUT = 20.0

# Preferencias de notificaciones (en memoria; persist en disco si querés)
NOTIF_PREFS: Dict[str, bool] = {
    "fills":    True,   # Ejecuciones de órdenes
    "signals":  True,   # Señales del monitor
    "errors":   True,   # Errores críticos
    "pnl":      True,   # Resúmenes PnL
}
NOTIF_LABELS = {
    "fills":   "Ejecuciones",
    "signals": "Señales",
    "errors":  "Errores",
    "pnl":     "PnL",
}

# ----------------------------------
# Utilidades HTTP
# ----------------------------------

def _h() -> Dict[str, str]:
    """Headers para FastAPI — usa 'x-secret' que es lo que espera FastAPI Header(x_secret)."""
    return {"x-secret": SIGNAL_SECRET} if SIGNAL_SECRET else {}

def escape(s: str) -> str:
    return html.escape(str(s), quote=False)

def chunk_text(s: str, limit: int = 4000) -> list[str]:
    """Trocea texto largo para respetar el límite de Telegram (4096 chars)."""
    out, buf, total = [], [], 0
    for line in s.splitlines(keepends=True):
        if total + len(line) > limit:
            out.append("".join(buf))
            buf, total = [line], len(line)
        else:
            buf.append(line); total += len(line)
    if buf:
        out.append("".join(buf))
    return out or [""]

async def api_post(path: str, json_body: Dict[str, Any]) -> Tuple[bool, Any]:
    """POST al servidor FastAPI."""
    url = f"{API_BASE}{path}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=False) as cli:
        try:
            r = await cli.post(url, headers=_h(), json=json_body)
            if r.status_code < 300:
                return True, r.json()
            return False, {"status": r.status_code, "body": r.text}
        except httpx.HTTPError as e:
            return False, {"error": str(e)}

async def api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
    """GET al servidor FastAPI."""
    url = f"{API_BASE}{path}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=False) as cli:
        try:
            r = await cli.get(url, headers=_h(), params=params or {})
            if r.status_code < 300:
                return True, r.json()
            return False, {"status": r.status_code, "body": r.text}
        except httpx.HTTPError as e:
            return False, {"error": str(e)}

# ----------------------------------
# UI builders
# ----------------------------------

def kb_proposal(pid: str, symbol: str) -> InlineKeyboardMarkup:
    """Botones para manejar una propuesta concreta."""
    rows = [
        [
            InlineKeyboardButton("✅ Aceptar",    callback_data=f"acc:{pid}"),
            InlineKeyboardButton("❌ Rechazar",   callback_data=f"rej:{pid}"),
        ],
        [
            InlineKeyboardButton("🔻 Cerrar 50%",  callback_data=f"cz50:{symbol}"),
            InlineKeyboardButton("🧯 Cerrar 100%", callback_data=f"cz100:{symbol}"),
        ],
        [
            InlineKeyboardButton("📊 Estado", callback_data=f"status:{symbol}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def kb_home() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("➕ Proponer trade", callback_data="ui:new"),
            InlineKeyboardButton("📊 Estado",         callback_data="ui:status"),
        ],
        [
            InlineKeyboardButton("🔔 Notificaciones", callback_data="ui:notifs"),
            InlineKeyboardButton("⚙️ Config",         callback_data="ui:cfg"),
        ],
        [
            InlineKeyboardButton("❓ Ayuda",           callback_data="ui:help"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def kb_notifs() -> InlineKeyboardMarkup:
    """Teclado de preferencias de notificaciones con estado actual."""
    def btn(key: str) -> InlineKeyboardButton:
        icon  = "✅" if NOTIF_PREFS.get(key, True) else "🔕"
        label = NOTIF_LABELS.get(key, key.capitalize())
        return InlineKeyboardButton(f"{icon} {label}", callback_data=f"notif:tog:{key}")

    rows = [
        [btn("fills"),   btn("signals")],
        [btn("errors"),  btn("pnl")],
        [InlineKeyboardButton("↩️ Menú principal", callback_data="ui:home")],
    ]
    return InlineKeyboardMarkup(rows)

# ----------------------------------
# Formateo de propuestas
# ----------------------------------

def fmt_proposal_preview(p: Dict[str, Any]) -> str:
    """Render legible de la propuesta (HTML seguro). Acepta respuesta anidada o plana."""
    # Soporta tanto {"order": {...}} como respuesta plana del servidor
    if "order" in p and isinstance(p["order"], dict):
        order = p["order"]
    else:
        order = p  # flat

    sym    = escape(str(order.get("symbol", "—")))
    side   = escape(str(order.get("side",   "—")))
    action = escape(str(order.get("action", "OPEN")))

    # Risk — puede venir como dict o campos planos
    risk = order.get("risk") or {}
    if isinstance(risk, dict):
        risk_parts = []
        if "risk_usdt" in risk: risk_parts.append(f"risk_usdt={risk['risk_usdt']}")
        if "leverage"  in risk: risk_parts.append(f"lev={risk['leverage']}")
        risk_s = ", ".join(risk_parts) or "—"
    else:
        ru  = order.get("risk_usdt", "—")
        lev = order.get("leverage",  "—")
        risk_s = f"risk_usdt={ru}, lev={lev}"

    # SL
    sl = order.get("sl")
    if isinstance(sl, dict) and "price" in sl:
        sl_s = f"{sl.get('type','')}: {sl['price']}"
    elif order.get("sl_pct"):
        sl_s = f"SL {float(order['sl_pct'])*100:.2f}%"
    else:
        sl_s = "—"

    # TP
    tp = order.get("tp")
    tp_s = "—"
    if isinstance(tp, list) and tp:
        parts = []
        for i, leg in enumerate(tp[:2], 1):
            if isinstance(leg, dict):
                if "price" in leg:
                    parts.append(f"TP{i}@{leg['price']} {leg.get('size_pct','') or ''}".strip())
                elif leg.get("type", "").upper().startswith("TRAIL"):
                    parts.append(f"TP{i} TRAIL×{leg.get('mult','?')}")
        if parts:
            tp_s = " | ".join(parts)
    elif order.get("tp_pct"):
        tp_s = f"TP {float(order['tp_pct'])*100:.2f}%"

    lines = [
        "<b>📋 Propuesta</b>",
        f"• Acción:  <b>{action}</b>",
        f"• Símbolo: <b>{sym}</b>",
        f"• Lado:    <b>{side}</b>",
        f"• Riesgo:  <code>{escape(risk_s)}</code>",
        f"• SL:      <code>{escape(sl_s)}</code>",
        f"• TP:      <code>{escape(tp_s)}</code>",
    ]
    return "\n".join(lines)

# ----------------------------------
# Commands
# ----------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "<b>TradingBot listo</b> ✅\n\n"
        "• <code>/propose SYMBOL SIDE RISK LEV</code>\n"
        "   ej: <code>/propose BTCUSDT LONG 10 8</code>\n"
        "• Usá los botones <b>Aceptar / Rechazar / Cerrar</b>.\n\n"
        "Comandos:\n"
        "  <code>/help</code>  <code>/status [SYMBOL]</code>  <code>/close SYMBOL</code>  <code>/notifs</code>\n"
    )
    await update.effective_message.reply_html(txt, reply_markup=kb_home())


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "<b>❓ Ayuda</b>\n\n"
        "<b>Comandos:</b>\n"
        "• <code>/propose SYMBOL SIDE RISK LEV</code>\n"
        "   Crea una propuesta de trade.\n"
        "   ej: <code>/propose XRPUSDT LONG 10 10</code>\n"
        "   RISK = USDT, LEV = apalancamiento.\n\n"
        "• <code>/status [SYMBOL]</code>\n"
        "   Estado de posición y propuestas recientes.\n\n"
        "• <code>/close SYMBOL</code>\n"
        "   Cierra la posición al mercado (reduceOnly).\n\n"
        "• <code>/notifs</code>\n"
        "   Configura qué tipo de notificaciones recibís.\n\n"
        "<i>El bot solo opera cuando aceptás una propuesta con ✅.</i>"
    )
    await update.effective_message.reply_html(txt, reply_markup=kb_home())


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    symbol = (ctx.args[0] if ctx.args else DEFAULT_SYMBOL).upper()
    ok, res = await api_get("/status", {"symbol": symbol})
    if not ok:
        await update.effective_message.reply_html(
            f"❌ Error status: <code>{escape(json.dumps(res, ensure_ascii=False))}</code>"
        )
        return
    pretty = escape(json.dumps(res, ensure_ascii=False, indent=2))
    chunks = chunk_text(f"<b>📊 Estado {escape(symbol)}</b>\n<pre>{pretty}</pre>")
    for c in chunks:
        await update.effective_message.reply_html(c)


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.effective_message.reply_html(
            "Uso: <code>/close SYMBOL</code>\n"
            "ej: <code>/close BTCUSDT</code>"
        )
        return
    symbol = ctx.args[0].upper()
    ok, res = await api_post("/close", {"symbol": symbol})
    if not ok:
        await update.effective_message.reply_html(
            f"❌ Error close: <code>{escape(json.dumps(res, ensure_ascii=False))}</code>"
        )
        return
    await update.effective_message.reply_html(
        f"🧯 Cierre enviado: <b>{escape(symbol)}</b>\n"
        f"<code>{escape(json.dumps(res, ensure_ascii=False))}</code>"
    )


async def cmd_propose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /propose SYMBOL SIDE RISK_USDT LEVERAGE
    ej: /propose BTCUSDT LONG 10 8
    """
    if len(ctx.args) < 4:
        await update.effective_message.reply_html(
            "Uso: <code>/propose SYMBOL SIDE RISK_USDT LEV</code>\n"
            "ej: <code>/propose XRPUSDT LONG 10 8</code>"
        )
        return
    symbol = ctx.args[0].upper().replace("/", "")
    side   = ctx.args[1].upper()
    try:
        risk_usdt = float(ctx.args[2])
        lev       = int(ctx.args[3])
    except (ValueError, TypeError):
        await update.effective_message.reply_html(
            "❌ RISK_USDT y LEV deben ser numéricos.\n"
            "ej: <code>/propose BTCUSDT LONG 10 8</code>"
        )
        return

    if side not in ("LONG", "SHORT", "BUY", "SELL"):
        await update.effective_message.reply_html(
            "❌ SIDE debe ser LONG, SHORT, BUY o SELL."
        )
        return

    # Payload plano — coincide con ProposeIn del servidor
    payload = {
        "symbol":     symbol,
        "side":       side,
        "risk_usdt":  risk_usdt,
        "leverage":   lev,
    }

    ok, res = await api_post("/propose", payload)
    if not ok:
        await update.effective_message.reply_html(
            f"❌ Error propose: <code>{escape(json.dumps(res, ensure_ascii=False))}</code>"
        )
        return

    # El servidor devuelve {"ok": True, "proposal": {"id": ..., "order": {...}, ...}}
    proposal = res.get("proposal") or res  # compat por si es plano
    pid      = str(proposal.get("id") or res.get("id") or "?")

    preview = fmt_proposal_preview(proposal)
    await update.effective_message.reply_html(
        f"{preview}\n\n<i>ID:</i> <code>{escape(pid)}</code>",
        reply_markup=kb_proposal(pid, symbol),
    )


async def cmd_notifs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Muestra las preferencias de notificaciones con botones toggle."""
    on_count  = sum(1 for v in NOTIF_PREFS.values() if v)
    off_count = len(NOTIF_PREFS) - on_count
    txt = (
        "<b>🔔 Preferencias de notificaciones</b>\n\n"
        f"Activas: <b>{on_count}</b> / {len(NOTIF_PREFS)}\n\n"
        "Tocá un botón para activar/desactivar cada tipo:"
    )
    await update.effective_message.reply_html(txt, reply_markup=kb_notifs())


# ----------------------------------
# Text handler (atajos de texto libre)
# ----------------------------------

RE_PROPOSE = re.compile(
    r"^\s*(?:proponer?|propose)\s+([A-Z0-9_/\-]+)\s+(LONG|SHORT|BUY|SELL)\s+(\d+(?:\.\d+)?)\s+(\d+)\s*$",
    re.IGNORECASE,
)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    m = RE_PROPOSE.match(text)
    if m:
        sym, side = m.group(1).upper().replace("/",""), m.group(2).upper()
        ctx.args = [sym, side, m.group(3), m.group(4)]
        await cmd_propose(update, ctx)
        return

    await update.effective_message.reply_html(
        "No entendí. Probá por ejemplo:\n"
        "<code>propose BTCUSDT LONG 10 8</code>\n\n"
        "O usá los comandos: <code>/propose</code> <code>/status</code> "
        "<code>/close</code> <code>/notifs</code>",
        reply_markup=kb_home()
    )


# ----------------------------------
# Callback handler
# ----------------------------------

async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    await q.answer()

    try:
        # ── Propuesta: Aceptar ────────────────────────────────────────────
        if data.startswith("acc:"):
            pid = data.split(":", 1)[1]
            ok, res = await api_post("/proposal/accept", {"proposal_id": pid})
            if not ok:
                await q.edit_message_text(
                    f"❌ Error al aceptar:\n<code>{escape(json.dumps(res, ensure_ascii=False))}</code>",
                    parse_mode=ParseMode.HTML
                )
                return
            prop = res.get("proposal") or res
            await q.edit_message_text(
                f"✅ <b>Propuesta aceptada</b>\n"
                f"ID: <code>{escape(pid)}</code>\n"
                f"Estado: <b>{escape(str(prop.get('status','ACCEPTED')))}</b>",
                parse_mode=ParseMode.HTML
            )
            return

        # ── Propuesta: Rechazar ───────────────────────────────────────────
        if data.startswith("rej:"):
            pid = data.split(":", 1)[1]
            ok, res = await api_post("/proposal/reject", {"proposal_id": pid})
            if not ok:
                await q.edit_message_text(
                    f"❌ Error al rechazar:\n<code>{escape(json.dumps(res, ensure_ascii=False))}</code>",
                    parse_mode=ParseMode.HTML
                )
                return
            await q.edit_message_text(
                f"❌ <b>Propuesta rechazada</b>\n"
                f"ID: <code>{escape(pid)}</code>",
                parse_mode=ParseMode.HTML
            )
            return

        # ── Cerrar 50% ───────────────────────────────────────────────────
        if data.startswith("cz50:"):
            symbol = data.split(":", 1)[1].upper()
            ok, res = await api_post("/close_pct", {"symbol": symbol, "pct": 50})
            if not ok:
                # Fallback: intenta cierre total si /close_pct no existe
                ok, res = await api_post("/close", {"symbol": symbol})
                if not ok:
                    await q.edit_message_text(
                        f"❌ Error al cerrar 50% {escape(symbol)}:\n"
                        f"<code>{escape(json.dumps(res, ensure_ascii=False))}</code>",
                        parse_mode=ParseMode.HTML
                    )
                    return
                note = "\n<i>(endpoint /close_pct no disponible, se cerró 100%)</i>"
            else:
                note = ""
            await q.edit_message_text(
                f"🔻 Cierre 50% enviado: <b>{escape(symbol)}</b>{note}\n"
                f"<code>{escape(json.dumps(res, ensure_ascii=False))}</code>",
                parse_mode=ParseMode.HTML
            )
            return

        # ── Cerrar 100% ──────────────────────────────────────────────────
        if data.startswith("cz100:"):
            symbol = data.split(":", 1)[1].upper()
            ok, res = await api_post("/close", {"symbol": symbol})
            if not ok:
                await q.edit_message_text(
                    f"❌ Error al cerrar {escape(symbol)}:\n"
                    f"<code>{escape(json.dumps(res, ensure_ascii=False))}</code>",
                    parse_mode=ParseMode.HTML
                )
                return
            await q.edit_message_text(
                f"🧯 Cerrado 100%: <b>{escape(symbol)}</b>\n"
                f"<code>{escape(json.dumps(res, ensure_ascii=False))}</code>",
                parse_mode=ParseMode.HTML
            )
            return

        # ── Estado de posición ────────────────────────────────────────────
        if data.startswith("status:"):
            symbol = data.split(":", 1)[1].upper()
            ok, res = await api_get("/status", {"symbol": symbol})
            if not ok:
                await q.edit_message_text(
                    f"❌ Error status:\n<code>{escape(json.dumps(res, ensure_ascii=False))}</code>",
                    parse_mode=ParseMode.HTML
                )
                return
            pretty = escape(json.dumps(res, ensure_ascii=False, indent=2))
            txt    = f"<b>📊 Estado {escape(symbol)}</b>\n<pre>{pretty}</pre>"
            # Si es muy largo, editamos con resumen y enviamos el detalle
            if len(txt) > 4000:
                await q.edit_message_text(
                    f"<b>📊 Estado {escape(symbol)}</b>\n<i>(respuesta larga, ver abajo)</i>",
                    parse_mode=ParseMode.HTML
                )
                for chunk in chunk_text(f"<pre>{pretty}</pre>"):
                    await q.message.reply_html(chunk)
            else:
                await q.edit_message_text(txt, parse_mode=ParseMode.HTML)
            return

        # ── Notificaciones: toggle ────────────────────────────────────────
        if data.startswith("notif:tog:"):
            key = data.split(":", 2)[2]
            if key in NOTIF_PREFS:
                NOTIF_PREFS[key] = not NOTIF_PREFS[key]
                state = "✅ activado" if NOTIF_PREFS[key] else "🔕 desactivado"
                await q.answer(f"{NOTIF_LABELS.get(key, key)}: {state}", show_alert=False)
            # Actualiza los botones del mensaje
            on_count = sum(1 for v in NOTIF_PREFS.values() if v)
            txt = (
                "<b>🔔 Preferencias de notificaciones</b>\n\n"
                f"Activas: <b>{on_count}</b> / {len(NOTIF_PREFS)}\n\n"
                "Tocá un botón para activar/desactivar cada tipo:"
            )
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb_notifs())
            return

        # ── UI: Nuevo trade ───────────────────────────────────────────────
        if data == "ui:new":
            await q.edit_message_text(
                "<b>➕ Nueva propuesta</b>\n\n"
                "Usá: <code>/propose SYMBOL SIDE RISK LEV</code>\n"
                "ej: <code>/propose SOLUSDT LONG 15 6</code>\n\n"
                "O escribí directamente:\n"
                "<code>propose SOLUSDT LONG 15 6</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menú principal", callback_data="ui:home")
                ]])
            )
            return

        # ── UI: Estado ────────────────────────────────────────────────────
        if data == "ui:status":
            ok, res = await api_get("/status", {"symbol": DEFAULT_SYMBOL})
            if not ok:
                await q.edit_message_text(
                    f"❌ Error status:\n<code>{escape(json.dumps(res, ensure_ascii=False))}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Menú", callback_data="ui:home")
                    ]])
                )
                return
            pretty = escape(json.dumps(res, ensure_ascii=False, indent=2))
            txt = f"<b>📊 Estado {escape(DEFAULT_SYMBOL)}</b>\n<pre>{pretty}</pre>"
            if len(txt) > 4000:
                txt = txt[:3990] + "\n…</pre>"
            await q.edit_message_text(
                txt,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Actualizar", callback_data="ui:status"),
                    InlineKeyboardButton("↩️ Menú",      callback_data="ui:home"),
                ]])
            )
            return

        # ── UI: Notificaciones ────────────────────────────────────────────
        if data == "ui:notifs":
            on_count = sum(1 for v in NOTIF_PREFS.values() if v)
            txt = (
                "<b>🔔 Preferencias de notificaciones</b>\n\n"
                f"Activas: <b>{on_count}</b> / {len(NOTIF_PREFS)}\n\n"
                "Tocá un botón para activar/desactivar cada tipo:"
            )
            await q.edit_message_text(
                txt,
                parse_mode=ParseMode.HTML,
                reply_markup=kb_notifs()
            )
            return

        # ── UI: Config ────────────────────────────────────────────────────
        if data == "ui:cfg":
            await q.edit_message_text(
                f"<b>⚙️ Config actual</b>\n\n"
                f"API_BASE:      <code>{escape(API_BASE)}</code>\n"
                f"DEFAULT_SYMBOL: <code>{escape(DEFAULT_SYMBOL)}</code>\n"
                f"SIGNAL_SECRET:  <code>{'***' if SIGNAL_SECRET else '(vacío)'}</code>\n",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menú principal", callback_data="ui:home")
                ]])
            )
            return

        # ── UI: Ayuda ─────────────────────────────────────────────────────
        if data == "ui:help":
            txt = (
                "<b>❓ Ayuda</b>\n\n"
                "• <code>/propose SYMBOL SIDE RISK LEV</code> → propone un trade\n"
                "   ej: <code>/propose XRPUSDT LONG 10 10</code>\n"
                "   RISK=USDT, LEV=apalancamiento.\n\n"
                "• <code>/status [SYMBOL]</code> → estado de posición.\n"
                "• <code>/close SYMBOL</code> → cierra posición (reduceOnly).\n"
                "• <code>/notifs</code> → configurar notificaciones.\n\n"
                "<i>El bot solo opera cuando aceptás con ✅.</i>"
            )
            await q.edit_message_text(
                txt,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Menú principal", callback_data="ui:home")
                ]])
            )
            return

        # ── UI: Home (menú principal) ──────────────────────────────────────
        if data == "ui:home":
            txt = (
                "<b>TradingBot</b> 🤖\n\n"
                "¿Qué querés hacer?"
            )
            await q.edit_message_text(
                txt,
                parse_mode=ParseMode.HTML,
                reply_markup=kb_home()
            )
            return

        # ── Desconocido ───────────────────────────────────────────────────
        await q.answer(f"Acción no reconocida: {data}", show_alert=True)

    except Exception as e:
        log.exception("Error en callback %s", data)
        try:
            await q.edit_message_text(
                f"❌ Excepción inesperada:\n<code>{escape(str(e))}</code>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass


# ----------------------------------
# Main
# ----------------------------------

def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no está definido en .env")
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("close",   cmd_close))
    app.add_handler(CommandHandler("propose", cmd_propose))
    app.add_handler(CommandHandler("notifs",  cmd_notifs))   # ← NUEVO

    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


if __name__ == "__main__":
    log.info("Iniciando Telegram bot...")

    app = build_app()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info("Bot corriendo — presioná Ctrl+C para detener.")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            log.info("Deteniendo bot...")
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
