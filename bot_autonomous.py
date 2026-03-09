"""
bot_autonomous.py  v4 — Sistema de Supervivencia + Kill-Switch Gate
════════════════════════════════════════════════════════════════════
JERARQUÍA DE CORTACIRCUITOS — evaluados en _check_kill_switches()
ANTES de cualquier lógica de IA, en este orden exacto:

  KS-1  API Circuit Breaker    → 3 errores place_order en 5 min → pausa 15 min
  KS-2  Daily DD Kill-Switch   → PnL diario < -5 % → lock hasta 00:00 UTC
  KS-3  News Freeze Window     → ±30 min de evento macro HIGH_IMPACT
  KS-4  Strategy Cooldown      → 4 pérdidas × estrategia → 12 h
  KS-5  Max Total Exposure     → margen > 15 % balance → bloqueo

La IA (ai_filter.should_trade) se invoca DESPUÉS y NO puede saltarse
ninguno de los KS anteriores.

Estado persistente del CB: api_cb_state.json
Estado persistente del Risk: risk_state.json
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger("bot")

from bybit_client           import BybitClient
from news_engine            import NewsEngine
from analysis_engine_bybit  import (analyze_symbol, scan_best_opportunities,
                                     format_analysis_for_tg, ALL_TF)
from learning_engine        import LearningEngine
from risk_manager           import RiskManager
from ai_filter              import ai_filter
from tg_controller          import notify, notify_dev
from reason_codes           import (
    RC, ENTRY_MODE_TO_STRATEGY, VALID_STRATEGY_TYPES,
    API_CB_MAX_ERRORS, API_CB_WINDOW_SEC, API_CB_PAUSE_SEC,
)
import notify_prefs

# ── Config ─────────────────────────────────────────────────────────────────────
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY",   "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET","").strip()
PAPER_TRADING    = os.getenv("PAPER_TRADING","true").lower() in ("1","true","yes")
SCAN_INTERVAL_SEC= int(os.getenv("SCAN_INTERVAL_SEC",   "30"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL_SEC","10"))
AUTO_SCAN_ENABLED= os.getenv("AUTO_SCAN","true").lower() in ("1","true","yes")
MIN_VOLUME_USDT  = float(os.getenv("MIN_VOLUME_USDT","5000000"))
TG_TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TG_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID",  "").strip()

FIXED_WATCHLIST: List[str] = [
    s.strip() for s in
    os.getenv("WATCHLIST","BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
    if s.strip()
]
PROBLEMATIC_SYMBOLS: set = {"BARDUSDT","POWERUSDT"}

# Timeframe base por tipo de estrategia (minutos) — usado por el ATM Event-Driven
STRATEGY_TIMEFRAME_MIN: Dict[str, int] = {
    "FAST":          5,
    "NORMAL":       15,
    "INSTITUTIONAL": 240,
}

BOT_MAX_LEVERAGE = int(os.getenv("BOT_MAX_LEVERAGE","50"))
BOT_MAX_RISK_PCT = float(os.getenv("BOT_MAX_RISK_PCT","2.0"))
BOT_MIN_LEVERAGE = int(os.getenv("BOT_MIN_LEVERAGE","1"))

_API_CB_STATE_FILE = "api_cb_state.json"


# ══════════════════════════════════════════════════════════
#  KS-1: API CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════

class APICircuitBreaker:
    """
    Contador de errores consecutivos de la API de Bybit al abrir órdenes.

    Regla: si place_order devuelve retCode != 0 OR lanza excepción de red
    3 veces dentro de una ventana de 5 minutos → bot pausado 15 minutos.

    Estado completamente persistente en api_cb_state.json.
    Al reiniciar el bot, si el CB estaba activo y aún no expiró → sigue activo.
    """

    def __init__(self, tg_notifier=None) -> None:
        self.tg    = tg_notifier
        self._lock = threading.Lock()

        self._active:  bool        = False
        self._until:   float       = 0.0
        self._errors:  List[float] = []   # timestamps de errores recientes

        self._load()

    # ── Persistencia ──────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            with open(_API_CB_STATE_FILE, "w") as f:
                json.dump({
                    "active":   self._active,
                    "until":    self._until,
                    "errors":   self._errors,
                    "saved_at": int(time.time()),
                }, f)
        except Exception as e:
            log.error(f"Error guardando api_cb_state: {e}")

    def _load(self) -> None:
        if not os.path.exists(_API_CB_STATE_FILE):
            return
        try:
            with open(_API_CB_STATE_FILE) as f:
                d = json.load(f)
            self._active = bool(d.get("active", False))
            self._until  = float(d.get("until",  0.0))
            # Filtrar errores fuera de la ventana vigente
            now = time.time()
            self._errors = [
                float(ts) for ts in d.get("errors", [])
                if now - float(ts) < API_CB_WINDOW_SEC
            ]
            if self._active and self._until > time.time():
                rem = int(self._until - time.time())
                log.warning(
                    RC.fmt(RC.API_CIRCUIT_BREAKER_ACTIVATED,
                           source="state_restored", remaining_s=rem)
                )
            elif self._active:
                # El CB expiró mientras el bot estaba apagado → reset
                self._active = False
                self._save()
        except Exception as e:
            log.error(f"Error cargando api_cb_state: {e}")

    # ── API pública ────────────────────────────────────────────────────────────

    def is_open(self) -> Tuple[bool, float]:
        """
        Retorna (True, seconds_remaining) si el circuit está abierto (bloqueado).
        (False, 0.0) si el bot puede operar con normalidad.
        """
        with self._lock:
            if not self._active:
                return False, 0.0
            if time.time() >= self._until:
                self._lift()          # expiró → levantarlo
                return False, 0.0
            return True, self._until - time.time()

    def record_success(self) -> None:
        """Llamar tras cada place_order exitoso. Resetea el contador."""
        with self._lock:
            if self._errors:
                self._errors.clear()
                self._save()

    def record_error(self, symbol: str = "", retcode: int = 0,
                     errmsg: str = "") -> bool:
        """
        Registra un error de la API.
        Retorna True si este error activó el circuit breaker.
        """
        now = time.time()
        with self._lock:
            # Purgar errores fuera de la ventana
            self._errors = [
                ts for ts in self._errors
                if now - ts < API_CB_WINDOW_SEC
            ]
            self._errors.append(now)
            count = len(self._errors)

            log.warning(
                RC.fmt(RC.API_CONSECUTIVE_ERRORS,
                       symbol=symbol, count=count, max=API_CB_MAX_ERRORS,
                       retcode=retcode, msg=errmsg[:80] if errmsg else "")
            )

            if count >= API_CB_MAX_ERRORS:
                self._activate()
                return True
            self._save()
            return False

    # ── Internos ───────────────────────────────────────────────────────────────

    def _activate(self) -> None:
        """Abre el circuit breaker. Llamar bajo _lock."""
        self._active = True
        self._until  = time.time() + API_CB_PAUSE_SEC
        self._errors = []
        self._save()

        pause_min = API_CB_PAUSE_SEC // 60
        reactivates = time.strftime("%H:%M UTC", time.gmtime(self._until))
        log.critical(
            RC.fmt(RC.API_CIRCUIT_BREAKER_ACTIVATED,
                   errors=API_CB_MAX_ERRORS,
                   window_min=API_CB_WINDOW_SEC // 60,
                   pause_min=pause_min,
                   reactivates=reactivates)
        )
        if self.tg:
            self.tg.send(
                RC.tg(RC.API_CIRCUIT_BREAKER_ACTIVATED,
                      errores=API_CB_MAX_ERRORS,
                      ventana=f"{API_CB_WINDOW_SEC//60} min",
                      pausa=f"{pause_min} min",
                      reanuda=reactivates)
            )

    def _lift(self) -> None:
        """Cierra el circuit breaker. Llamar bajo _lock."""
        self._active = False
        self._until  = 0.0
        self._errors = []
        self._save()
        log.info(RC.fmt(RC.API_CIRCUIT_BREAKER_LIFTED))
        if self.tg:
            self.tg.send(RC.tg(RC.API_CIRCUIT_BREAKER_LIFTED))

    def get_status(self) -> Dict:
        is_open, remaining = self.is_open()
        return {
            "active":        is_open,
            "remaining_s":   int(remaining),
            "until":         self._until,
            "recent_errors": len(self._errors),
            "max_errors":    API_CB_MAX_ERRORS,
            "window_sec":    API_CB_WINDOW_SEC,
            "pause_sec":     API_CB_PAUSE_SEC,
        }


# ══════════════════════════════════════════════════════════
#  NOTIFICADOR TELEGRAM
# ══════════════════════════════════════════════════════════

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token   = token
        self.chat_id = chat_id
        self._queue: List[str] = []
        self._lock   = threading.Lock()
        self._active = bool(token and chat_id)
        if self._active:
            threading.Thread(
                target=self._worker, daemon=True, name="tg-notify"
            ).start()
            log.info("TelegramNotifier activo")
        else:
            log.info("TelegramNotifier desactivado (sin TOKEN o CHAT_ID)")

    def send(self, text: str) -> None:
        if not self._active:
            return
        with self._lock:
            self._queue.append(text)

    def send_direct(self, text: str) -> bool:
        import requests as req
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r    = req.post(url, json={"chat_id":self.chat_id,"text":text[:4000],
                                        "parse_mode":"HTML"}, timeout=10)
            data = r.json()
            if data.get("ok"):
                log.info(f"✅ Telegram OK → chat_id={self.chat_id}")
                return True
            log.error(
                RC.fmt(RC.API_NETWORK_ERROR,
                       detail=data.get("description",""),
                       chat_id=self.chat_id)
            )
            return False
        except Exception as e:
            log.error(RC.fmt(RC.API_NETWORK_ERROR, detail=str(e)))
            return False

    def _worker(self) -> None:
        import requests as req
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            with self._lock:
                msgs = self._queue[:]
                self._queue.clear()
            for text in msgs:
                try:
                    r    = req.post(url, json={"chat_id":self.chat_id,
                                               "text":text[:4000],
                                               "parse_mode":"HTML"}, timeout=10)
                    data = r.json()
                    if not data.get("ok"):
                        log.error(
                            RC.fmt(RC.API_NETWORK_ERROR,
                                   detail=data.get("description",""))
                        )
                    time.sleep(0.35)
                except Exception as e:
                    log.warning(RC.fmt(RC.API_NETWORK_ERROR, detail=str(e)))
            time.sleep(0.5)


# ══════════════════════════════════════════════════════════
#  PROXY DE NOTICIAS — gate de notify_prefs("news")
# ══════════════════════════════════════════════════════════

class _NewsTelegramProxy:
    """
    Proxy que envuelve TelegramNotifier para las notificaciones del NewsEngine.
    Sólo envía si notify_prefs.is_enabled("news") es True.

    Esto evita que el NewsEngine haga spam con alertas de noticias cuando
    el usuario ha desactivado esa categoría desde el menú de Telegram.
    """
    def __init__(self, notifier: TelegramNotifier) -> None:
        self._n = notifier

    @property
    def _active(self) -> bool:
        return self._n._active

    def send(self, text: str) -> None:
        if notify_prefs.is_enabled("news"):
            self._n.send(text)

    def send_direct(self, text: str) -> bool:
        # send_direct se usa solo para mensajes críticos de inicio
        return self._n.send_direct(text)


# ══════════════════════════════════════════════════════════
#  BOT AUTÓNOMO v4
# ══════════════════════════════════════════════════════════

class AutonomousBot:
    def __init__(self) -> None:
        if not BYBIT_API_KEY or not BYBIT_API_SECRET:
            raise ValueError(
                "BYBIT_API_KEY y BYBIT_API_SECRET requeridos en .env"
            )

        self.client   = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET,
                                    paper=PAPER_TRADING)
        self.learner  = LearningEngine()
        self.tg       = TelegramNotifier(TG_TOKEN, TG_CHAT_ID)

        # ── Instanciar Risk Manager y API CB (con tg ya listo) ─────────────────
        self.risk_mgr = RiskManager(self.learner)
        self.api_cb   = APICircuitBreaker(tg_notifier=self.tg)
        # ── NewsEngine recibe un proxy que respeta notify_prefs("news") ─────────
        self.news     = NewsEngine(
            telegram_notifier=_NewsTelegramProxy(self.tg),
            scan_interval=120
        )

        self.open_positions: Dict[str, Dict] = {}
        self.cooldowns:      Dict[str, float] = {}
        self.running = False
        self._lock   = threading.Lock()

        self._sync_positions()

        mode = "🟡 PAPER" if PAPER_TRADING else "🔴 REAL"
        ks   = (
            f"🛡 Kill-Switches:\n"
            f"  CB: {API_CB_MAX_ERRORS} err/{API_CB_WINDOW_SEC//60}min→{API_CB_PAUSE_SEC//60}min | "
            f"DD: {self.risk_mgr.DAILY_DD_KILL_PCT}% | "
            f"Exp: {self.risk_mgr.MAX_EXPOSURE_PCT}% | "
            f"Strat: 4×12h | News: ±30min"
        )
        msg = (
            f"🤖 <b>[{RC.SYSTEM_BOT_STARTED}] — {mode}</b>\n"
            f"Watchlist: {', '.join(FIXED_WATCHLIST)}\n"
            f"Scan: {SCAN_INTERVAL_SEC}s | Monitor: {MONITOR_INTERVAL}s\n"
            f"Lev: {BOT_MIN_LEVERAGE}x–{BOT_MAX_LEVERAGE}x | Risk: {BOT_MAX_RISK_PCT}%\n"
            f"{ks}"
        )
        # ── TASK 3: SYSTEM_BOT_STARTED → "risk" ──────────────────────────────
        if self.tg._active and notify_prefs.is_enabled("risk"):
            ok = self.tg.send_direct(msg)
            if not ok:
                log.error(
                    RC.fmt(RC.API_NETWORK_ERROR,
                           detail="Telegram inicial — verifica TELEGRAM_CHAT_ID")
                )
        log.info(RC.fmt(RC.SYSTEM_BOT_STARTED, mode=mode,
                         watchlist=",".join(FIXED_WATCHLIST)))

    # ══════════════════════════════════════════════════════
    #  KILL-SWITCH GATE — evaluado PRIMERO, antes que la IA
    # ══════════════════════════════════════════════════════

    def _check_kill_switches(
        self,
        symbol:        str,
        score:         float,
        strategy_type: str,
    ) -> Tuple[bool, str]:
        """
        Evalúa TODOS los kill-switches en orden de precedencia.
        Retorna (True, "") si se puede operar, (False, reason_msg) si no.

        Este método es la ÚNICA puerta de entrada para abrir trades.
        La IA se llama DESPUÉS de pasar todos estos checks.

        KS-1  API Circuit Breaker
        KS-2  Daily Drawdown (y reset UTC)     ┐
        KS-3  News Freeze                      ├─ delegados a risk_mgr.can_open()
        KS-4  Strategy Cooldown                │
        KS-5  Max Total Exposure               ┘
        """
        # ── KS-1: API Circuit Breaker ──────────────────────────────────────────
        cb_open, cb_rem = self.api_cb.is_open()
        if cb_open:
            return False, RC.fmt(
                RC.TRADE_BLOCKED_CIRCUIT_BREAKER,
                symbol=symbol,
                remaining_min=int(cb_rem // 60)
            )

        # ── KS-2 … KS-5: delegados al RiskManager ─────────────────────────────
        balance = self._get_balance()
        with self._lock:
            positions_snapshot = dict(self.open_positions)

        freeze_active, freeze_evt = self.news.is_news_freeze_active()

        can, reason = self.risk_mgr.can_open(
            symbol         = symbol,
            score          = score,
            balance        = balance,
            open_positions = positions_snapshot,
            strategy_type  = strategy_type,
            news_freeze    = freeze_active,
        )
        return can, reason

    # ══════════════════════════════════════════════════════
    #  SINCRONIZACIÓN DE POSICIONES
    # ══════════════════════════════════════════════════════

    def _sync_positions(self) -> None:
        try:
            new_pos: Dict[str, Dict] = {}
            for p in self.client.get_positions():
                try:
                    if float(p.get("size", 0)) == 0:
                        continue
                except Exception:
                    continue
                sym = p["symbol"]
                if sym in PROBLEMATIC_SYMBOLS:
                    continue
                side = "LONG" if p["side"] == "Buy" else "SHORT"
                ep   = float(p.get("avgPrice", 0))

                # ── Recuperar estado local existente para este símbolo ─────────
                # Leer bajo lock para evitar race con el monitor
                with self._lock:
                    existing: Dict = self.open_positions.get(sym, {})

                # ── open_ts: prioridad 1 local → 2 Bybit createdTime → 3 now ──
                # Bybit devuelve createdTime en milisegundos como string
                _created_ms = p.get("createdTime")
                _bybit_ts: Optional[int] = None
                if _created_ms:
                    try:
                        _bybit_ts = int(int(_created_ms) / 1000)
                    except (ValueError, TypeError):
                        pass
                open_ts = (
                    existing.get("open_ts")      # 1º: reloj original del bot
                    or _bybit_ts                 # 2º: timestamp real de Bybit
                    or int(time.time())          # 3º: fallback
                )

                # ── tf_minutes: leer de existing si existe, calcular si no ─────
                st_existing  = existing.get("strategy_type", "NORMAL")
                tf_m_default = STRATEGY_TIMEFRAME_MIN.get(st_existing, 15)
                tf_m         = existing.get("tf_minutes", tf_m_default)

                # ── next_eval_ts: preservar si existe, programar si es nueva ───
                next_eval_ts = (
                    existing.get("next_eval_ts")
                    or self._get_next_bar_close_ts(tf_m)
                )

                new_pos[sym] = {
                    # ── Campos que siempre vienen de Bybit (fuente de verdad) ──
                    "symbol":       sym,
                    "side":         side,
                    "entry_price":  ep,
                    "qty":          float(p.get("size", 0)),
                    "leverage":     int(float(p.get("leverage", 10))),
                    "tp":           float(p.get("takeProfit", 0)) or None,
                    "sl":           float(p.get("stopLoss",   0)) or None,

                    # ── Campos con fusión local → Bybit → default ─────────────
                    "trade_id":     existing.get("trade_id", f"sync_{sym}"),
                    "open_ts":      open_ts,

                    # ── Campos de memoria del bot: preservar si existen ────────
                    # Si la posición es nueva (existing={}) se usan los defaults
                    "strategy_type":  existing.get("strategy_type",  "NORMAL"),
                    "peak_price":     existing.get("peak_price",     ep),
                    "atr":            existing.get("atr"),
                    "ai_decision":    existing.get("ai_decision"),

                    # ── Campos ATM Event-Driven: preservar intactos ────────────
                    "tf_minutes":           tf_m,
                    "next_eval_ts":         next_eval_ts,
                    "last_eval_price":      existing.get("last_eval_price",      ep),
                    "sl_at_breakeven":      existing.get("sl_at_breakeven",      False),
                    "last_news_alert_ts":   existing.get("last_news_alert_ts",   0),
                    # Campos de ciclo de vida adicionales (forward-compat)
                    "has_taken_partial":    existing.get("has_taken_partial",    False),
                    "sl_moved_to_be":       existing.get("sl_moved_to_be",       False),
                }

            old_keys = set(self.open_positions.keys())
            new_keys = set(new_pos.keys())

            for sym in new_keys - old_keys:
                pos = new_pos[sym]
                log.info(
                    f"📡 [{RC.RISK_STATE_LOADED}] "
                    f"pos detectada: {sym} {pos['side']} @ {pos['entry_price']}  "
                    f"open_ts={pos['open_ts']}  tf={pos['tf_minutes']}m"
                )
                fake = {
                    "composite_score":0.0,"confidence":0.5,
                    "signal":pos["side"],"entry_mode":"SYNCED",
                    "atr":0,"squeeze":False,"vol_spike":False,
                    "tf_details":{},"smc_summary":{},
                    "smc_sweep":False,"smc_ob_hit":False,
                    "smc_fvg_fill":False,"smc_vwap_retest":False,
                }
                self.learner.record_open(
                    pos["trade_id"], sym, pos["side"],
                    pos["entry_price"], pos["qty"], pos["leverage"],
                    pos["tp"] or 0, pos["sl"] or 0, fake
                )
                self.risk_mgr.on_open(sym)

            for sym in old_keys - new_keys:
                pos = self.open_positions[sym]
                pnl = 0.0
                try:
                    closed = self.client.get_closed_pnl(sym, limit=3)
                    if closed:
                        pnl = float(closed[0].get("closedPnl", 0))
                except Exception:
                    pass
                reason   = RC.TRADE_CLOSED_TP if pnl >= 0 else RC.TRADE_CLOSED_SL
                trade_id = pos.get("trade_id", f"sync_{sym}")
                st = self.learner.record_close(
                    trade_id,
                    self.client.get_mark_price(sym), pnl, reason
                ) or pos.get("strategy_type","NORMAL")
                self.risk_mgr.on_close(sym, pnl, strategy_type=st)
                result_e = "✅" if pnl >= 0 else "❌"
                # ── "trades": TRADE_CLOSED_EXTERNAL detectado en sync ─────────
                if notify_prefs.is_enabled("trades"):
                    self.tg.send(
                        f"{result_e} <b>[{RC.TRADE_CLOSED_EXTERNAL}]</b>\n"
                        f"Par: <b>{sym}</b>  Razón: {reason}\n"
                        f"PnL: <code>{pnl:+.2f} USDT</code>"
                    )
                log.warning(
                    RC.fmt(RC.TRADE_CLOSED_EXTERNAL,
                           symbol=sym, pnl=f"{pnl:+.2f}", reason=reason)
                )
                with self._lock:
                    self.open_positions.pop(sym, None)

            with self._lock:
                self.open_positions.update(new_pos)

            if old_keys != new_keys:
                log.info(
                    f"Sync posiciones: "
                    f"{sorted(self.open_positions.keys()) or 'ninguna'}"
                )
        except Exception as e:
            log.error(f"_sync_positions: {e}")

    # ══════════════════════════════════════════════════════
    #  UTILIDADES
    # ══════════════════════════════════════════════════════

    def _get_next_bar_close_ts(self, timeframe_minutes: int) -> int:
        """Calcula el timestamp exacto del próximo cierre de vela para el ATM."""
        now    = int(time.time())
        tf_sec = timeframe_minutes * 60
        if tf_sec <= 0:
            tf_sec = 900  # fallback de seguridad a 15 min
        current_bar_start = (now // tf_sec) * tf_sec
        return current_bar_start + tf_sec

    def _in_cooldown(self, sym: str) -> bool:
        return time.time() < self.cooldowns.get(sym, 0)

    def _set_cooldown(self, sym: str) -> None:
        self.cooldowns[sym] = (
            time.time() + self.learner.params.get("cooldown_seconds", 60)
        )

    def _get_balance(self) -> float:
        try:
            return self.client.get_usdt_balance()
        except Exception as e:
            log.error(RC.fmt(RC.API_NETWORK_ERROR, detail=f"get_balance: {e}"))
            return 0.0

    def _resolve_leverage(
        self, symbol: str, ai_suggested: int
    ) -> Tuple[int, str]:
        lev      = max(BOT_MIN_LEVERAGE, ai_suggested)
        info     = self.client.get_instrument_info(symbol)
        bybit_max= info["max_leverage"]
        final    = min(lev, bybit_max, BOT_MAX_LEVERAGE)
        final    = max(BOT_MIN_LEVERAGE, final)
        parts: List[str] = []
        if ai_suggested > bybit_max:        parts.append(f"bybit_max={bybit_max}")
        if ai_suggested > BOT_MAX_LEVERAGE: parts.append(f"bot_cap={BOT_MAX_LEVERAGE}")
        return final, (
            f"lev: AI={ai_suggested}x → final={final}x"
            + (f" (limitado por {', '.join(parts)})" if parts else "")
        )

    def _calc_qty(
        self, symbol: str, balance: float, mark: float,
        atr_v: float, leverage: int
    ) -> Tuple[float, str]:
        if balance <= 0 or mark <= 0:
            return 0.0, RC.fmt(RC.TRADE_BLOCKED_QTY_INVALID,
                                detail="balance o mark_price=0")
        risk_usdt       = balance * (BOT_MAX_RISK_PCT / 100.0)
        raw_qty         = (risk_usdt * leverage) / mark
        learner_qty     = self.learner.calculate_position_size(
            balance, mark, atr_v, leverage
        )
        qty_before_safe = (
            min(raw_qty, learner_qty) if learner_qty > 0 else raw_qty
        )
        safe, err = self.client.safe_qty(symbol, qty_before_safe)
        if safe <= 0:
            return 0.0, RC.fmt(RC.TRADE_BLOCKED_QTY_INVALID,
                                raw=f"{qty_before_safe:.6f}", err=err)
        return safe, (
            f"qty: risk={risk_usdt:.2f} USDT  lev={leverage}x  "
            f"raw={qty_before_safe:.6f} → safe={safe:.6f}"
        )

    # ══════════════════════════════════════════════════════
    #  ABRIR TRADE
    # ══════════════════════════════════════════════════════

    def try_open_trade(self, analysis: Dict) -> bool:
        sym        = analysis["symbol"]
        sig        = analysis["signal"]
        # ── Scores arquitectura v5 (con backward-compat) ──────────────────────
        score      = analysis.get("direction_score",
                         analysis.get("composite_score", 0))   # v5 alias
        conf       = analysis["confidence"]
        mark       = analysis["mark_price"]
        atr_v      = analysis.get("atr") or 0
        entry_mode = analysis.get("entry_mode", "STANDARD")
        threshold  = analysis.get("threshold", 4.0)
        strategy_type = ENTRY_MODE_TO_STRATEGY.get(entry_mode, "NORMAL")

        # ── Campos Prop-Firm v5 ────────────────────────────────────────────────
        quality_score = float(analysis.get("trade_quality_score") or 0.0)
        regime        = analysis.get("regime", "UNKNOWN")

        # ── Pre-filtros baratos (sin IO) ───────────────────────────────────────
        if sym in PROBLEMATIC_SYMBOLS:
            return False
        if self._in_cooldown(sym):
            log.debug(RC.fmt(RC.TRADE_BLOCKED_COOLDOWN, symbol=sym))
            return False
        if sig == "FLAT":
            return False

        # ══════════════════════════════════════════════════════════════════
        #  ▼▼▼  BLOQUEADOR DURO: TRADE QUALITY (Prop-Firm gate)  ▼▼▼
        # ══════════════════════════════════════════════════════════════════
        # Se evalúa ANTES que los Kill-Switches para no desperdiciar CPU.
        # Un score < 40 implica SL absurdo, sin volumen o precio muy lejos
        # del VWAP — ninguna estrategia institucional operaría aquí.
        if quality_score < 40.0:
            log.info(
                RC.fmt(RC.BLOCKED_LOW_QUALITY,
                       symbol=sym, quality=f"{quality_score:.1f}",
                       regime=regime)
            )
            return False
        # ══════════════════════════════════════════════════════════════════
        #  ▲▲▲  FIN BLOQUEADOR DURO QUALITY  ▲▲▲
        # ══════════════════════════════════════════════════════════════════

        # ══════════════════════════════════════════════════════════════════
        #  ▼▼▼  KILL-SWITCH GATE — PRIMERO, ANTES QUE CUALQUIER LÓGICA  ▼▼▼
        # ══════════════════════════════════════════════════════════════════
        can, block_reason = self._check_kill_switches(sym, score, strategy_type)
        if not can:
            log.warning(block_reason)
            # Notificar a Telegram solo los KS críticos (no los técnicos)
            _critical_ks = {
                RC.TRADE_BLOCKED_DAILY_DD,
                RC.TRADE_BLOCKED_CIRCUIT_BREAKER,
                RC.TRADE_BLOCKED_NEWS_WINDOW,
                RC.TRADE_BLOCKED_STRATEGY_COOLDOWN,
                RC.TRADE_BLOCKED_MAX_EXPOSURE,
            }
            # ── "risk": alertas de Kill-Switches críticos ─────────────────────
            if (any(c in block_reason for c in _critical_ks)
                    and notify_prefs.is_enabled("risk")):
                self.tg.send(
                    f"🛡 <b>Kill-Switch activo</b>\n"
                    f"Par: {sym}  {sig}  [{strategy_type}]\n"
                    f"<code>{block_reason[:300]}</code>"
                )
            return False
        # ══════════════════════════════════════════════════════════════════
        #  ▲▲▲  FIN KILL-SWITCH GATE  ▲▲▲
        # ══════════════════════════════════════════════════════════════════

        # ── Filtros técnicos ───────────────────────────────────────────────────
        params = self.learner.get_params()

        if abs(score) < threshold:
            log.debug(
                RC.fmt(RC.TRADE_BLOCKED_SCORE_LOW,
                       symbol=sym, score=f"{score:+.2f}",
                       threshold=threshold, mode=entry_mode)
            )
            return False
        if conf < params.get("min_confidence", 0.40):
            log.debug(RC.fmt(RC.TRADE_BLOCKED_CONF_LOW,
                              symbol=sym, conf=f"{conf:.0%}"))
            return False
        with self._lock:
            if sym in self.open_positions:
                return False
            if len(self.open_positions) >= params.get("max_open_positions", 3):
                log.debug(RC.fmt(RC.TRADE_BLOCKED_MAX_POSITIONS,
                                  symbol=sym,
                                  open=len(self.open_positions)))
                return False

        ok, reason = self.learner.should_trade_symbol(sym)
        if not ok:
            log.info(reason)
            return False

        # ── Filtro de noticias (sentimiento) ──────────────────────────────────
        news_bias = self.news.get_news_bias(sym)
        if news_bias["should_block"]:
            log.info(
                RC.fmt(RC.TRADE_BLOCKED_NEWS_SENTIMENT,
                       symbol=sym,
                       score=f"{news_bias['news_score']:+.2f}")
            )
            return False

        news_adj       = news_bias["news_score"] * 0.5 + news_bias["fg_adj"] * 0.3
        adjusted_score = score + news_adj
        if sig == "LONG"  and news_bias["direction"] == "BEARISH":
            adjusted_score -= 0.5
        if sig == "SHORT" and news_bias["direction"] == "BULLISH":
            adjusted_score -= 0.5
        if abs(adjusted_score) < threshold:
            log.info(
                RC.fmt(RC.TRADE_BLOCKED_SCORE_ADJ,
                       symbol=sym,
                       original=f"{score:.2f}",
                       adjusted=f"{adjusted_score:.2f}",
                       threshold=threshold)
            )
            return False

        # ── FILTRO DE IA ← se llama DESPUÉS de todos los Kill-Switches ────────
        _pending_id = str(uuid.uuid4())[:8]
        ai_decision = ai_filter.should_trade(
            analysis,
            symbol_stats=self.learner.get_symbol_stats(sym),
            news_bias=news_bias,
            recent_news=self.news.get_recent_news(5),
            trade_id=_pending_id,
        )

        approve   = bool(ai_decision.get("approve", True))
        ai_conf   = float(ai_decision.get("confidence", 0.5))
        ai_lev    = int(ai_decision.get("recommended_leverage", 10))
        reasoning = ai_decision.get("reasoning", "")
        smc_anal  = ai_decision.get("smc_analysis", "")
        news_imp  = ai_decision.get("news_impact", "NEUTRAL")

        if not approve and ai_conf >= 0.55:
            log.info(
                RC.fmt(RC.TRADE_BLOCKED_AI_REJECTION,
                       symbol=sym, sig=sig,
                       conf=f"{ai_conf:.0%}",
                       reason=reasoning[:80])
            )
            notify("ai_decisions",
                   f"🤖 <b>[{RC.TRADE_BLOCKED_AI_REJECTION}]</b>\n"
                   f"Par: {sym}  {sig}\n"
                   f"Score: {score:+.2f}  Conf: {conf:.0%}\n"
                   f"SMC: {smc_anal}\nNoticias: {news_imp}\n"
                   f"Razón IA: {reasoning}")
            return False

        # ── Leverage final (triple capa) ───────────────────────────────────────
        final_leverage, lev_log = self._resolve_leverage(sym, ai_lev)
        log.info(f"[{sym}] {lev_log}")

        # ── Balance y qty ──────────────────────────────────────────────────────
        balance = self._get_balance()
        if balance < 5:
            log.warning(RC.fmt(RC.TRADE_BLOCKED_BALANCE_LOW,
                                symbol=sym, balance=f"{balance:.2f}"))
            return False

        qty, qty_log = self._calc_qty(sym, balance, mark, atr_v, final_leverage)
        if qty <= 0:
            log.warning(qty_log)
            return False
        log.info(f"[{sym}] {qty_log}")

        # ── TP / SL ────────────────────────────────────────────────────────────
        tp_mult = params.get("tp_atr_mult", 2.5)
        sl_mult = params.get("sl_atr_mult", 1.2)
        tp: Optional[float] = None
        sl: Optional[float] = None
        if atr_v > 0:
            if sig == "LONG":
                tp = self.client.safe_price(sym, mark + atr_v * tp_mult)
                sl = self.client.safe_price(sym, mark - atr_v * sl_mult)
            else:
                tp = self.client.safe_price(sym, mark - atr_v * tp_mult)
                sl = self.client.safe_price(sym, mark + atr_v * sl_mult)
        else:
            tp = analysis.get("tp")
            sl = analysis.get("sl")

        # ── Set leverage ───────────────────────────────────────────────────────
        lev_resp = self.client.set_leverage(sym, final_leverage)
        if lev_resp.get("retCode", -1) not in (0, 110043):
            log.warning(
                RC.fmt(RC.API_LEVERAGE_ERROR,
                       symbol=sym,
                       retcode=lev_resp.get("retCode"),
                       msg=lev_resp.get("retMsg",""))
            )

        # ── PLACE ORDER — con registro en APICircuitBreaker ────────────────────
        side_str = "Buy" if sig == "LONG" else "Sell"
        try:
            resp = self.client.place_order(sym, side_str, qty, tp=tp, sl=sl)
            rc   = resp.get("retCode", -1)
        except Exception as net_err:
            # Error de red → registrar en CB
            triggered = self.api_cb.record_error(
                symbol=sym, retcode=-1, errmsg=str(net_err)
            )
            log.error(
                RC.fmt(RC.API_NETWORK_ERROR,
                       symbol=sym, detail=str(net_err)[:100])
            )
            if triggered:
                notify_dev(
                    f"🚨 <b>[{RC.API_CIRCUIT_BREAKER_ACTIVATED}]</b>\n"
                    f"Error red: {net_err}"
                )
            return False

        if rc != 0:
            err_msg   = resp.get("retMsg", "")
            triggered = self.api_cb.record_error(
                symbol=sym, retcode=rc, errmsg=err_msg
            )
            log.error(
                RC.fmt(RC.API_ORDER_ERROR,
                       symbol=sym, retcode=rc, msg=err_msg[:100])
            )
            notify_dev(
                f"❌ <b>[{RC.API_ORDER_ERROR}]</b>\n"
                f"{sym} [{rc}] {err_msg}\n"
                f"qty={qty} lev={final_leverage}x"
                + (f"\n🚨 CIRCUIT BREAKER ACTIVADO" if triggered else "")
            )
            return False

        # ── Orden exitosa → resetear contador del CB ───────────────────────────
        self.api_cb.record_success()
        log.info(
            RC.fmt(RC.API_ORDER_SUCCESS,
                   symbol=sym, side=side_str, qty=qty,
                   lev=final_leverage,
                   order_id=resp.get("result",{}).get("orderId","?"))
        )

        # ── Registrar posición ─────────────────────────────────────────────────
        pos_data: Dict[str, Any] = {
            "trade_id":       _pending_id,
            "symbol":         sym,
            "side":           sig,
            "entry_price":    mark,
            "qty":            qty,
            "leverage":       final_leverage,
            "tp":             tp,
            "sl":             sl,
            "initial_sl":     sl,    # ← SL original para calcular 1R en ATM
            "open_ts":        int(time.time()),
            "peak_price":     mark,
            "atr":            atr_v,
            "ai_decision":    ai_decision,
            "strategy_type":  strategy_type,
            # ── Campos ATM híbrido ─────────────────────────────────────────────
            "sl_moved_to_be":    False,   # se activa cuando el SL llega a BE
            "has_taken_partial": False,   # se activa tras el cierre parcial 1R
        }
        with self._lock:
            self.open_positions[sym] = pos_data

        self.learner.record_open(
            _pending_id, sym, sig, mark, qty,
            final_leverage, tp or 0, sl or 0, analysis
        )
        self.risk_mgr.on_open(sym)
        self._set_cooldown(sym)

        # ── Notificación Telegram ──────────────────────────────────────────────
        rr = (abs((tp - mark) / (mark - sl))
              if (tp and sl and abs(mark - sl) > 0) else 0)
        aligned_txt = (
            "✅ Perfectamente alineado" if analysis.get("aligned")
            else "⚠️ Parcialmente alineado"
        )
        smc_badges: List[str] = []
        if analysis.get("smc_sweep"):       smc_badges.append("🌊 Sweep")
        if analysis.get("smc_ob_hit"):      smc_badges.append("🧱 OB")
        if analysis.get("smc_fvg_fill"):    smc_badges.append("🪟 FVG")
        if analysis.get("smc_vwap_retest"): smc_badges.append("🎯 VWAP")

        fg     = news_bias["fear_greed"]
        fg_lbl = news_bias["fg_label"]
        news_d = news_bias["direction"]
        news_e = "🟢" if news_d=="BULLISH" else ("🔴" if news_d=="BEARISH" else "⚪")
        risk_s = self.risk_mgr.get_status()
        cb_s   = self.api_cb.get_status()

        regime_emoji = {
            "TREND_UP":"📈","TREND_DOWN":"📉","RANGE":"↔️","EXPANSION":"💥"
        }.get(regime, "❓")

        # ── "trades": TRADE ABIERTO ───────────────────────────────────────────
        if notify_prefs.is_enabled("trades"):
            self.tg.send(
                f"{'🟢' if sig=='LONG' else '🔴'} <b>TRADE ABIERTO</b>\n"
                f"Par: <b>{sym}</b>  {sig}  x{final_leverage}  [{strategy_type}]\n"
                f"Entrada: <code>{mark:.4f}</code>  Qty: {qty}\n"
                f"TP: <code>{tp}</code>  SL: <code>{sl}</code>\n"
                f"R:R ≈ 1:{rr:.1f}\n"
                f"Score: {score:+.2f}  Conf: {conf:.0%}  [{entry_mode}]\n"
                f"Quality: {quality_score:.0f}/100  Régimen: {regime_emoji} {regime}\n"
                f"{aligned_txt}\n"
                f"SMC: {' '.join(smc_badges) if smc_badges else 'sin setup'}\n"
                f"🤖 IA: {smc_anal[:60]}\n"
                f"   Noticias: {news_imp} | {news_e} {news_d}\n"
                f"   Leverage IA={ai_lev}x → final={final_leverage}x\n"
                f"F&G: {fg} — {fg_lbl} | Balance: {balance:.2f} USDT\n"
                f"📊 DD hoy: {risk_s['daily_pnl']:+.2f} | "
                f"CB: {'✅' if not cb_s['active'] else '🔴'}"
            )

        if notify_prefs.is_enabled("analysis"):
            self.tg.send(format_analysis_for_tg(analysis))

        if notify_prefs.is_enabled("ai_decisions"):
            ai_s = ai_filter.get_stats()
            if ai_s["calls"] > 0:
                self.tg.send(
                    f"🤖 <b>AI Risk Manager</b>\n"
                    f"✅ {ai_s['approved']} aprobados  "
                    f"🚫 {ai_s['rejected']} rechazados\n"
                    f"Tasa: {ai_s['approval_rate']:.1f}%  "
                    f"Tiempo: {ai_s['avg_ms']}ms"
                )

        log.info(
            f"✅ ABIERTO {sym} {sig} @ {mark:.4f}  "
            f"TP={tp}  SL={sl}  qty={qty}  lev={final_leverage}x  "
            f"score={score:.2f}  quality={quality_score:.0f}  "
            f"regime={regime}  strategy={strategy_type}  [AI_lev={ai_lev}x]"
        )
        return True

    # ══════════════════════════════════════════════════════
    #  CERRAR TRADE
    # ══════════════════════════════════════════════════════

    def try_close_trade(
        self, sym: str,
        reason: str              = RC.TRADE_CLOSED_MANUAL,
        pnl_override: Optional[float] = None,
    ) -> bool:
        try:
            real      = self.client.get_positions(sym)
            real_size = sum(float(p.get("size", 0)) for p in real)
        except Exception:
            real_size = None

        with self._lock:
            pos = self.open_positions.get(sym)

        if real_size == 0:
            log.warning(
                RC.fmt(RC.TRADE_CLOSED_POSITION_GHOST,
                       symbol=sym, action="limpiando local")
            )
            with self._lock:
                self.open_positions.pop(sym, None)
            return False

        if not pos:
            log.warning(f"No hay posición local en {sym}")
            return False

        try:
            self.client.cancel_all_orders(sym)
            resp = self.client.close_position(sym, pos["side"], pos["qty"])
            rc   = resp.get("retCode", -1) if isinstance(resp, dict) else -1
            if rc == 110017:
                log.warning(
                    RC.fmt(RC.TRADE_CLOSED_POSITION_GHOST,
                           symbol=sym, code=110017)
                )
                with self._lock:
                    self.open_positions.pop(sym, None)
                return False
            if rc != 0:
                log.error(
                    RC.fmt(RC.API_ORDER_ERROR,
                           symbol=sym, retcode=rc,
                           msg=resp.get("retMsg","") if isinstance(resp,dict) else "")
                )
                return False
        except Exception as e:
            log.error(RC.fmt(RC.API_NETWORK_ERROR, detail=f"close {sym}: {e}"))
            return False

        mark = self.client.get_mark_price(sym)
        if pnl_override is not None:
            pnl = pnl_override
        else:
            entry = pos["entry_price"]
            pnl   = (
                (mark - entry) if pos["side"] == "LONG"
                else (entry - mark)
            ) * pos["qty"]

        # record_close retorna strategy_type
        st = self.learner.record_close(
            pos["trade_id"], mark, pnl, reason
        ) or pos.get("strategy_type","NORMAL")
        self.risk_mgr.on_close(sym, pnl, strategy_type=st)

        # Guardar outcome para que la IA aprenda
        try:
            dur_s    = int(time.time()) - pos.get("open_ts", int(time.time()))
            entry_p  = pos.get("entry_price", 0) or mark
            raw_pct  = (mark - entry_p) / entry_p * 100 if entry_p > 0 else 0
            pnl_pct  = raw_pct if pos["side"] == "LONG" else -raw_pct
            result_s = ("WIN"  if pnl >  0.5 else
                        "LOSS" if pnl < -0.5 else "BREAKEVEN")
            ai_filter.record_outcome(
                trade_id    = pos["trade_id"],
                symbol      = sym,
                side        = pos["side"],
                entry_price = entry_p,
                close_price = mark,
                pnl_usdt    = round(pnl, 4),
                pnl_pct     = round(pnl_pct, 3),
                result      = result_s,
                close_reason= reason,
                duration_s  = dur_s,
                leverage    = pos.get("leverage", 1),
                ts_open     = pos.get("open_ts", int(time.time())),
            )
        except Exception as _e:
            log.warning(f"record_outcome {sym}: {_e}")

        with self._lock:
            del self.open_positions[sym]

        dur_s   = int(time.time()) - pos.get("open_ts", int(time.time()))
        dur_str = (f"{dur_s//60}m {dur_s%60}s"
                   if dur_s < 3600 else f"{dur_s//3600}h {(dur_s%3600)//60}m")
        risk_s  = self.risk_mgr.get_status()

        # ── "trades": TRADE CERRADO ───────────────────────────────────────────
        if notify_prefs.is_enabled("trades"):
            self.tg.send(
                f"{'✅' if pnl>0 else '❌'} <b>TRADE CERRADO</b>  [{reason}]\n"
                f"Par: <b>{sym}</b>  {pos['side']}  x{pos['leverage']}  [{st}]\n"
                f"Entrada: <code>{pos['entry_price']:.4f}</code>  "
                f"Cierre: <code>{mark:.4f}</code>\n"
                f"PnL: <code>{pnl:+.2f} USDT</code> "
                f"{'🟢' if pnl>=0 else '🔴'}  Duración: {dur_str}\n"
                f"Balance: {self._get_balance():.2f} USDT\n"
                f"📊 DD hoy: {risk_s['daily_pnl']:+.2f} | "
                f"Cons: {risk_s['consecutive_losses']}"
            )
        log.info(
            f"{'✅' if pnl>0 else '❌'} CERRADO {sym}  "
            f"PnL={pnl:+.2f}  [{reason}]  strategy={st}"
        )
        return True

    # ══════════════════════════════════════════════════════
    #  MONITOR DE POSICIONES
    # ══════════════════════════════════════════════════════

    def _monitor_loop(self) -> None:
        log.info("Monitor iniciado")
        while self.running:
            try:
                with self._lock:
                    syms = list(self.open_positions.keys())
                for sym in syms:
                    self._check_position(sym)
            except Exception as e:
                log.error(f"monitor_loop: {e}")
            time.sleep(MONITOR_INTERVAL)

    def _check_position(self, sym: str) -> None:
        with self._lock:
            pos = self.open_positions.get(sym)
        if not pos:
            return

        mark = self.client.get_mark_price(sym)
        if not mark:
            return

        side   = pos["side"]
        atr_v  = pos.get("atr") or 0
        params = self.learner.get_params()

        # ── Trailing stop ──────────────────────────────────────────────────────
        if params.get("use_trailing", True) and atr_v:
            peak    = pos.get("peak_price", pos["entry_price"])
            trail_m = params.get("trail_atr_mult", 1.0)
            if side == "LONG" and mark > peak:
                new_sl = self.client.safe_price(sym, mark - atr_v * trail_m)
                if new_sl > (pos.get("sl") or 0):
                    try:
                        self.client.set_tp_sl(sym, sl=new_sl)
                        with self._lock:
                            self.open_positions[sym]["sl"]         = new_sl
                            self.open_positions[sym]["peak_price"] = mark
                        log.debug(
                            RC.fmt(RC.TRADE_CLOSED_TRAILING_SL,
                                   symbol=sym, new_sl=new_sl, mark=mark)
                        )
                    except Exception:
                        pass
            elif side == "SHORT" and mark < peak:
                new_sl = self.client.safe_price(sym, mark + atr_v * trail_m)
                if new_sl < (pos.get("sl") or 999999):
                    try:
                        self.client.set_tp_sl(sym, sl=new_sl)
                        with self._lock:
                            self.open_positions[sym]["sl"]         = new_sl
                            self.open_positions[sym]["peak_price"] = mark
                        log.debug(
                            RC.fmt(RC.TRADE_CLOSED_TRAILING_SL,
                                   symbol=sym, new_sl=new_sl, mark=mark)
                        )
                    except Exception:
                        pass

        # ══════════════════════════════════════════════════════════════════
        #  ▼▼▼  SALIDA HÍBRIDA INSTITUCIONAL (1R Gate)  ▼▼▼
        # ══════════════════════════════════════════════════════════════════
        #
        #  Lógica de un experto institucional:
        #    - Calcula 1R = abs(entry_price − sl_inicial)
        #    - Si el precio está en ganancia > 1R Y todavía no se ha
        #      activado esta lógica (sl_moved_to_be = False):
        #        a) Mueve el SL a Break-Even (entry_price) → riesgo = 0
        #        b) Cierra el 50% de la posición → "toma parcial"
        #        c) Deja correr el 50% restante como "runner"
        #        d) Notifica Telegram con el estado del trade asegurado
        #
        #  Condiciones de seguridad:
        #    - atr_v debe ser > 0 para que 1R sea calculable
        #    - sl_inicial debe existir; si no, usa 1×ATR como fallback
        #    - No se reintenta si has_taken_partial ya está en True
        #    - Cualquier fallo de API cancela silenciosamente (no rompe el loop)
        # ══════════════════════════════════════════════════════════════════
        entry_price   = pos["entry_price"]
        sl_moved_to_be    = pos.get("sl_moved_to_be",    False)
        has_taken_partial = pos.get("has_taken_partial", False)

        if not sl_moved_to_be and not has_taken_partial:
            # ── Calcular 1R ────────────────────────────────────────────────
            initial_sl = pos.get("initial_sl") or pos.get("sl")
            if initial_sl and entry_price > 0:
                one_r = abs(entry_price - initial_sl)
            elif atr_v > 0:
                one_r = atr_v       # fallback: sin SL inicial usamos 1×ATR
            else:
                one_r = 0

            if one_r > 0:
                # Precio en ganancia (en términos absolutos, sin apalancamiento)
                price_gain = (
                    (mark - entry_price) if side == "LONG"
                    else (entry_price - mark)
                )

                if price_gain > one_r:
                    # ── a) Mover SL a Break-Even ───────────────────────────
                    be_price  = self.client.safe_price(sym, entry_price)
                    be_ok     = False
                    try:
                        self.client.set_tp_sl(sym, sl=be_price)
                        be_ok = True
                    except Exception as _be_err:
                        log.warning(
                            f"ATM 1R [{sym}] set_tp_sl (BE) falló: {_be_err}"
                        )

                    # ── b) Cierre parcial 50% ──────────────────────────────
                    partial_ok  = False
                    partial_qty = 0.0
                    if be_ok:
                        current_qty = float(pos.get("qty") or 0)
                        raw_half    = current_qty / 2.0
                        try:
                            partial_qty, _ = self.client.safe_qty(sym, raw_half)
                        except Exception:
                            partial_qty = raw_half   # fallback sin redondeo

                        if partial_qty > 0:
                            side_str = "Sell" if side == "LONG" else "Buy"
                            try:
                                resp_p = self.client.place_order(
                                    sym, side_str, partial_qty,
                                    reduce_only=True
                                )
                                if resp_p.get("retCode", -1) == 0:
                                    partial_ok = True
                                else:
                                    log.warning(
                                        f"ATM 1R [{sym}] cierre parcial rc="
                                        f"{resp_p.get('retCode')} "
                                        f"{resp_p.get('retMsg','')}"
                                    )
                            except Exception as _pe:
                                log.warning(
                                    f"ATM 1R [{sym}] cierre parcial excepción: {_pe}"
                                )

                    # ── c) Actualizar estado local ─────────────────────────
                    with self._lock:
                        if sym in self.open_positions:
                            self.open_positions[sym]["sl_moved_to_be"]    = True
                            self.open_positions[sym]["has_taken_partial"]  = True
                            if be_ok:
                                self.open_positions[sym]["sl"]            = be_price
                                self.open_positions[sym]["sl_at_breakeven"]= True
                            if partial_ok:
                                # Actualizar qty restante (runner)
                                new_qty = float(
                                    self.open_positions[sym].get("qty", 0)
                                ) - partial_qty
                                self.open_positions[sym]["qty"] = max(0.0, new_qty)

                    # ── d) Notificación Telegram ───────────────────────────
                    pnl_partial = price_gain * partial_qty if partial_ok else 0.0
                    log.info(
                        f"🏦 ATM 1R [{sym}] {side}  price_gain={price_gain:.4f} "
                        f"one_r={one_r:.4f}  be={'✅' if be_ok else '❌'}  "
                        f"partial={'✅' if partial_ok else '❌'}  "
                        f"partial_qty={partial_qty}  pnl≈{pnl_partial:+.2f}"
                    )
                    if notify_prefs.is_enabled("trades"):
                        status_be  = "✅ SL → Break-Even" if be_ok  else "⚠️ SL sin cambios"
                        status_prt = (
                            f"✅ 50% cerrado ({partial_qty} contratos)"
                            if partial_ok else "⚠️ cierre parcial falló"
                        )
                        self.tg.send(
                            f"🏦 <b>TRADE ASEGURADO — Runner activo</b>\n"
                            f"Par: <b>{sym}</b>  {side}  [{pos.get('strategy_type','?')}]\n"
                            f"Ganancia ≥ 1R ({one_r:.4f})\n"
                            f"Entrada: <code>{entry_price:.4f}</code>  "
                            f"Precio actual: <code>{mark:.4f}</code>\n"
                            f"{status_be}\n"
                            f"{status_prt}\n"
                            f"🏃 Runner: 50% restante libre de riesgo\n"
                            f"PnL parcial ≈ <code>{pnl_partial:+.2f} USDT</code>"
                        )
        # ══════════════════════════════════════════════════════════════════
        #  ▲▲▲  FIN SALIDA HÍBRIDA 1R  ▲▲▲
        # ══════════════════════════════════════════════════════════════════

        # ── Verificar cierre externo (TP/SL hit) ───────────────────────────────
        try:
            real      = self.client.get_positions(sym)
            real_size = sum(float(p.get("size", 0)) for p in real)
            if real_size == 0 and sym in self.open_positions:
                pnl = 0.0
                try:
                    closed = self.client.get_closed_pnl(sym, limit=3)
                    if closed:
                        pnl = float(closed[0].get("closedPnl", 0))
                except Exception:
                    pass
                reason   = RC.TRADE_CLOSED_TP if pnl >= 0 else RC.TRADE_CLOSED_SL
                mark_now = self.client.get_mark_price(sym) or pos["entry_price"]
                with self._lock:
                    pos = self.open_positions.get(sym)
                if pos:
                    st = self.learner.record_close(
                        pos["trade_id"], mark_now, pnl, reason
                    ) or pos.get("strategy_type","NORMAL")
                    self.risk_mgr.on_close(sym, pnl, strategy_type=st)
                    dur_s   = int(time.time()) - pos.get("open_ts", int(time.time()))
                    risk_s  = self.risk_mgr.get_status()
                    # ── "trades": posición cerrada por TP/SL en el exchange ────
                    if notify_prefs.is_enabled("trades"):
                        self.tg.send(
                            f"{'✅' if pnl>=0 else '❌'} <b>CERRADO {sym}</b>  [{reason}]\n"
                            f"PnL: <code>{pnl:+.2f} USDT</code>  "
                            f"Duración: {dur_s//60}m{dur_s%60}s\n"
                            f"Balance: {self._get_balance():.2f} USDT\n"
                            f"📊 DD hoy: {risk_s['daily_pnl']:+.2f} | "
                            f"Cons: {risk_s['consecutive_losses']}"
                        )
                    log.info(
                        RC.fmt(reason, symbol=sym,
                               pnl=f"{pnl:+.2f}", dur=f"{dur_s//60}m")
                    )
                    with self._lock:
                        self.open_positions.pop(sym, None)
        except Exception as e:
            log.debug(f"_check_position {sym}: {e}")

    # ══════════════════════════════════════════════════════
    #  ESCÁNER DE MERCADO
    # ══════════════════════════════════════════════════════

    def _scan_loop(self) -> None:
        log.info("Escáner iniciado")
        cycle = 0
        while self.running:
            try:
                try:
                    self._sync_positions()
                except Exception:
                    pass

                for sym in list(FIXED_WATCHLIST):
                    if not self.running:
                        break
                    try:
                        a   = analyze_symbol(self.client, sym, timeframes=ALL_TF)
                        sig = a["signal"]

                        # Estado de KS para el log
                        cb_open, _ = self.api_cb.is_open()
                        freeze_on, _ = self.news.is_news_freeze_active()
                        risk_s = self.risk_mgr.get_status()
                        ks_flags = "".join([
                            " 🔒" if risk_s["dd_locked"] else "",
                            " ❄️" if freeze_on           else "",
                            " 🚨" if cb_open             else "",
                        ])
                        smc_flags = "".join([
                            "⚡" if a.get("squeeze")        else "",
                            "🌊" if a.get("smc_sweep")       else "",
                            "🧱" if a.get("smc_ob_hit")      else "",
                            "🪟" if a.get("smc_fvg_fill")    else "",
                            "🎯" if a.get("smc_vwap_retest") else "",
                        ])
                        log.info(
                            f"📊 {sym}: {sig:5s}  "
                            f"score={a['composite_score']:+.2f}  "
                            f"conf={a['confidence']:.0%}  "
                            f"macro={a['macro_bias']}  mid={a['mid_bias']}  "
                            f"entry={a['entry_bias']}  {smc_flags}{ks_flags}"
                        )
                        if sig != "FLAT":
                            from tg_controller import is_paused
                            if not is_paused():
                                self.try_open_trade(a)
                            else:
                                log.debug(
                                    RC.fmt(RC.SYSTEM_PAUSED, symbol=sym)
                                )
                        if notify_prefs.is_enabled("signals"):
                            if sig != "FLAT":
                                self.tg.send(format_analysis_for_tg(a))
                            elif cycle % 10 == 0:
                                self.tg.send(format_analysis_for_tg(a))
                    except Exception as e:
                        log.warning(f"scan {sym}: {e}")
                        notify_dev(f"scan {sym}: {e}")
                    time.sleep(1.5)

                if AUTO_SCAN_ENABLED and cycle % 3 == 0:
                    log.info("🔍 Auto-scan mercado...")
                    opps = scan_best_opportunities(
                        self.client, top_n=5,
                        min_volume_usdt=MIN_VOLUME_USDT
                    )
                    if opps:
                        lines = ["<b>🔍 Top oportunidades SMC</b>"]
                        for o in opps:
                            e   = "🟢" if o["signal"] == "LONG" else "🔴"
                            smc = (("🌊" if o.get("smc_sweep")  else "") +
                                   ("🧱" if o.get("smc_ob_hit") else ""))
                            lines.append(
                                f"{e} {o['symbol']:12s} {o['signal']:5s}  "
                                f"score={o['composite_score']:+.2f}  "
                                f"conf={o['confidence']:.0%}  {smc}"
                            )
                        # ── TASK 1: "signals" — Top oportunidades SMC ─────────
                        if notify_prefs.is_enabled("signals"):
                            self.tg.send("\n".join(lines))
                        for o in opps:
                            if not self.running:
                                break
                            self.try_open_trade(o)

                cycle += 1
            except Exception as e:
                log.error(f"scan_loop: {e}")
            time.sleep(SCAN_INTERVAL_SEC)

    # ══════════════════════════════════════════════════════
    #  CONTROL
    # ══════════════════════════════════════════════════════

    def start(self) -> None:
        self.running = True
        self.news.start()
        threading.Thread(
            target=self._scan_loop, daemon=True, name="scanner"
        ).start()
        threading.Thread(
            target=self._monitor_loop, daemon=True, name="monitor"
        ).start()
        log.info(RC.fmt(RC.SYSTEM_BOT_STARTED, mode="running"))

    def stop(self) -> None:
        self.running = False
        self.news.stop()
        # ── "risk": notificación de parada del bot ───────────────────────────
        if notify_prefs.is_enabled("risk"):
            self.tg.send(f"⛔ <b>[{RC.SYSTEM_BOT_STOPPED}]</b>")
        log.info(RC.fmt(RC.SYSTEM_BOT_STOPPED))

    def get_status(self) -> Dict:
        bal    = self._get_balance()
        perf   = self.learner.get_performance_summary()
        risk_s = self.risk_mgr.get_status()
        cb_s   = self.api_cb.get_status()
        freeze_on, freeze_evt = self.news.is_news_freeze_active()

        with self._lock:
            poss = list(self.open_positions.values())

        return {
            "running":        self.running,
            "paper_mode":     PAPER_TRADING,
            "balance_usdt":   round(bal, 2),
            "open_positions": len(poss),
            "positions":      poss,
            "performance":    perf,
            "params":         self.learner.get_params(),
            "risk":           risk_s,
            "news":           self.news.get_status(),
            "ts":             int(time.time()),
            "ai_filter":      ai_filter.get_stats(),
            "leverage_config":{
                "bot_max":  BOT_MAX_LEVERAGE,
                "bot_min":  BOT_MIN_LEVERAGE,
                "risk_pct": BOT_MAX_RISK_PCT,
                "ai_cap":   ai_filter.lev_cap,
            },
            # ── Kill-Switches status ───────────────────────────────────────────
            "kill_switches": {
                "api_circuit_breaker": cb_s,
                "daily_dd_locked":     risk_s["dd_locked"],
                "daily_pnl":           risk_s["daily_pnl"],
                "news_freeze_active":  freeze_on,
                "news_freeze_event":   freeze_evt,
                "strategy_cooldowns":  risk_s.get("strategy", {}),
                "max_exposure_pct":    risk_s["max_exposure_pct"],
                "consecutive_losses":  risk_s["consecutive_losses"],
            },
        }

    def force_close_all(self) -> None:
        with self._lock:
            syms = list(self.open_positions.keys())
        for sym in syms:
            self.try_close_trade(sym, reason=RC.TRADE_CLOSED_CLOSE_ALL)

    def add_to_watchlist(self, sym: str) -> None:
        if sym not in FIXED_WATCHLIST:
            FIXED_WATCHLIST.append(sym)
            self.tg.send(f"➕ {sym} añadido al watchlist")

    def remove_from_watchlist(self, sym: str) -> None:
        if sym in FIXED_WATCHLIST:
            FIXED_WATCHLIST.remove(sym)
            self.tg.send(f"➖ {sym} removido del watchlist")

    def send_daily_summary(self) -> None:
        perf   = self.learner.get_performance_summary()
        risk_s = self.risk_mgr.get_status()
        ai_s   = ai_filter.get_stats()
        cb_s   = self.api_cb.get_status()

        strat_lines = "\n".join(
            f"  {s}: {v['trades']}t | WR={v['win_rate']:.1f}% | "
            f"PnL={v['total_pnl']:+.2f} | "
            f"{'⏸ COOLDOWN' if risk_s['strategy'].get(s,{}).get('in_cooldown') else '✅'}"
            for s, v in perf.get("strategy_stats", {}).items()
            if v["trades"] > 0
        )

        # ── "performance": resumen diario de rendimiento ──────────────────────
        if notify_prefs.is_enabled("performance"):
            self.tg.send(
                f"📈 <b>Resumen diario</b>\n"
                f"Trades: {perf['total_trades']}  WR: {perf['win_rate']:.1f}%\n"
                f"PnL hoy: {risk_s['daily_pnl']:+.2f} USDT  "
                f"{'🔒 LOCKED' if risk_s['dd_locked'] else '✅'}\n"
                f"PnL total: {perf['total_pnl']:+.2f} USDT\n"
                f"Mejor: +{perf['best_trade']:.2f}  Peor: {perf['worst_trade']:.2f}\n"
                f"Balance: {self._get_balance():.2f} USDT\n"
                + (f"\n<b>Por estrategia:</b>\n{strat_lines}\n" if strat_lines else "") +
                f"\n<b>Kill-Switches:</b>\n"
                f"  API CB: {'🔴 ACTIVO' if cb_s['active'] else '✅'} | "
                f"Cons. losses: {risk_s['consecutive_losses']}\n"
                f"\n🤖 IA: {ai_s['approved']} aprobados, "
                f"{ai_s['rejected']} rechazados ({ai_s['approval_rate']:.1f}%)"
            )


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler("bot.log", "a", "utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    bot = AutonomousBot()
    bot.start()
    try:
        while True:
            s  = bot.get_status()
            p  = s["performance"]
            ai = s["ai_filter"]
            ks = s["kill_switches"]
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"Bal:{s['balance_usdt']:.2f} | "
                f"Pos:{s['open_positions']} | "
                f"Trades:{p['total_trades']} | "
                f"WR:{p['win_rate']:.1f}% | "
                f"PnL:{p['total_pnl']:+.2f} | "
                f"DD:{ks['daily_pnl']:+.2f}"
                f"{'[LOCK]' if ks['daily_dd_locked']              else ''}"
                f"{'[CB]'   if ks['api_circuit_breaker']['active'] else ''}"
                f"{'[NF]'   if ks['news_freeze_active']           else ''}"
            )
            time.sleep(60)
    except KeyboardInterrupt:
        bot.stop()
