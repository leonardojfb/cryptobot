"""
bot_autonomous.py  v5 — ATM Event-Driven Monitor + Kill-Switch Gate
════════════════════════════════════════════════════════════════════
FASE A — JERARQUÍA DE CORTACIRCUITOS (intacta)
Evaluados en _check_kill_switches() ANTES de cualquier lógica de IA:

  KS-1  API Circuit Breaker    → 3 errores place_order en 5 min → pausa 15 min
  KS-2  Daily DD Kill-Switch   → PnL diario < -5 % → lock hasta 00:00 UTC
  KS-3  News Freeze Window     → ±30 min de evento macro HIGH_IMPACT
  KS-4  Strategy Cooldown      → 4 pérdidas × estrategia → 12 h
  KS-5  Max Total Exposure     → margen > 15 % balance → bloqueo

La IA (ai_filter.should_trade) se invoca DESPUÉS y NO puede saltarse
ninguno de los KS anteriores.

FASE B — ATM EVENT-DRIVEN MONITOR
El monitor corre cada MONITOR_TICK_SEC (15s por defecto) pero la IA
(ai_filter.evaluate_open_position) sólo se despierta si:

  Condición NORMAL     → time.time() >= pos["next_eval_ts"]
                         Wakeup: ATM_WAKEUP_BAR_CLOSE
                         Timeframes: FAST=5m | NORMAL=15m | INSTITUTIONAL=4h

  Condición EMERGENCIA → precio se movió ≥ ATM_EMERGENCY_PRICE_MOVE_PCT%
                         desde la última evaluación
                         Wakeup: ATM_WAKEUP_EMERGENCY_VOLATILITY

  Condición EMERGENCIA → news_engine emite alerta crítica nueva
                         Wakeup: ATM_WAKEUP_EMERGENCY_NEWS

La IA adapta su paciencia y agresividad según strategy_type + wakeup_reason.

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
    STRATEGY_TIMEFRAME_MIN, ATM_EMERGENCY_PRICE_MOVE_PCT,
)
import notify_prefs

# ── Config ─────────────────────────────────────────────────────────────────────
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY",   "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET","").strip()
PAPER_TRADING    = os.getenv("PAPER_TRADING","true").lower() in ("1","true","yes")
SCAN_INTERVAL_SEC= int(os.getenv("SCAN_INTERVAL_SEC",   "30"))
MONITOR_TICK_SEC = int(os.getenv("MONITOR_TICK_SEC",    "15"))   # tick del monitor ATM
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
        self.news     = NewsEngine(telegram_notifier=self.tg, scan_interval=120)

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
        atm  = (
            f"⏰ ATM Event-Driven (tick={MONITOR_TICK_SEC}s):\n"
            f"  FAST={STRATEGY_TIMEFRAME_MIN['FAST']}m | "
            f"NORMAL={STRATEGY_TIMEFRAME_MIN['NORMAL']}m | "
            f"INST={STRATEGY_TIMEFRAME_MIN['INSTITUTIONAL']}m | "
            f"Emerg≥{ATM_EMERGENCY_PRICE_MOVE_PCT}%"
        )
        msg = (
            f"🤖 <b>[{RC.SYSTEM_BOT_STARTED}] — {mode}</b>\n"
            f"Watchlist: {', '.join(FIXED_WATCHLIST)}\n"
            f"Scan: {SCAN_INTERVAL_SEC}s | Monitor tick: {MONITOR_TICK_SEC}s\n"
            f"Lev: {BOT_MIN_LEVERAGE}x–{BOT_MAX_LEVERAGE}x | Risk: {BOT_MAX_RISK_PCT}%\n"
            f"{ks}\n{atm}"
        )
        if self.tg._active:
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
                st   = "NORMAL"   # estrategia por defecto en posiciones sincronizadas
                tf_m = STRATEGY_TIMEFRAME_MIN.get(st, 15)
                new_pos[sym] = {
                    "trade_id":          f"sync_{sym}",
                    "symbol":            sym,
                    "side":              side,
                    "entry_price":       ep,
                    "qty":               float(p.get("size", 0)),
                    "leverage":          int(float(p.get("leverage", 10))),
                    "tp":                float(p.get("takeProfit", 0)) or None,
                    "sl":                float(p.get("stopLoss",   0)) or None,
                    "open_ts":           int(time.time()),
                    "peak_price":        ep,
                    "atr":               None,
                    "ai_decision":       None,
                    "strategy_type":     st,
                    # ATM event-driven fields
                    "tf_minutes":        tf_m,
                    "next_eval_ts":      self._get_next_bar_close_ts(tf_m),
                    "last_eval_price":   ep,
                    "sl_at_breakeven":   False,
                    "last_news_alert_ts":0,
                }

            old_keys = set(self.open_positions.keys())
            new_keys = set(new_pos.keys())

            for sym in new_keys - old_keys:
                pos = new_pos[sym]
                log.info(
                    f"📡 [{RC.RISK_STATE_LOADED}] "
                    f"pos detectada: {sym} {pos['side']} @ {pos['entry_price']}"
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

    def _in_cooldown(self, sym: str) -> bool:
        return time.time() < self.cooldowns.get(sym, 0)

    def _set_cooldown(self, sym: str) -> None:
        self.cooldowns[sym] = (
            time.time() + self.learner.params.get("cooldown_seconds", 60)
        )

    @staticmethod
    def _get_next_bar_close_ts(timeframe_minutes: int) -> int:
        """
        Calcula el timestamp UTC exacto (epoch seconds) del cierre de la
        vela ACTUAL para el timeframe dado.

        Ejemplo: timeframe_minutes=15, now=10:07 UTC
          → vela actual abrió a las 10:00
          → cierra a las 10:15 → retorna ese timestamp

        Los timestamps de Bybit/CCXT son siempre alineados al borde de hora UTC,
        así que floor(now / tf_sec) * tf_sec + tf_sec funciona correctamente
        para todos los timeframes estándar (1m, 5m, 15m, 4h, etc.).
        """
        now    = int(time.time())
        tf_sec = timeframe_minutes * 60
        current_bar_open = (now // tf_sec) * tf_sec
        return current_bar_open + tf_sec

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
        score      = analysis["composite_score"]
        conf       = analysis["confidence"]
        mark       = analysis["mark_price"]
        atr_v      = analysis.get("atr") or 0
        entry_mode = analysis.get("entry_mode", "STANDARD")
        threshold  = analysis.get("threshold", 4.0)
        strategy_type = ENTRY_MODE_TO_STRATEGY.get(entry_mode, "NORMAL")

        # ── Pre-filtros baratos (sin IO) ───────────────────────────────────────
        if sym in PROBLEMATIC_SYMBOLS:
            return False
        if self._in_cooldown(sym):
            log.debug(RC.fmt(RC.TRADE_BLOCKED_COOLDOWN, symbol=sym))
            return False
        if sig == "FLAT":
            return False

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
            if any(c in block_reason for c in _critical_ks):
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
        tf_m    = STRATEGY_TIMEFRAME_MIN.get(strategy_type, 15)
        pos_data: Dict[str, Any] = {
            "trade_id":          _pending_id,
            "symbol":            sym,
            "side":              sig,
            "entry_price":       mark,
            "qty":               qty,
            "leverage":          final_leverage,
            "tp":                tp,
            "sl":                sl,
            "open_ts":           int(time.time()),
            "peak_price":        mark,
            "atr":               atr_v,
            "ai_decision":       ai_decision,
            "strategy_type":     strategy_type,
            # ── ATM Event-Driven fields ────────────────────────────────────────
            # tf_minutes: timeframe base de la estrategia (5 / 15 / 240)
            "tf_minutes":        tf_m,
            # next_eval_ts: timestamp UTC del próximo cierre de vela → despertar normal
            "next_eval_ts":      self._get_next_bar_close_ts(tf_m),
            # last_eval_price: precio en la última evaluación IA → detectar spikes
            "last_eval_price":   mark,
            # sl_at_breakeven: bandera para evitar mover SL a BE dos veces
            "sl_at_breakeven":   False,
            # last_news_alert_ts: ts de la última alerta macro procesada
            "last_news_alert_ts":0,
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
        next_ts = pos_data["next_eval_ts"]
        next_str = time.strftime("%H:%M UTC", time.gmtime(next_ts))

        self.tg.send(
            f"{'🟢' if sig=='LONG' else '🔴'} <b>TRADE ABIERTO</b>\n"
            f"Par: <b>{sym}</b>  {sig}  x{final_leverage}  [{strategy_type}]\n"
            f"Entrada: <code>{mark:.4f}</code>  Qty: {qty}\n"
            f"TP: <code>{tp}</code>  SL: <code>{sl}</code>\n"
            f"R:R ≈ 1:{rr:.1f}\n"
            f"Score: {score:+.2f}  Conf: {conf:.0%}  [{entry_mode}]\n"
            f"{aligned_txt}\n"
            f"SMC: {' '.join(smc_badges) if smc_badges else 'sin setup'}\n"
            f"🤖 IA: {smc_anal[:60]}\n"
            f"   Noticias: {news_imp} | {news_e} {news_d}\n"
            f"   Leverage IA={ai_lev}x → final={final_leverage}x\n"
            f"F&G: {fg} — {fg_lbl} | Balance: {balance:.2f} USDT\n"
            f"⏰ ATM [{strategy_type} | {tf_m}m] → primera eval: {next_str}\n"
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
            f"score={score:.2f}  strategy={strategy_type}  [AI_lev={ai_lev}x]"
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
    #  MONITOR EVENT-DRIVEN — Fase B
    # ══════════════════════════════════════════════════════

    def _monitor_loop(self) -> None:
        """
        Loop rápido que corre cada MONITOR_TICK_SEC (15s por defecto).

        Cada tick hace:
          1. Obtener precio de mercado para cada posición abierta.
          2. Verificar cierre externo (TP/SL hit en el exchange).
          3. Actualizar trailing stop mecánico (sin IA).
          4. Evaluar condiciones de despertar para la IA:
               - BAR_CLOSE:             time.time() >= next_eval_ts
               - EMERGENCY_VOLATILITY:  precio movido ≥ umbral desde last_eval_price
               - EMERGENCY_NEWS:        nueva alerta crítica del news_engine
          5. Si hay condición → llamar ai_filter.evaluate_open_position().
          6. Ejecutar la acción retornada por la IA.
          7. Actualizar next_eval_ts y last_eval_price.

        La IA NO se llama en cada tick — sólo cuando hay una condición real.
        """
        log.info(
            f"Monitor event-driven iniciado  "
            f"(tick={MONITOR_TICK_SEC}s | "
            f"TF: FAST={STRATEGY_TIMEFRAME_MIN['FAST']}m "
            f"NORMAL={STRATEGY_TIMEFRAME_MIN['NORMAL']}m "
            f"INST={STRATEGY_TIMEFRAME_MIN['INSTITUTIONAL']}m)"
        )
        while self.running:
            try:
                with self._lock:
                    syms = list(self.open_positions.keys())
                for sym in syms:
                    try:
                        self._check_position(sym)
                    except Exception as e:
                        log.error(f"_check_position [{sym}]: {e}")
            except Exception as e:
                log.error(f"monitor_loop: {e}")
            time.sleep(MONITOR_TICK_SEC)

    def _check_position(self, sym: str) -> None:
        """
        Evaluación de una posición abierta por el monitor event-driven.
        Sólo llama a la IA si se cumple una condición de despertar.
        """
        with self._lock:
            pos = self.open_positions.get(sym)
        if not pos:
            return

        mark = self.client.get_mark_price(sym)
        if not mark:
            return

        now = time.time()

        # ── 1. Verificar cierre externo (TP/SL hit) — SIEMPRE primero ─────────
        try:
            real      = self.client.get_positions(sym)
            real_size = sum(float(p.get("size", 0)) for p in real)
            if real_size == 0 and sym in self.open_positions:
                self._handle_external_close(sym, pos, mark)
                return
        except Exception as e:
            log.debug(f"_check_position [{sym}] external check: {e}")

        # ── 2. Trailing stop mecánico (siempre activo, independiente de la IA) ─
        self._update_trailing_stop(sym, pos, mark)

        # Refrescar pos después del trailing update
        with self._lock:
            pos = self.open_positions.get(sym)
        if not pos:
            return

        # ── 3. Determinar wakeup reason ────────────────────────────────────────
        wakeup_reason: Optional[str] = None

        # 3a. Emergencia — nueva alerta crítica de noticias
        _, freeze_evt = self.news.is_news_freeze_active()
        last_news_ts  = float(pos.get("last_news_alert_ts", 0))
        if freeze_evt:
            evt_ts = float(freeze_evt.get("timestamp", 0))
            # Es nueva si ocurrió tras la última eval Y en los últimos 10 minutos
            if evt_ts > last_news_ts and evt_ts > (now - 600):
                wakeup_reason = RC.ATM_WAKEUP_EMERGENCY_NEWS
                with self._lock:
                    if sym in self.open_positions:
                        self.open_positions[sym]["last_news_alert_ts"] = evt_ts

        # 3b. Emergencia — spike de precio desde la última evaluación
        if wakeup_reason is None:
            last_eval_price = float(
                pos.get("last_eval_price", pos["entry_price"]) or pos["entry_price"]
            )
            if last_eval_price > 0:
                move_pct = abs(mark - last_eval_price) / last_eval_price * 100
                # Umbral dinámico: si el ATR es conocido usarlo, si no el estático
                atr_v = float(pos.get("atr") or 0)
                entry = float(pos.get("entry_price", 0) or 0)
                if atr_v > 0 and entry > 0:
                    atr_pct_of_price   = atr_v / entry * 100
                    # Emergencia = movimiento de ≥ 0.75 × ATR% o el umbral estático
                    emergency_threshold = max(ATM_EMERGENCY_PRICE_MOVE_PCT,
                                              atr_pct_of_price * 0.75)
                else:
                    emergency_threshold = ATM_EMERGENCY_PRICE_MOVE_PCT
                if move_pct >= emergency_threshold:
                    wakeup_reason = RC.ATM_WAKEUP_EMERGENCY_VOLATILITY

        # 3c. Normal — cierre de vela del timeframe base
        if wakeup_reason is None:
            next_eval_ts = int(pos.get("next_eval_ts", 0))
            if now >= next_eval_ts:
                wakeup_reason = RC.ATM_WAKEUP_BAR_CLOSE

        # ── 4. Sin condición de despertar → tick silencioso, no llamar a la IA ─
        if wakeup_reason is None:
            next_eval_ts   = int(pos.get("next_eval_ts", 0))
            secs_remaining = max(0, next_eval_ts - now)
            log.debug(
                RC.fmt(RC.ATM_AI_SKIPPED,
                       symbol=sym, mark=f"{mark:.4f}",
                       strategy=pos.get("strategy_type","?"),
                       next_eval_in=f"{secs_remaining:.0f}s")
            )
            return

        # ── 5. Llamar a la IA ──────────────────────────────────────────────────
        log.info(
            RC.fmt(wakeup_reason,
                   symbol=sym, mark=f"{mark:.4f}",
                   strategy=pos.get("strategy_type","?"),
                   tf=f"{pos.get('tf_minutes','?')}m")
        )
        news_bias   = self.news.get_news_bias(sym)
        recent_news = self.news.get_recent_news(4)

        atm_result = ai_filter.evaluate_open_position(
            pos           = pos,
            current_price = mark,
            wakeup_reason = wakeup_reason,
            news_bias     = news_bias,
            recent_news   = recent_news,
        )

        action     = atm_result.get("action", "HOLD")
        confidence = float(atm_result.get("confidence", 0.5))
        new_sl     = atm_result.get("new_sl")
        reasoning  = str(atm_result.get("reasoning", ""))

        # ── 6. Ejecutar la acción ordenada por la IA ───────────────────────────
        self._execute_atm_action(
            sym, pos, action, new_sl, reasoning,
            wakeup_reason, mark, confidence
        )

        # ── 7. Actualizar estado ATM para el próximo ciclo ─────────────────────
        # Solo actualizar si la posición sigue abierta (CLOSE la elimina)
        with self._lock:
            if sym in self.open_positions:
                tf_m     = int(pos.get("tf_minutes", 15))
                next_ts  = self._get_next_bar_close_ts(tf_m)
                self.open_positions[sym]["last_eval_price"] = mark
                self.open_positions[sym]["next_eval_ts"]    = next_ts
                log.info(
                    RC.fmt(RC.ATM_EVAL_SCHEDULED,
                           symbol=sym,
                           tf=f"{tf_m}m",
                           wakeup_was=wakeup_reason.split("_")[-1],
                           action_taken=action,
                           next_eval=time.strftime("%H:%M:%S UTC",
                                                   time.gmtime(next_ts)))
                )

    # ── Helpers del monitor ───────────────────────────────────────────────────

    def _update_trailing_stop(self, sym: str, pos: Dict, mark: float) -> None:
        """
        Trailing stop mecánico basado en ATR.
        Se ejecuta en cada tick, independientemente de la IA.
        Solo mueve el SL si el nuevo nivel es MEJOR que el actual.
        """
        atr_v  = float(pos.get("atr") or 0)
        params = self.learner.get_params()
        if not (params.get("use_trailing", True) and atr_v):
            return

        side    = pos["side"]
        peak    = float(pos.get("peak_price", pos["entry_price"]))
        trail_m = float(params.get("trail_atr_mult", 1.0))

        if side == "LONG" and mark > peak:
            new_sl = self.client.safe_price(sym, mark - atr_v * trail_m)
            if new_sl > (pos.get("sl") or 0):
                try:
                    self.client.set_tp_sl(sym, sl=new_sl)
                    with self._lock:
                        if sym in self.open_positions:
                            self.open_positions[sym]["sl"]         = new_sl
                            self.open_positions[sym]["peak_price"] = mark
                    log.debug(
                        RC.fmt(RC.TRADE_CLOSED_TRAILING_SL,
                               symbol=sym, new_sl=f"{new_sl:.4f}",
                               mark=f"{mark:.4f}", atr=f"{atr_v:.4f}")
                    )
                except Exception:
                    pass

        elif side == "SHORT" and mark < peak:
            new_sl = self.client.safe_price(sym, mark + atr_v * trail_m)
            if new_sl < (pos.get("sl") or 999_999_999):
                try:
                    self.client.set_tp_sl(sym, sl=new_sl)
                    with self._lock:
                        if sym in self.open_positions:
                            self.open_positions[sym]["sl"]         = new_sl
                            self.open_positions[sym]["peak_price"] = mark
                    log.debug(
                        RC.fmt(RC.TRADE_CLOSED_TRAILING_SL,
                               symbol=sym, new_sl=f"{new_sl:.4f}",
                               mark=f"{mark:.4f}", atr=f"{atr_v:.4f}")
                    )
                except Exception:
                    pass

    def _handle_external_close(self, sym: str, pos: Dict, mark: float) -> None:
        """
        Posición cerrada externamente (TP/SL hit, liquidación, cierre manual).
        Limpia estado interno y notifica.
        """
        pnl = 0.0
        try:
            closed = self.client.get_closed_pnl(sym, limit=3)
            if closed:
                pnl = float(closed[0].get("closedPnl", 0))
        except Exception:
            pass

        reason  = RC.TRADE_CLOSED_TP if pnl >= 0 else RC.TRADE_CLOSED_SL
        st      = self.learner.record_close(
            pos["trade_id"], mark, pnl, reason
        ) or pos.get("strategy_type", "NORMAL")
        self.risk_mgr.on_close(sym, pnl, strategy_type=st)

        dur_s  = int(time.time()) - pos.get("open_ts", int(time.time()))
        risk_s = self.risk_mgr.get_status()
        self.tg.send(
            f"{'✅' if pnl>=0 else '❌'} <b>CERRADO {sym}</b>  [{reason}]\n"
            f"Estrategia: [{st}]  x{pos.get('leverage','?')}\n"
            f"PnL: <code>{pnl:+.2f} USDT</code>  "
            f"Duración: {dur_s//60}m{dur_s%60}s\n"
            f"Balance: {self._get_balance():.2f} USDT\n"
            f"📊 DD hoy: {risk_s['daily_pnl']:+.2f} | "
            f"Cons: {risk_s['consecutive_losses']}"
        )
        log.info(
            RC.fmt(reason, symbol=sym,
                   pnl=f"{pnl:+.2f}", dur=f"{dur_s//60}m",
                   strategy=st)
        )
        with self._lock:
            self.open_positions.pop(sym, None)

    def _execute_atm_action(
        self,
        sym:           str,
        pos:           Dict,
        action:        str,
        new_sl:        Optional[float],
        reasoning:     str,
        wakeup_reason: str,
        mark:          float,
        confidence:    float,
    ) -> None:
        """
        Ejecuta la acción ATM ordenada por la IA.
        Todos los branches usan RC para logs y Telegram.

        Acciones:
          HOLD                 → no tocar nada
          MOVE_SL_TO_BREAKEVEN → mover SL al precio de entrada
          TRAIL_STOP           → actualizar SL a new_sl (validado)
          PARTIAL_CLOSE        → cerrar ~50% de la posición
          CLOSE                → cerrar posición completa
        """
        strategy  = pos.get("strategy_type", "NORMAL")
        entry     = float(pos.get("entry_price", mark) or mark)
        side      = pos.get("side", "LONG")
        leverage  = int(pos.get("leverage", 1) or 1)
        qty       = float(pos.get("qty", 0) or 0)
        atr_v     = float(pos.get("atr") or 0)
        pnl_usdt  = ((mark - entry) if side == "LONG" else (entry - mark)) * qty
        is_profit = pnl_usdt > 0

        # Ícono de urgencia para el mensaje Telegram
        urgency = (
            "🚨" if wakeup_reason in (RC.ATM_WAKEUP_EMERGENCY_VOLATILITY,
                                       RC.ATM_WAKEUP_EMERGENCY_NEWS)
            else "⏰"
        )

        # ── HOLD ─────────────────────────────────────────────────────────────
        if action == "HOLD":
            log.info(
                RC.fmt(RC.ATM_ACTION_HOLD,
                       symbol=sym, strategy=strategy,
                       wakeup=wakeup_reason.split("_")[-1],
                       conf=f"{confidence:.0%}",
                       pnl=f"{pnl_usdt:+.2f}",
                       reason=reasoning[:60])
            )
            return

        # ── MOVE_SL_TO_BREAKEVEN ──────────────────────────────────────────────
        elif action == "MOVE_SL_TO_BREAKEVEN":
            if not is_profit:
                log.warning(
                    RC.fmt(RC.ATM_SL_VALIDATION_FAILED,
                           symbol=sym,
                           reason="PnL negativo: no se puede mover SL a BE",
                           pnl=f"{pnl_usdt:+.2f}")
                )
                return
            if pos.get("sl_at_breakeven", False):
                log.debug(f"ATM [{sym}] SL ya está en BE — ignorando")
                return

            be_price   = self.client.safe_price(sym, entry)
            current_sl = pos.get("sl")
            # Solo si el nuevo SL es más favorable que el actual
            sl_improves = (
                current_sl is None or
                (side == "LONG"  and be_price > (current_sl or 0)) or
                (side == "SHORT" and be_price < (current_sl or 999_999_999))
            )
            if not sl_improves:
                log.debug(f"ATM [{sym}] SL actual ya supera el BE — skip")
                return

            try:
                self.client.set_tp_sl(sym, sl=be_price)
                with self._lock:
                    if sym in self.open_positions:
                        self.open_positions[sym]["sl"]             = be_price
                        self.open_positions[sym]["sl_at_breakeven"] = True
                log.info(
                    RC.fmt(RC.ATM_ACTION_MOVE_SL_BREAKEVEN,
                           symbol=sym, be=f"{be_price:.4f}",
                           pnl=f"{pnl_usdt:+.2f}",
                           strategy=strategy)
                )
                self.tg.send(
                    f"{urgency} <b>[{RC.ATM_ACTION_MOVE_SL_BREAKEVEN}]</b>\n"
                    f"Par: <b>{sym}</b>  [{strategy}]  x{leverage}\n"
                    f"SL → Breakeven: <code>{be_price:.4f}</code>\n"
                    f"PnL actual: <code>{pnl_usdt:+.2f} USDT</code>\n"
                    f"Wakeup: <code>{wakeup_reason}</code>\n"
                    f"🤖 {reasoning[:140]}"
                )
            except Exception as e:
                log.error(f"ATM [{sym}] set_tp_sl BE: {e}")

        # ── TRAIL_STOP ────────────────────────────────────────────────────────
        elif action == "TRAIL_STOP":
            if new_sl is None:
                log.warning(
                    f"ATM [{sym}] TRAIL_STOP sin new_sl → degradando a HOLD"
                )
                return

            new_sl_safe = self.client.safe_price(sym, new_sl)
            current_sl  = pos.get("sl")

            # Validar que el nuevo SL sea más favorable Y no cruce el precio
            sl_valid = (
                (side == "LONG"
                 and new_sl_safe > (current_sl or 0)
                 and new_sl_safe < mark) or
                (side == "SHORT"
                 and new_sl_safe < (current_sl or 999_999_999)
                 and new_sl_safe > mark)
            )
            if not sl_valid:
                log.warning(
                    RC.fmt(RC.ATM_SL_VALIDATION_FAILED,
                           symbol=sym,
                           side=side,
                           new_sl=f"{new_sl_safe:.4f}",
                           current_sl=f"{current_sl}",
                           mark=f"{mark:.4f}",
                           reason="nuevo SL no mejora el actual o cruza el precio")
                )
                return

            try:
                self.client.set_tp_sl(sym, sl=new_sl_safe)
                with self._lock:
                    if sym in self.open_positions:
                        self.open_positions[sym]["sl"]         = new_sl_safe
                        self.open_positions[sym]["peak_price"] = mark
                log.info(
                    RC.fmt(RC.ATM_ACTION_TRAIL_STOP,
                           symbol=sym, new_sl=f"{new_sl_safe:.4f}",
                           mark=f"{mark:.4f}",
                           conf=f"{confidence:.0%}",
                           strategy=strategy)
                )
                self.tg.send(
                    f"{urgency} <b>[{RC.ATM_ACTION_TRAIL_STOP}]</b>\n"
                    f"Par: <b>{sym}</b>  [{strategy}]  x{leverage}\n"
                    f"Trailing SL → <code>{new_sl_safe:.4f}</code>\n"
                    f"Precio actual: <code>{mark:.4f}</code>\n"
                    f"PnL: <code>{pnl_usdt:+.2f} USDT</code>\n"
                    f"Wakeup: <code>{wakeup_reason}</code>\n"
                    f"🤖 {reasoning[:140]}"
                )
            except Exception as e:
                log.error(f"ATM [{sym}] set_tp_sl TRAIL: {e}")

        # ── PARTIAL_CLOSE ─────────────────────────────────────────────────────
        elif action == "PARTIAL_CLOSE":
            if qty <= 0:
                log.warning(f"ATM [{sym}] PARTIAL_CLOSE qty=0 → skip")
                return

            # Cerrar ~50 % redondeado al step válido del exchange
            close_qty, _ = self.client.safe_qty(sym, qty * 0.5)
            if close_qty <= 0:
                log.warning(
                    f"ATM [{sym}] PARTIAL_CLOSE safe_qty=0 "
                    f"(raw={qty*0.5:.6f}) → skip"
                )
                return

            close_side = "Sell" if side == "LONG" else "Buy"
            try:
                resp = self.client.place_order(
                    sym, close_side, close_qty, reduce_only=True
                )
                rc_code = resp.get("retCode", -1)
                if rc_code == 0:
                    remaining  = qty - close_qty
                    pnl_partial = pnl_usdt * (close_qty / qty)
                    with self._lock:
                        if sym in self.open_positions:
                            self.open_positions[sym]["qty"] = remaining
                    log.info(
                        RC.fmt(RC.ATM_ACTION_PARTIAL_CLOSE,
                               symbol=sym,
                               close_qty=close_qty,
                               remaining=remaining,
                               pnl_partial=f"{pnl_partial:+.2f}",
                               strategy=strategy)
                    )
                    self.tg.send(
                        f"{urgency} <b>[{RC.ATM_ACTION_PARTIAL_CLOSE}]</b>\n"
                        f"Par: <b>{sym}</b>  [{strategy}]  x{leverage}\n"
                        f"Cerrado 50%: <code>{close_qty}</code> unidades\n"
                        f"PnL parcial ≈ <code>{pnl_partial:+.2f} USDT</code>\n"
                        f"Restante: <code>{remaining:.6f}</code>\n"
                        f"Wakeup: <code>{wakeup_reason}</code>\n"
                        f"🤖 {reasoning[:140]}"
                    )
                else:
                    log.error(
                        RC.fmt(RC.API_ORDER_ERROR,
                               symbol=sym, retcode=rc_code,
                               msg=resp.get("retMsg","")[:80],
                               context="ATM_PARTIAL_CLOSE")
                    )
            except Exception as e:
                log.error(f"ATM [{sym}] partial_close: {e}")

        # ── CLOSE ─────────────────────────────────────────────────────────────
        elif action == "CLOSE":
            log.info(
                RC.fmt(RC.ATM_ACTION_CLOSE,
                       symbol=sym, strategy=strategy,
                       wakeup=wakeup_reason.split("_")[-1],
                       conf=f"{confidence:.0%}",
                       pnl=f"{pnl_usdt:+.2f}",
                       reason=reasoning[:60])
            )
            # Reason code de cierre: más específico según el wakeup
            if wakeup_reason == RC.ATM_WAKEUP_EMERGENCY_VOLATILITY:
                close_rc = RC.TRADE_CLOSED_TRAILING_SL
            elif wakeup_reason == RC.ATM_WAKEUP_EMERGENCY_NEWS:
                close_rc = RC.TRADE_CLOSED_MANUAL
            else:
                close_rc = RC.TRADE_CLOSED_MANUAL

            # Notificar ANTES de cerrar (por si el cierre falla)
            self.tg.send(
                f"{urgency} <b>[{RC.ATM_ACTION_CLOSE}]</b>  IA Decision\n"
                f"Par: <b>{sym}</b>  [{strategy}]  x{leverage}\n"
                f"Wakeup: <code>{wakeup_reason}</code>\n"
                f"PnL antes de cierre: <code>{pnl_usdt:+.2f} USDT</code>\n"
                f"Confianza IA: {confidence:.0%}\n"
                f"🤖 {reasoning[:180]}"
            )
            self.try_close_trade(sym, reason=close_rc)

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
        self.tg.send(f"⛔ <b>[{RC.SYSTEM_BOT_STOPPED}]</b>")
        log.info(RC.fmt(RC.SYSTEM_BOT_STOPPED))

    def get_status(self) -> Dict:
        bal    = self._get_balance()
        perf   = self.learner.get_performance_summary()
        risk_s = self.risk_mgr.get_status()
        cb_s   = self.api_cb.get_status()
        freeze_on, freeze_evt = self.news.is_news_freeze_active()
        now    = time.time()

        with self._lock:
            poss = list(self.open_positions.values())

        # Enriquecer cada posición con info del próximo despertar ATM
        poss_enriched = []
        for p in poss:
            p_copy = dict(p)
            next_ts = int(p.get("next_eval_ts", 0))
            p_copy["atm_next_eval_in_s"]  = max(0, next_ts - now)
            p_copy["atm_next_eval_at_utc"] = (
                time.strftime("%H:%M:%S UTC", time.gmtime(next_ts))
                if next_ts > 0 else "N/A"
            )
            poss_enriched.append(p_copy)

        return {
            "running":        self.running,
            "paper_mode":     PAPER_TRADING,
            "balance_usdt":   round(bal, 2),
            "open_positions": len(poss_enriched),
            "positions":      poss_enriched,
            "performance":    perf,
            "params":         self.learner.get_params(),
            "risk":           risk_s,
            "news":           self.news.get_status(),
            "ts":             int(now),
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
            # ── ATM Event-Driven config ────────────────────────────────────────
            "atm_config": {
                "monitor_tick_sec":        MONITOR_TICK_SEC,
                "emergency_move_pct":      ATM_EMERGENCY_PRICE_MOVE_PCT,
                "timeframes":              STRATEGY_TIMEFRAME_MIN,
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
