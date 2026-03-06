"""
tg_controller.py v4 — Sistema completo de Telegram
- Menú interactivo de notificaciones por categoría
- /pos con PnL en tiempo real y botones Cerrar
- /signals filtrables por % de acierto
- /live: monitor en tiempo real de posiciones abiertas
- Todos los errores de API/bot van a categoría "dev"
- 27 comandos organizados por sección
"""

import asyncio, logging, os, time, threading
from typing import Optional, TYPE_CHECKING
from dotenv import load_dotenv
load_dotenv()

try:
    from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    )
    TG_AVAILABLE = True
except ImportError:
    TG_AVAILABLE = False
    # Fallback types para TYPE_CHECKING cuando la librería no está instalada
    if TYPE_CHECKING:
        from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.constants import ParseMode
        from telegram.ext import (
            Application, CommandHandler, CallbackQueryHandler, ContextTypes,
        )
    else:
        Update = BotCommand = InlineKeyboardButton = InlineKeyboardMarkup = None
        ParseMode = Application = CommandHandler = CallbackQueryHandler = ContextTypes = None

import notify_prefs
from ai_filter import ai_filter
import ai_memory

# Importar y configurar logger avanzado
try:
    from telegram_logger import get_telegram_logger
    log = get_telegram_logger()
except ImportError:
    # Fallback si telegram_logger.py no está disponible
    log = logging.getLogger("tg_controller")
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(name)s [%(levelname)s] - %(message)s'
    )
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",  "").strip()

BOT_INSTANCE = None
_paused      = False

# Tracks mensajes de posiciones "live" para editarlos en lugar de enviar nuevos
# { symbol: message_id }
_live_msgs: dict = {}
_live_lock = threading.Lock()

# ── Lista de comandos para BotFather ──────────────────────────────────────────
COMMAND_LIST = [
    ("start",     "Iniciar el bot"),
    ("stop",      "Detener el bot"),
    ("pause",     "Pausar entradas nuevas"),
    ("resume",    "Reanudar entradas"),
    ("scan",      "Scan manual del mercado ahora"),
    ("status",    "Estado general y balance"),
    ("balance",   "Balance USDT"),
    ("pos",       "Posiciones abiertas con PnL en tiempo real"),
    ("live",      "Monitor en vivo de posiciones (se actualiza solo)"),
    ("pnl",       "Resumen de PnL positivo y negativo"),
    ("daily",     "PnL dia a dia ultimos 14 dias"),
    ("close",     "Cerrar posicion: /close BTCUSDT"),
    ("closeall",  "Cerrar todas las posiciones"),
    ("perf",      "Rendimiento historico detallado"),
    ("signals",   "Señales mas exitosas y por que"),
    ("news",      "Sentimiento de noticias por par"),
    ("fg",        "Fear and Greed Index"),
    ("ai",        "Estado del filtro IA DeepSeek"),
    ("aihist",    "Ultimas decisiones de la IA con resultados"),
    ("accuracy",  "Precision historica de la IA"),
    ("notifs",    "Menu de notificaciones en tiempo real"),
    ("params",    "Parametros del motor IA"),
    ("set",       "Cambiar parametro: /set KEY VALUE"),
    ("lev",       "Cambiar leverage: /lev 20"),
    ("mode",      "Modo de trading actual"),
    ("watchlist", "Ver pares monitoreados"),
    ("add",       "Anadir par: /add DOGEUSDT"),
    ("remove",    "Quitar par: /remove DOGEUSDT"),
    ("risk",      "Estado del gestor de riesgo"),
    ("help",      "Lista de todos los comandos"),
]


def set_bot(bot):
    global BOT_INSTANCE
    BOT_INSTANCE = bot


# ── Envío con filtro de preferencias ─────────────────────────────────────────

def notify(category: str, text: str, parse_mode: str = "HTML"):
    """
    Envía un mensaje solo si la categoría está habilitada en las prefs.
    Usar en bot_autonomous en lugar de self.tg.send() directamente.
    """
    if BOT_INSTANCE and notify_prefs.is_enabled(category):
        BOT_INSTANCE.tg.send(text)


def notify_dev(text: str):
    """Errores, fallos de API, debug — solo si 'dev' está habilitado."""
    notify("dev", f"🔧 <b>DEV</b>\n{text}")


# ── Utilidades de formato ──────────────────────────────────────────────────────

def _pnl_str(pnl) -> str:
    if pnl is None: return "N/A"
    return f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"

def _pnl_emoji(pnl) -> str:
    if pnl is None: return "⚪"
    return "🟢" if pnl >= 0 else "🔴"

def _dur(seconds: int) -> str:
    if seconds < 60:   return f"{seconds}s"
    if seconds < 3600: return f"{seconds//60}m {seconds%60}s"
    return f"{seconds//3600}h {(seconds%3600)//60}m"

def _bar10(value: float, max_val: float = 10.0) -> str:
    filled = max(0, min(10, int(abs(value) / max(max_val, 0.001) * 10)))
    return "█" * filled + "░" * (10 - filled)

def _get_live_pnl(p: dict, client=None) -> tuple:
    """Retorna (mark_price, pnl_usdt, pnl_pct) para una posición."""
    entry = p.get("entry_price", 0)
    qty   = p.get("qty", 0)
    lev   = p.get("leverage", 1)
    side  = p.get("side", "LONG")
    mark  = entry
    if client:
        try:
            mark = client.get_mark_price(p["symbol"])
        except Exception:
            pass
    if entry and entry > 0:
        raw_pct  = (mark - entry) / entry * 100
        pnl_pct  = raw_pct if side == "LONG" else -raw_pct
        pnl_usdt = (mark - entry) * qty * lev * (1 if side == "LONG" else -1)
    else:
        pnl_pct = 0; pnl_usdt = 0
    return mark, pnl_usdt, pnl_pct

def _fmt_position_detail(p: dict, client=None) -> str:
    """Formato detallado de una posición con PnL en tiempo real."""
    sym   = p["symbol"]
    side  = p.get("side","LONG")
    entry = p.get("entry_price", 0)
    qty   = p.get("qty", 0)
    lev   = p.get("leverage", 1)
    tp    = p.get("tp")
    sl    = p.get("sl")
    dur_s = int(time.time()) - p.get("open_ts", int(time.time()))
    e     = "🟢" if side == "LONG" else "🔴"
    mark, pnl_usdt, pnl_pct = _get_live_pnl(p, client)

    # Barra de progreso hacia TP
    progress = ""
    if tp and sl and entry and abs(tp - sl) > 0:
        dist_total = abs(tp - entry)
        dist_now   = abs(mark - entry) if ((side=="LONG" and mark > entry) or (side=="SHORT" and mark < entry)) else 0
        pct_tp = min(100, int(dist_now / dist_total * 100)) if dist_total > 0 else 0
        filled = pct_tp // 10
        progress = f"\n   TP: <code>[{'█'*filled}{'░'*(10-filled)}]</code> {pct_tp}%"

    return (
        f"{e} <b>{sym}</b>  {side}  x{lev}\n"
        f"   Entry: <code>{entry:.4f}</code>  Ahora: <code>{mark:.4f}</code>\n"
        f"   PnL: <code>{_pnl_str(pnl_usdt)} USDT</code> "
        f"(<code>{_pnl_str(pnl_pct)}%</code>) {_pnl_emoji(pnl_usdt)}\n"
        f"   TP: <code>{tp or '—'}</code>  SL: <code>{sl or '—'}</code>"
        f"{progress}\n"
        f"   Duración: {_dur(dur_s)}  Qty: {qty}"
    )


# ── CONTROL ───────────────────────────────────────────────────────────────────

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if BOT_INSTANCE and not BOT_INSTANCE.running:
        BOT_INSTANCE.start()
        await u.effective_message.reply_text("🚀 Bot iniciado!\n\nUsa /help para ver los comandos disponibles.")
    else:
        await u.effective_message.reply_text("ℹ️ Bot ya activo.\n/status — estado general\n/pos — posiciones")

async def cmd_stop(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if BOT_INSTANCE and BOT_INSTANCE.running:
        BOT_INSTANCE.stop()
        await u.effective_message.reply_text("⛔ Bot detenido.")
    else:
        await u.effective_message.reply_text("ℹ️ Bot ya detenido.")

async def cmd_pause(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global _paused
    _paused = True
    await u.effective_message.reply_text(
        "⏸ <b>Entradas pausadas</b>\n"
        "Las posiciones abiertas siguen activas.\n"
        "Usa /resume para reanudar.",
        parse_mode=ParseMode.HTML
    )

async def cmd_resume(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global _paused
    _paused = False
    await u.effective_message.reply_text("▶️ <b>Entradas reanudadas</b>", parse_mode=ParseMode.HTML)

async def cmd_scan(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    msg = await u.effective_message.reply_text("🔍 Escaneando mercado...")
    try:
        from analysis_engine_bybit import scan_best_opportunities
        opps = scan_best_opportunities(BOT_INSTANCE.client, top_n=5, min_volume_usdt=5_000_000)
        if not opps:
            await msg.edit_text("🔍 Sin oportunidades destacadas ahora.")
            return
        lines = [f"<b>🔍 Top {len(opps)} oportunidades</b>\n"]
        for o in opps:
            e    = "🟢" if o["signal"] == "LONG" else "🔴"
            mode_e = {"AGGRESSIVE":"⚡","MOMENTUM":"🚀","STANDARD":"📊"}.get(o.get("entry_mode",""),"📊")
            lines.append(
                f"{e} <b>{o['symbol']}</b>  {o['signal']}  {mode_e}\n"
                f"   Score: <code>{o['composite_score']:+.2f}</code>  "
                f"Conf: <code>{o['confidence']:.0%}</code>"
            )
        await msg.edit_text("\n\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.edit_text(f"❌ Error en scan: {e}")


# ── ESTADO ────────────────────────────────────────────────────────────────────

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    s      = BOT_INSTANCE.get_status()
    perf   = s.get("performance", {})
    params = s.get("params", {})
    news   = s.get("news", {})
    risk   = s.get("risk", {})
    ai_s   = s.get("ai_filter", {})
    mode   = "🟡 PAPER" if s.get("paper_mode") else "🔴 REAL"
    state  = "✅ Activo" if s.get("running") else "⛔ Detenido"
    pause  = "  ⏸ PAUSADO" if _paused else ""
    fg_val = news.get("fear_greed", 50)
    fg_e   = "😱" if fg_val<=25 else ("😨" if fg_val<=45 else ("😐" if fg_val<=55 else ("😀" if fg_val<=75 else "🤑")))
    pnl    = perf.get("total_pnl", 0)
    cb     = "🔴 ON" if risk.get("circuit_breaker") else "✅ off"
    ai_str = f"✅ {ai_s.get('approved',0)}✅ {ai_s.get('rejected',0)}🚫 ({ai_s.get('approval_rate',0):.0f}%)" if ai_s.get("enabled") else "⚫ off"
    notif_active = sum(1 for v in notify_prefs.get_all().values() if v)
    await u.effective_message.reply_text(
        f"<b>Bot Trading — {mode}</b>\n"
        f"{state}{pause}\n"
        f"Balance: <code>{s.get('balance_usdt',0):.2f} USDT</code>  "
        f"Pos: {s.get('open_positions',0)}/{params.get('max_open_positions',5)}\n\n"
        f"<b>📊 Rendimiento</b>\n"
        f"Trades: {perf.get('total_trades',0)}  WR: {perf.get('win_rate',0):.1f}%\n"
        f"PnL: <code>{_pnl_str(pnl)} USDT</code> {_pnl_emoji(pnl)}\n\n"
        f"<b>⚙️ Config</b>\n"
        f"Umbral: {params.get('min_score_long',3.0):.1f}  "
        f"Lev: {params.get('default_leverage',20)}x  "
        f"Risk: {params.get('risk_pct_per_trade',2.0):.1f}%\n\n"
        f"{fg_e} F&G: {fg_val}  🛡️ CB: {cb}  🤖 IA: {ai_str}\n"
        f"🔔 Notifs activas: {notif_active}/{len(notify_prefs.DEFAULTS)}  "
        f"(ver /notifs)",
        parse_mode=ParseMode.HTML
    )

async def cmd_balance(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    bal = BOT_INSTANCE._get_balance()
    await u.effective_message.reply_text(
        f"💳 <b>Balance</b>\n<code>{bal:.2f} USDT</code>",
        parse_mode=ParseMode.HTML
    )


# ── POSICIONES ────────────────────────────────────────────────────────────────

async def cmd_pos(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Posiciones abiertas con PnL en tiempo real y botones para cerrar."""
    if not BOT_INSTANCE: return
    with BOT_INSTANCE._lock:
        poss = list(BOT_INSTANCE.open_positions.values())

    if not poss:
        await u.effective_message.reply_text("📭 No hay posiciones abiertas.")
        return

    for p in poss:
        text = _fmt_position_detail(p, BOT_INSTANCE.client)
        sym  = p["symbol"]
        # Botones inline: Cerrar | Actualizar
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"❌ Cerrar {sym}", callback_data=f"close:{sym}"),
            InlineKeyboardButton("🔄 Actualizar", callback_data=f"refresh:{sym}"),
        ]])
        await u.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def cmd_live(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Monitor en vivo: envía un mensaje por posición que se edita cada 30s."""
    if not BOT_INSTANCE: return
    with BOT_INSTANCE._lock:
        poss = list(BOT_INSTANCE.open_positions.values())
    if not poss:
        await u.effective_message.reply_text("📭 No hay posiciones abiertas para monitorear.")
        return

    await u.effective_message.reply_text(
        f"📡 <b>Monitor en vivo activado</b>\n"
        f"{len(poss)} posición(es) — se actualiza cada 30s\n"
        f"Usa /pos para ver el estado actual.",
        parse_mode=ParseMode.HTML
    )
    # Iniciar task de actualización periódica
    asyncio.create_task(_live_updater(u.effective_chat.id, c.bot))

async def _live_updater(chat_id: int, bot):
    """Task que edita mensajes de posiciones cada 30s."""
    if not BOT_INSTANCE: return
    # Enviar mensajes iniciales
    msg_ids = {}
    with BOT_INSTANCE._lock:
        poss = list(BOT_INSTANCE.open_positions.values())
    for p in poss:
        text = _fmt_position_detail(p, BOT_INSTANCE.client)
        sym  = p["symbol"]
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"❌ Cerrar {sym}", callback_data=f"close:{sym}"),
            InlineKeyboardButton("⏹ Stop live", callback_data=f"stoplive:{sym}"),
        ]])
        sent = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                      reply_markup=keyboard)
        msg_ids[sym] = sent.message_id

    # Loop de actualización (máx 20 min = 40 ciclos de 30s)
    for _ in range(40):
        await asyncio.sleep(30)
        if not BOT_INSTANCE or not BOT_INSTANCE.running:
            break
        with BOT_INSTANCE._lock:
            current_poss = dict(BOT_INSTANCE.open_positions)
        for sym, msg_id in list(msg_ids.items()):
            p = current_poss.get(sym)
            if not p:
                # Posición cerrada
                try:
                    await bot.edit_message_text(
                        f"✅ <b>{sym}</b> — posición cerrada",
                        chat_id=chat_id, message_id=msg_id,
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass
                del msg_ids[sym]
                continue
            try:
                text = _fmt_position_detail(p, BOT_INSTANCE.client)
                ts   = time.strftime("%H:%M:%S")
                text += f"\n<i>Actualizado: {ts}</i>"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"❌ Cerrar {sym}", callback_data=f"close:{sym}"),
                    InlineKeyboardButton("⏹ Stop live", callback_data=f"stoplive:{sym}"),
                ]])
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=msg_id,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
            except Exception:
                pass
        if not msg_ids:
            break


# ── PnL ───────────────────────────────────────────────────────────────────────

async def cmd_pnl(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    s    = BOT_INSTANCE.get_status()
    poss = s.get("positions", [])

    # No realizado
    unrealized = 0.0
    ur_lines   = []
    for p in poss:
        mark, pnl_u, pnl_pct = _get_live_pnl(p, BOT_INSTANCE.client)
        unrealized += pnl_u
        ur_lines.append(
            f"  {_pnl_emoji(pnl_u)} {p['symbol']:15s} "
            f"<code>{_pnl_str(pnl_u)} USDT</code> ({_pnl_str(pnl_pct)}%)"
        )

    # Realizado (DB)
    summary  = ai_memory.get_pnl_summary(days=30)
    closed   = summary.get("total_pnl") or 0
    wins     = summary.get("wins", 0)
    losses   = summary.get("losses", 0)
    total_t  = summary.get("total_trades", 0)
    wr       = summary.get("win_rate", 0)
    best_t   = summary.get("best_trade")
    worst_t  = summary.get("worst_trade")
    avg_win  = summary.get("avg_win")
    avg_loss = summary.get("avg_loss")
    pf       = summary.get("profit_factor")
    grand    = closed + unrealized

    lines = ["<b>💰 REPORTE PnL</b>\n",
             "<b>No realizado (posiciones abiertas):</b>"]
    lines += ur_lines if ur_lines else ["  Sin posiciones abiertas"]
    lines += [
        f"  Subtotal: <code>{_pnl_str(unrealized)} USDT</code> {_pnl_emoji(unrealized)}",
        "",
        "<b>Realizado últimos 30 días:</b>",
        f"  {total_t} trades  ✅{wins} / ❌{losses}  WR: {wr:.1f}%",
        f"  Total: <code>{_pnl_str(closed)} USDT</code> {_pnl_emoji(closed)}",
    ]
    if avg_win  is not None: lines.append(f"  Avg win: <code>+{avg_win:.2f}</code>")
    if avg_loss is not None: lines.append(f"  Avg loss: <code>{avg_loss:.2f}</code>")
    if best_t   is not None: lines.append(f"  Mejor: <code>{_pnl_str(best_t)}</code> 🏆")
    if worst_t  is not None: lines.append(f"  Peor: <code>{_pnl_str(worst_t)}</code> 💀")
    if pf       is not None: lines.append(f"  Profit factor: <code>{pf:.2f}</code>")
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>TOTAL GLOBAL</b>",
        f"<code>{_pnl_str(grand)} USDT</code>  {_pnl_emoji(grand)}",
    ]
    await u.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_daily(u: Update, c: ContextTypes.DEFAULT_TYPE):
    days_data = ai_memory.get_daily_pnl(days=14)
    if not days_data:
        await u.effective_message.reply_text("Sin datos de trades cerrados aún.")
        return
    lines = ["<b>📅 PnL por día (14 días)</b>\n"]
    for d in days_data:
        pnl  = d.get("pnl") or 0
        tr   = d.get("trades", 0)
        wins = d.get("wins", 0)
        day  = d.get("day", "?")
        bar  = _bar10(pnl, max_val=max(abs(pnl), 20))
        lines.append(
            f"{_pnl_emoji(pnl)} <code>{day}</code>  "
            f"<code>{_pnl_str(pnl):>9} USDT</code>\n"
            f"   {bar}  {tr}t ({wins}✅)"
        )
    total = sum(d.get("pnl") or 0 for d in days_data)
    lines.append(f"\n<b>Total: <code>{_pnl_str(total)} USDT</code></b>")
    await u.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── TRADES ────────────────────────────────────────────────────────────────────

async def cmd_close(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    if not c.args:
        await u.effective_message.reply_text("Uso: /close BTCUSDT")
        return
    sym = c.args[0].upper()
    msg = await u.effective_message.reply_text(f"⏳ Cerrando {sym}...")
    ok  = BOT_INSTANCE.try_close_trade(sym, reason="MANUAL_TG")
    await msg.edit_text(
        f"✅ {sym} cerrado." if ok else f"❌ Sin posición abierta en {sym}."
    )

async def cmd_closeall(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    with BOT_INSTANCE._lock:
        n = len(BOT_INSTANCE.open_positions)
    if n == 0:
        await u.effective_message.reply_text("ℹ️ No hay posiciones abiertas.")
        return
    msg = await u.effective_message.reply_text(f"⏳ Cerrando {n} posición(es)...")
    BOT_INSTANCE.force_close_all()
    await msg.edit_text("🔴 Todas las posiciones cerradas.")


# ── ANÁLISIS ──────────────────────────────────────────────────────────────────

async def cmd_perf(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    p   = BOT_INSTANCE.learner.get_performance_summary()
    sym = BOT_INSTANCE.learner.get_symbol_stats()
    ms  = BOT_INSTANCE.learner.get_mode_stats() if hasattr(BOT_INSTANCE.learner, "get_mode_stats") else {}
    pnl = p.get("total_pnl", 0)
    lines = [
        "<b>📊 Rendimiento histórico</b>",
        f"Trades: {p['total_trades']}  ✅{p['wins']} / ❌{p['losses']}  WR: {p['win_rate']:.1f}%",
        f"PnL: <code>{_pnl_str(pnl)} USDT</code> {_pnl_emoji(pnl)}",
        f"Avg win: <code>+{p['avg_win']:.2f}</code>  Avg loss: <code>{p['avg_loss']:.2f}</code>",
        f"Mejor: <code>{_pnl_str(p['best_trade'])}</code>  "
        f"Peor: <code>{_pnl_str(p['worst_trade'])}</code>",
    ]
    if ms:
        lines += ["", "<b>Por modo:</b>"]
        for mode, mv in sorted(ms.items(), key=lambda x: x[1].get("total_pnl",0), reverse=True):
            mwr  = mv["wins"]/mv["trades"]*100 if mv["trades"] else 0
            mpnl = mv["total_pnl"]
            lines.append(f"  {mode}: {mv['trades']}t WR={mwr:.0f}% PnL={_pnl_str(mpnl)} {_pnl_emoji(mpnl)}")
    if sym:
        lines += ["", "<b>Top 8 por PnL:</b>"]
        for s, v in sorted(sym.items(), key=lambda x: x[1].get("total_pnl",0), reverse=True)[:8]:
            wr   = v["wins"]/v["trades"]*100 if v["trades"] else 0
            spnl = v["total_pnl"]
            lines.append(f"  {s:15s} {v['trades']}t WR={wr:.0f}% {_pnl_str(spnl)} {_pnl_emoji(spnl)}")
    await u.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_signals(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    Señales más exitosas. Uso: /signals [min_wr] [symbol]
    Ej: /signals 60     → señales con WR >= 60%
        /signals 0 BTCUSDT → todas las señales de BTC
    """
    min_wr = 0
    filter_sym = None
    if c.args:
        try:    min_wr = float(c.args[0])
        except: pass
    if len(c.args) >= 2:
        filter_sym = c.args[1].upper()

    # Obtener stats por símbolo del learner
    sym_stats = BOT_INSTANCE.learner.get_symbol_stats() if BOT_INSTANCE else {}
    mode_stats = BOT_INSTANCE.learner.get_mode_stats() if BOT_INSTANCE and hasattr(BOT_INSTANCE.learner, "get_mode_stats") else {}

    # Datos de DB para análisis de éxito
    try:
        import sqlite3
        conn = sqlite3.connect("ai_memory.db")
        conn.row_factory = sqlite3.Row

        # Señales exitosas por símbolo + modo + condiciones
        where = "WHERE o.symbol=?" if filter_sym else ""
        params_q = (filter_sym,) if filter_sym else ()

        rows = conn.execute(f"""
            SELECT o.symbol, o.side, o.result,
                   d.entry_mode, d.composite_score, d.confidence,
                   d.squeeze, d.vol_spike, d.macro_bias, d.entry_bias,
                   d.fear_greed, d.news_direction,
                   o.pnl_usdt, o.close_reason
            FROM trade_outcomes o
            LEFT JOIN ai_decisions d ON o.trade_id = d.trade_id
            {where}
            ORDER BY o.ts_close DESC
            LIMIT 200
        """, params_q).fetchall()
        conn.close()
    except Exception as e:
        rows = []

    if not rows and not sym_stats:
        await u.effective_message.reply_text("Sin suficientes datos aún. Los análisis aparecen después de cerrar trades.")
        return

    # Agrupar por símbolo
    from collections import defaultdict
    sym_data = defaultdict(lambda: {"wins":0,"losses":0,"pnl":0,"modes":defaultdict(int),
                                    "squeeze_wins":0,"squeeze_total":0,
                                    "best":0,"worst":0,"reasons":defaultdict(int)})
    for r in rows:
        sym   = r["symbol"]
        res   = r["result"]
        pnl   = r["pnl_usdt"] or 0
        mode  = r["entry_mode"] or "STANDARD"
        sq    = r["squeeze"]
        close = r["close_reason"] or "?"
        if res == "WIN":
            sym_data[sym]["wins"] += 1
            sym_data[sym]["best"] = max(sym_data[sym]["best"], pnl)
        elif res == "LOSS":
            sym_data[sym]["losses"] += 1
            sym_data[sym]["worst"] = min(sym_data[sym]["worst"], pnl)
        sym_data[sym]["pnl"] += pnl
        sym_data[sym]["modes"][mode] += 1
        sym_data[sym]["reasons"][close] += 1
        if sq:
            sym_data[sym]["squeeze_total"] += 1
            if res == "WIN": sym_data[sym]["squeeze_wins"] += 1

    # Filtrar por WR mínimo y ordenar
    results = []
    for sym, d in sym_data.items():
        total = d["wins"] + d["losses"]
        if total < 2: continue
        wr = d["wins"] / total * 100
        if wr >= min_wr:
            results.append((sym, wr, total, d))
    results.sort(key=lambda x: (-x[1], -x[2]))  # Ordenar por WR desc, luego trades

    if not results:
        await u.effective_message.reply_text(
            f"No hay señales con WR ≥ {min_wr:.0f}%\n"
            f"Prueba con /signals 0 para ver todas."
        )
        return

    # Formatear
    sym_filter_str = f" para {filter_sym}" if filter_sym else ""
    header = (
        f"<b>📡 Señales más exitosas{sym_filter_str}</b>\n"
        f"<i>Filtro: WR ≥ {min_wr:.0f}% | {len(results)} símbolos</i>\n\n"
        f"Uso: /signals [min_wr] [SYMBOL]\n"
        f"Ej: /signals 60  o  /signals 0 BTCUSDT\n\n"
    )
    lines = [header]
    for sym, wr, total, d in results[:12]:
        wins   = d["wins"]
        pnl    = d["pnl"]
        best_mode = max(d["modes"], key=d["modes"].get) if d["modes"] else "?"
        sq_rate = d["squeeze_wins"]/d["squeeze_total"]*100 if d["squeeze_total"] else 0
        best_close = max(d["reasons"], key=d["reasons"].get) if d["reasons"] else "?"
        bar = _bar10(wr, 100)

        why_parts = []
        if wr >= 60: why_parts.append(f"modo {best_mode} domina")
        if sq_rate >= 60 and d["squeeze_total"] >= 2:
            why_parts.append(f"squeeze WR={sq_rate:.0f}%")
        if d["best"] > 20: why_parts.append(f"trades grandes (+{d['best']:.0f})")
        why = " | ".join(why_parts) if why_parts else "historial limitado"

        lines.append(
            f"{'🏆' if wr>=65 else ('✅' if wr>=50 else '⚠️')} "
            f"<b>{sym}</b>  WR=<code>{wr:.0f}%</code>  ({wins}/{total})\n"
            f"   {bar}\n"
            f"   PnL: <code>{_pnl_str(pnl)} USDT</code> {_pnl_emoji(pnl)}\n"
            f"   Mejor modo: {best_mode}  |  Cierre: {best_close}\n"
            f"   🔍 {why}"
        )
    await u.effective_message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_news(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    ns   = BOT_INSTANCE.news.get_status()
    fg   = ns.get("fear_greed", 50)
    gb   = ns.get("global_bias", 0.0)
    fg_e = "😱" if fg<=25 else ("😨" if fg<=45 else ("😐" if fg<=55 else ("😀" if fg<=75 else "🤑")))
    gb_e = "🟢" if gb>0.1 else ("🔴" if gb<-0.1 else "⚪")
    last = time.strftime("%H:%M:%S", time.localtime(ns.get("last_scan",0))) if ns.get("last_scan") else "nunca"
    lines = [
        "<b>📰 Noticias y Sentimiento</b>",
        f"Scan: <code>{last}</code>  Caché: {ns.get('total_cached',0)}",
        f"{fg_e} F&G: {fg}/100 — {ns.get('fg_label','Neutral')}",
        f"{gb_e} Global: <code>{gb:+.2f}</code>",
        "",
        "<b>Por par (top 10):</b>",
    ]
    for sym, score in sorted(ns.get("symbol_biases",{}).items(), key=lambda x: abs(x[1]), reverse=True)[:10]:
        e   = "🟢" if score>0.1 else ("🔴" if score<-0.1 else "⚪")
        bar = _bar10(score, 1.0)
        lines.append(f"  {e} {sym:15s} <code>{score:+.2f}</code> {bar}")
    recent = BOT_INSTANCE.news.get_recent_news(5)
    if recent:
        lines += ["", "<b>Últimas noticias:</b>"]
        for item in recent:
            d   = item.get("direction","")
            s   = item.get("sentiment_score", 0)
            age = int((time.time() - item.get("ts", time.time())) / 60)
            e   = "🟢" if d=="BULLISH" else "🔴"
            lines.append(
                f"{e} <code>{s:+.2f}</code> hace {age}m [{item.get('source','')}]\n"
                f"   {item.get('title','')[:90]}"
            )
    await u.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_fg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    fg    = BOT_INSTANCE.news.get_fear_greed()
    v     = fg.get("value", 50)
    lbl   = fg.get("label", "Neutral")
    label = ("😱 EXTREMO MIEDO" if v<=25 else "😨 MIEDO" if v<=45
             else "😐 NEUTRAL" if v<=55 else "😀 CODICIA" if v<=75 else "🤑 CODICIA EXTREMA")
    bar   = "█" * int(v/10) + "░" * (10 - int(v/10))
    interp = ("Mercado pesimista → posible oportunidad" if v<30
              else "Neutral → seguir señales técnicas" if v<60
              else "Euforia → precaución")
    await u.effective_message.reply_text(
        f"<b>Fear & Greed</b>\n\n<code>[{bar}]</code> {v}/100\n{label}\n\n{interp}",
        parse_mode=ParseMode.HTML
    )


# ── IA ────────────────────────────────────────────────────────────────────────

async def cmd_ai(u: Update, c: ContextTypes.DEFAULT_TYPE):
    s = ai_filter.get_stats()
    if not s["enabled"]:
        await u.effective_message.reply_text(
            "🤖 <b>AI Filter</b>\nEstado: ⚫ Desactivado\n\n"
            "Añade en .env:\n<code>DEEPSEEK_API_KEY=sk-xxx</code>\n"
            "<code>AI_FILTER_ENABLED=true</code>",
            parse_mode=ParseMode.HTML
        )
        return
    bar = _bar10(s["approval_rate"], 100)
    await u.effective_message.reply_text(
        f"🤖 <b>AI Filter — DeepSeek</b>  ✅ Activo\n\n"
        f"Consultas: <code>{s['calls']}</code>  Cache: <code>{s['cache_hits']}</code>\n"
        f"✅ Aprobados:  <code>{s['approved']}</code>\n"
        f"🚫 Rechazados: <code>{s['rejected']}</code>\n"
        f"❌ Errores:    <code>{s['errors']}</code>\n\n"
        f"Tasa: <code>{s['approval_rate']:.1f}%</code>  <code>[{bar}]</code>\n"
        f"Tiempo avg: <code>{s['avg_ms']}ms</code>  "
        f"Confianza mín: <code>{ai_filter.min_conf:.0%}</code>",
        parse_mode=ParseMode.HTML
    )

async def cmd_aihist(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    Historial de decisiones IA con señal enviada y razonamiento.
    Uso: /aihist [filtro]  donde filtro = all | approved | rejected
    """
    filtro = c.args[0].lower() if c.args else "all"
    decisions = ai_memory.get_recent_decisions(limit=15)
    if not decisions:
        await u.effective_message.reply_text("Sin decisiones registradas aún.")
        return

    filtered = [d for d in decisions if
                filtro == "all" or
                (filtro == "approved"  and d.get("approved")) or
                (filtro == "rejected"  and not d.get("approved"))]

    lines = [
        f"<b>🤖 Decisiones IA</b>  <i>({filtro})</i>\n"
        f"<i>/aihist all | approved | rejected</i>\n"
    ]
    for d in filtered[:10]:
        ts    = time.strftime("%m/%d %H:%M", time.localtime(d.get("ts",0)))
        appr  = "✅" if d.get("approved") else "🚫"
        conf  = d.get("ai_confidence") or 0
        sym   = d.get("symbol","?")
        sig   = d.get("signal","?")
        score = d.get("composite_score") or 0
        mode  = d.get("entry_mode","?")
        reas  = (d.get("reasoning") or "")[:100]
        fg    = d.get("fear_greed","?")
        n_dir = d.get("news_direction","?")
        # Resultado real
        res   = d.get("result")
        pnl   = d.get("pnl_usdt")
        out_str = ""
        if res:
            out_str = f"\n   Resultado: {_pnl_emoji(pnl)} <b>{res}</b> <code>{_pnl_str(pnl)} USDT</code>"

        lines.append(
            f"{appr} <code>{ts}</code>  <b>{sym}</b> {sig}\n"
            f"   Score: {score:+.1f}  Conf IA: {conf:.0%}  Mode: {mode}\n"
            f"   F&G: {fg}  News: {n_dir}\n"
            f"   📝 <i>\"{reas}\"</i>"
            f"{out_str}"
        )
    await u.effective_message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_accuracy(u: Update, c: ContextTypes.DEFAULT_TYPE):
    filter_sym = c.args[0].upper() if c.args else None
    gen = ai_memory.get_ai_accuracy(filter_sym)
    total = gen.get("total", 0)
    if not total:
        await u.effective_message.reply_text(
            "Sin suficientes datos.\n<i>/accuracy BTCUSDT para un par específico</i>",
            parse_mode=ParseMode.HTML
        )
        return
    app_wins  = gen.get("approved_wins",0) or 0
    app_loss  = gen.get("approved_losses",0) or 0
    rej_wins  = gen.get("rejected_wins",0) or 0
    rej_loss  = gen.get("rejected_losses",0) or 0
    app_total = app_wins + app_loss
    rej_total = rej_wins + rej_loss
    app_pnl   = gen.get("approved_pnl") or 0
    avoided   = gen.get("avoided_loss") or 0
    app_wr    = app_wins/app_total*100 if app_total else 0
    rej_acc   = rej_loss/rej_total*100 if rej_total else 0

    sym_str = f" ({filter_sym})" if filter_sym else " (global)"
    lines = [
        f"<b>🎯 Precisión IA{sym_str}</b>\n",
        "<b>Trades APROBADOS:</b>",
        f"  {app_total} trades  ✅{app_wins} / ❌{app_loss}  WR={app_wr:.1f}%",
        f"  PnL: <code>{_pnl_str(app_pnl)} USDT</code> {_pnl_emoji(app_pnl)}",
        "",
        "<b>Trades RECHAZADOS:</b>",
        f"  {rej_total}  (habrían ganado: {rej_wins}  perdido: {rej_loss})",
        f"  Precisión rechazo: <code>{rej_acc:.1f}%</code>",
        f"  Pérdidas evitadas: <code>{_pnl_str(avoided)} USDT</code> {_pnl_emoji(avoided)}",
        "",
        "<i>/accuracy BTCUSDT para ver por par</i>",
    ]
    await u.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── NOTIFICACIONES ────────────────────────────────────────────────────────────

async def cmd_notifs(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Menú interactivo para activar/desactivar notificaciones por categoría."""
    await _send_notifs_menu(u.effective_message.reply_text, u.effective_chat.id)

async def _send_notifs_menu(reply_fn, chat_id):
    prefs = notify_prefs.get_all()
    keyboard = []
    for cat, enabled in prefs.items():
        desc = notify_prefs.DESCRIPTIONS.get(cat, cat)
        icon = "🔔" if enabled else "🔕"
        btn_label = f"{icon} {desc}"
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=f"notif:{cat}")])
    # Fila final: activar todo / desactivar todo
    keyboard.append([
        InlineKeyboardButton("✅ Activar todo",    callback_data="notif:_all_on"),
        InlineKeyboardButton("🔕 Desactivar todo", callback_data="notif:_all_off"),
    ])
    active   = sum(1 for v in prefs.values() if v)
    inactive = len(prefs) - active
    text = (
        f"<b>🔔 Notificaciones en tiempo real</b>\n"
        f"{active} activas / {inactive} desactivadas\n\n"
        f"Toca para activar/desactivar cada tipo:"
    )
    await reply_fn(text, parse_mode=ParseMode.HTML,
                   reply_markup=InlineKeyboardMarkup(keyboard))


# ── CONFIG ────────────────────────────────────────────────────────────────────

async def cmd_params(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    params = BOT_INSTANCE.learner.get_params()
    skip   = {"tf_weights","indicators_active"}
    lines  = ["<b>⚙️ Parámetros IA</b>", "<i>/set KEY VALUE para cambiar</i>", ""]
    for k, v in params.items():
        if k not in skip:
            lines.append(f"  <code>{k}</code>: <code>{v}</code>")
    await u.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_set(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    if not c.args or len(c.args) < 2:
        await u.effective_message.reply_text(
            "Uso: /set KEY VALUE\n\n"
            "Ejemplos:\n/set min_score_long 3.5\n"
            "/set default_leverage 15\n/set risk_pct_per_trade 1.5"
        )
        return
    key, raw = c.args[0], c.args[1]
    try:    val = float(raw) if "." in raw else int(raw)
    except: val = {"true":True,"false":False}.get(raw.lower(), raw)
    BOT_INSTANCE.learner.params[key] = val
    BOT_INSTANCE.learner._save()
    await u.effective_message.reply_text(f"✅ <code>{key}</code> = <code>{val}</code>", parse_mode=ParseMode.HTML)

async def cmd_lev(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    if not c.args:
        lev = BOT_INSTANCE.learner.params.get("default_leverage", 20)
        await u.effective_message.reply_text(f"Leverage actual: <code>{lev}x</code>\nUso: /lev 20", parse_mode=ParseMode.HTML)
        return
    try:
        lev = int(c.args[0])
        if not (1 <= lev <= 100):
            await u.effective_message.reply_text("❌ Leverage entre 1 y 100.")
            return
        BOT_INSTANCE.learner.params["default_leverage"] = lev
        BOT_INSTANCE.learner._save()
        await u.effective_message.reply_text(f"✅ Leverage: <code>{lev}x</code>", parse_mode=ParseMode.HTML)
    except ValueError:
        await u.effective_message.reply_text("❌ Usa un número. Ej: /lev 20")

async def cmd_mode(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    params = BOT_INSTANCE.learner.get_params()
    pause_str = "⏸ PAUSADO" if _paused else "▶️ Activo"
    await u.effective_message.reply_text(
        f"<b>🎛 Modo de Trading</b>\n{pause_str}\n\n"
        f"<b>Umbrales:</b>\n"
        f"  ⚡ AGGRESSIVE: score ≥ <code>2.5</code>\n"
        f"  🚀 MOMENTUM:  score ≥ <code>3.0</code>\n"
        f"  📊 STANDARD:  score ≥ <code>{params.get('min_score_long',3.0)}</code>\n\n"
        f"<b>Posiciones:</b>\n"
        f"  Lev: <code>{params.get('default_leverage',20)}x</code>  "
        f"Risk: <code>{params.get('risk_pct_per_trade',2.0):.1f}%</code>  "
        f"Max: <code>{params.get('max_open_positions',5)}</code>\n"
        f"  Cooldown: <code>{params.get('cooldown_seconds',20)}s</code>  "
        f"Trailing: <code>{'✅' if params.get('use_trailing') else '❌'}</code>\n\n"
        f"<b>IA:</b> {'✅ Activo' if ai_filter.enabled else '⚫ Desactivado'}\n"
        f"  Confianza mín: <code>{ai_filter.min_conf:.0%}</code>",
        parse_mode=ParseMode.HTML
    )

async def cmd_watchlist(u: Update, c: ContextTypes.DEFAULT_TYPE):
    from bot_autonomous import FIXED_WATCHLIST
    await u.effective_message.reply_text(
        f"<b>📋 Watchlist ({len(FIXED_WATCHLIST)} pares)</b>\n" +
        "\n".join(f"• {s}" for s in FIXED_WATCHLIST),
        parse_mode=ParseMode.HTML
    )

async def cmd_add(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE or not c.args: return
    sym = c.args[0].upper()
    if not sym.endswith("USDT"):
        await u.effective_message.reply_text("⚠️ Usa formato XXXUSDT (ej: DOGEUSDT)")
        return
    BOT_INSTANCE.add_to_watchlist(sym)
    await u.effective_message.reply_text(f"✅ {sym} añadido.")

async def cmd_remove(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE or not c.args: return
    BOT_INSTANCE.remove_from_watchlist(c.args[0].upper())
    await u.effective_message.reply_text(f"✅ {c.args[0].upper()} removido.")

async def cmd_risk(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not BOT_INSTANCE: return
    r  = BOT_INSTANCE.risk_mgr.get_status()
    cb = "🔴 ACTIVO" if r["circuit_breaker"] else "✅ OFF"
    pnl_d = r.get("daily_pnl",0)
    text = (
        f"<b>🛡️ Risk Manager</b>\n"
        f"PnL diario: <code>{_pnl_str(pnl_d)} USDT</code> {_pnl_emoji(pnl_d)}\n"
        f"Pérdidas consecutivas: {r['consecutive_losses']}/4\n"
        f"Circuit breaker: {cb}"
    )
    if r["circuit_breaker"] and r.get("circuit_until",0) > time.time():
        text += f"\nReanuda en: {int((r['circuit_until']-time.time())/60)}m"
    await u.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_help(u: Update, c: ContextTypes.DEFAULT_TYPE):
    sections = [
        ("🎮 Control",   ["start","pause","resume","stop","scan"]),
        ("📊 Estado",    ["status","balance","pos","live","pnl","daily"]),
        ("💼 Trades",    ["close","closeall"]),
        ("🔬 Análisis",  ["perf","signals","news","fg"]),
        ("🤖 IA",        ["ai","aihist","accuracy"]),
        ("🔔 Notifs",    ["notifs"]),
        ("⚙️ Config",    ["params","set","lev","mode","watchlist","add","remove","risk"]),
    ]
    cmd_map = {cmd: desc for cmd, desc in COMMAND_LIST}
    lines   = ["<b>🤖 Comandos disponibles</b>\n"]
    for section, cmds in sections:
        lines.append(f"<b>{section}:</b>")
        for cmd in cmds:
            lines.append(f"  /{cmd} — {cmd_map.get(cmd,'')}")
        lines.append("")
    await u.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Callback handler (botones inline) ─────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Patrón correcto PTB v20+:
    1. q.answer() SIEMPRE primero — quita el spinner del botón
    2. Toda la lógica dentro de try/except
    """
    q = update.callback_query
    if not q:
        log.warning("[CALLBACK] callback_query es None")
        return
    data = q.data or ""
    user_id = update.effective_user.id if update.effective_user else "unknown"
    log.info(f"[CALLBACK] Botón presionado - usuario: {user_id}, data: '{data}'")

    # Responder INMEDIATAMENTE para quitar el spinner
    # (igual que tg_bot.py que ya funciona en este proyecto)
    # *** CRÍTICO: Responder INMEDIATAMENTE al callback para quitar spinner ***
    try:
        await q.answer()
        log.info(f"[CALLBACK] ✅ ANSWER ENVIADO - spinner bajo control")
    except Exception as e:
        log.error(f"[CALLBACK] ❌ CRÍTICO: Error en q.answer() - El botón puede quedar en spinner: {e}", exc_info=True)

    try:
        # ── Cerrar posición ───────────────────────────────────────────────────
        if data.startswith("close:"):
            sym = data[6:]
            log.info(f"[CLOSE] Solicitud de cierre del símbolo: {sym}")
            if not BOT_INSTANCE:
                log.warning(f"[CLOSE] BOT_INSTANCE no disponible")
                await q.edit_message_text("❌ Bot no disponible")
                return
            log.info(f"[CLOSE] Ejecutando try_close_trade para {sym}")
            ok = BOT_INSTANCE.try_close_trade(sym, reason="MANUAL_TG")
            log.info(f"[CLOSE] Resultado de cierre: {ok}")
            if ok:
                log.info(f"[CLOSE] ✅ {sym} cerrado exitosamente desde Telegram")
                await q.edit_message_text(
                    f"✅ <b>{sym}</b> cerrado.", parse_mode=ParseMode.HTML
                )
            else:
                log.warning(f"[CLOSE] ❌ No hay posición abierta para {sym} o error al cerrar")
                await q.edit_message_text(f"❌ Sin posición abierta en {sym}.")

        # ── Actualizar posición ───────────────────────────────────────────────
        elif data.startswith("refresh:"):
            sym = data[8:]
            log.info(f"[REFRESH] Solicitud de actualización - usuario: {user_id}, símbolo: {sym}")
            
            if not BOT_INSTANCE:
                log.error(f"[REFRESH] ERROR: BOT_INSTANCE no disponible")
                try:
                    await q.edit_message_text("❌ Bot no disponible")
                except Exception as e:
                    log.error(f"[REFRESH] Error al editar mensaje (bot no disponible): {e}")
                return
            
            log.debug(f"[REFRESH] Obteniendo posición {sym} del lock")
            with BOT_INSTANCE._lock:
                p = BOT_INSTANCE.open_positions.get(sym)
            
            if not p:
                log.warning(f"[REFRESH] Posición no encontrada o cerrada: {sym}")
                try:
                    await q.edit_message_text(f"📭 {sym} — posición cerrada.")
                except Exception as e:
                    log.error(f"[REFRESH] Error al editar mensaje (posición no exist): {e}")
                return
            
            try:
                log.debug(f"[REFRESH] Formateando detalles de {sym}")
                text = _fmt_position_detail(p, BOT_INSTANCE.client)
                text += f"\n<i>🕐 {time.strftime('%H:%M:%S')}</i>"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"❌ Cerrar {sym}", callback_data=f"close:{sym}"),
                    InlineKeyboardButton("🔄 Actualizar",   callback_data=f"refresh:{sym}"),
                ]])
                log.debug(f"[REFRESH] Editando mensaje para {sym}")
                await q.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
                log.info(f"[REFRESH] ✅ Actualización completada para {sym}")
            except Exception as e:
                # "message is not modified" — no hay cambio real, es normal
                log.debug(f"[REFRESH] Excepción al editar (normal si no hay cambios): {type(e).__name__}: {e}")

        # ── Stop live monitor ─────────────────────────────────────────────────
        elif data.startswith("stoplive:"):
            log.info(f"[STOPLIVE] Usuario {user_id} detuvo monitor live")
            await q.edit_message_text("⏹ Monitor en vivo detenido.")

        # ── Toggle notificación ───────────────────────────────────────────────
        elif data.startswith("notif:"):
            cat = data[6:]
            log.info(f"[NOTIF] Usuario {user_id} cambió preferencia: {cat}")

            try:
                if cat == "_all_on":
                    log.debug(f"[NOTIF] Activando TODAS las notificaciones")
                    for k in notify_prefs.DEFAULTS:
                        notify_prefs.set_pref(k, True)
                elif cat == "_all_off":
                    log.debug(f"[NOTIF] Desactivando TODAS las notificaciones")
                    for k in notify_prefs.DEFAULTS:
                        notify_prefs.set_pref(k, False)
                else:
                    log.debug(f"[NOTIF] Toggle categoría: {cat}")
                    notify_prefs.toggle(cat)
                
                log.debug(f"[NOTIF] Obteniendo estado actual de preferencias")
                # Redibujar el menú completo con estado actualizado
                prefs    = notify_prefs.get_all()
                active   = sum(1 for v in prefs.values() if v)
                inactive = len(prefs) - active

                rows = []
                for cat_k, enabled in prefs.items():
                    desc    = notify_prefs.DESCRIPTIONS.get(cat_k, cat_k)
                    btn_ico = "🔔" if enabled else "🔕"
                    rows.append([InlineKeyboardButton(
                        f"{btn_ico} {desc}",
                        callback_data=f"notif:{cat_k}"
                    )])
                rows.append([
                    InlineKeyboardButton("✅ Activar todo",    callback_data="notif:_all_on"),
                    InlineKeyboardButton("🔕 Desactivar todo", callback_data="notif:_all_off"),
                ])

                log.debug(f"[NOTIF] Editando mensaje con nuevo estado: {active} activas, {inactive} desactivadas")
                await q.edit_message_text(
                    f"<b>🔔 Notificaciones en tiempo real</b>\n"
                    f"{active} activas / {inactive} desactivadas\n\n"
                    f"Toca para activar/desactivar cada tipo:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(rows)
                )
                log.info(f"[NOTIF] ✅ Actualización completada: {active} activas")
            except Exception as e:
                log.error(f"[NOTIF] Error al procesar notificación: {type(e).__name__}: {e}")
                pass  # sin cambio real

    except Exception as e:
        log.error(f"[CALLBACK] ❌ ERROR NO CAPTURADO EN LÓGICA: {type(e).__name__}: {e}", exc_info=True)
        # Intentar notificar al usuario
        try:
            error_msg = f"❌ Error interno: {type(e).__name__}"
            log.debug(f"[CALLBACK] Intentando editar mensaje con error")
            await q.edit_message_text(text=error_msg)
        except Exception as edit_e:
            log.error(f"[CALLBACK] No se pudo enviar mensaje de error: {edit_e}")
            pass


# ── Mapa de handlers ──────────────────────────────────────────────────────────

HANDLER_MAP = {
    "start":     cmd_start,    "stop":      cmd_stop,
    "pause":     cmd_pause,    "resume":    cmd_resume,
    "scan":      cmd_scan,
    "status":    cmd_status,   "balance":   cmd_balance,
    "pos":       cmd_pos,      "live":      cmd_live,
    "pnl":       cmd_pnl,      "daily":     cmd_daily,
    "close":     cmd_close,    "closeall":  cmd_closeall,
    "perf":      cmd_perf,     "signals":   cmd_signals,
    "news":      cmd_news,     "fg":        cmd_fg,
    "ai":        cmd_ai,       "aihist":    cmd_aihist,
    "accuracy":  cmd_accuracy,
    "notifs":    cmd_notifs,
    "params":    cmd_params,   "set":       cmd_set,
    "lev":       cmd_lev,      "mode":      cmd_mode,
    "watchlist": cmd_watchlist,"add":       cmd_add,
    "remove":    cmd_remove,   "risk":      cmd_risk,
    "help":      cmd_help,
}


def is_paused() -> bool:
    return _paused


# ── post_init ─────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    try:
        commands = [BotCommand(cmd, desc) for cmd, desc in COMMAND_LIST]
        await app.bot.set_my_commands(commands)
        log.info(f"✅ {len(commands)} comandos registrados en BotFather")
    except Exception as e:
        log.error(f"set_my_commands: {e}")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_telegram_bot(bot_instance):
    log.info("[INIT] Iniciando Telegram bot controller...")
    if not TG_AVAILABLE:
        log.error("[INIT] ERROR: python-telegram-bot no instalado.")
        return
    if not TG_TOKEN:
        log.warning("[INIT] WARNING: TELEGRAM_BOT_TOKEN vacío — Telegram desactivado.")
        return

    set_bot(bot_instance)
    log.info(f"[INIT] BOT_INSTANCE asignado correctamente")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(_post_init)
        .build()
    )

    log.info(f"[INIT] Registrando {len(COMMAND_LIST)} comandos...")
    for cmd, _ in COMMAND_LIST:
        handler = HANDLER_MAP.get(cmd)
        if handler:
            app.add_handler(CommandHandler(cmd, handler))

    # Callback para botones inline
    log.info("[INIT] Registrando CallbackQueryHandler para botones inline")
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info(f"✅ Telegram bot iniciado — {len(COMMAND_LIST)} comandos + inline buttons")
    log.info("[INIT] ⏳ Polling iniciado - esperando mensajes y interacciones de usuarios...")
    try:
        app.run_polling()
    except Exception as e:
        log.error(f"[INIT] FATAL: Error en polling: {e}", exc_info=True)
        raise
