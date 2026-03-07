"""
risk_manager.py  v3 — Kill-Switches Deterministas
══════════════════════════════════════════════════════════════════════
JERARQUÍA DE EVALUACIÓN (can_open evalúa en este orden exacto):
  KS-1  API Circuit Breaker          → evaluado en bot_autonomous PRIMERO
  KS-2  Daily Drawdown Kill-Switch   → PnL diario < -5 % → lock hasta 00:00 UTC
  KS-3  News Freeze Window           → evaluado en bot_autonomous antes de llamar aquí
  KS-4  Strategy Cooldown            → 4 pérdidas × estrategia → 12 h
  KS-5  Max Total Exposure           → margen total > 15 % balance

La IA NO tiene autoridad para saltarse ninguna de estas reglas.
Estado persistente: risk_state.json  (sobrevive reinicios del bot).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, Optional, Tuple

from reason_codes import (
    RC,
    DAILY_DD_KILL_PCT,
    MAX_EXPOSURE_PCT,
    STRATEGY_MAX_CONSECUTIVE_LOSSES,
    STRATEGY_COOLDOWN_SEC,
    VALID_STRATEGY_TYPES,
    ENTRY_MODE_TO_STRATEGY,
)

log = logging.getLogger("risk_manager")

_STATE_FILE = "risk_state.json"


def _utc_day_start() -> int:
    """Timestamp Unix del inicio del día UTC actual (00:00:00 UTC)."""
    return int(time.time() // 86400 * 86400)


# ══════════════════════════════════════════════════════════
#  TRACKER DE COOLDOWN POR ESTRATEGIA
# ══════════════════════════════════════════════════════════

class _StrategyTracker:
    """
    Trackea pérdidas consecutivas y cooldown separado por
    strategy_type: FAST | NORMAL | INSTITUTIONAL.
    Las tres estrategias son completamente independientes entre sí.
    """

    def __init__(self) -> None:
        self._state: Dict[str, Dict] = {
            s: {"consecutive_losses": 0, "cooldown_until": 0.0}
            for s in VALID_STRATEGY_TYPES
        }

    # ── Cooldown codes por estrategia ─────────────────────────────────────────
    _COOLDOWN_RC: Dict[str, str] = {
        "FAST":          RC.ENGINE_FAST_COOLDOWN_4_LOSSES,
        "NORMAL":        RC.ENGINE_NORMAL_COOLDOWN_4_LOSSES,
        "INSTITUTIONAL": RC.ENGINE_INSTITUTIONAL_COOLDOWN_4_LOSSES,
    }

    def record(self, strategy: str, is_win: bool) -> Optional[str]:
        """
        Registra resultado de un trade para la estrategia dada.
        Retorna reason_code si se activó un cooldown, None si no.
        """
        st = self._state.get(strategy, self._state["NORMAL"])
        if is_win:
            st["consecutive_losses"] = 0
            return None
        st["consecutive_losses"] += 1
        if st["consecutive_losses"] >= STRATEGY_MAX_CONSECUTIVE_LOSSES:
            st["cooldown_until"] = time.time() + STRATEGY_COOLDOWN_SEC
            code = self._COOLDOWN_RC.get(strategy, RC.TRADE_BLOCKED_STRATEGY_COOLDOWN)
            cooldown_end = time.strftime(
                "%H:%M UTC", time.gmtime(st["cooldown_until"])
            )
            log.warning(
                RC.fmt(code,
                       strategy=strategy,
                       losses=st["consecutive_losses"],
                       cooldown_until=cooldown_end)
            )
            return code
        return None

    def is_in_cooldown(self, strategy: str) -> Tuple[bool, float]:
        """
        Retorna (in_cooldown, remaining_seconds).
        Si el cooldown ya expiró lo resetea automáticamente.
        """
        st    = self._state.get(strategy, self._state["NORMAL"])
        until = st.get("cooldown_until", 0.0)
        if time.time() < until:
            return True, until - time.time()
        if st["consecutive_losses"] >= STRATEGY_MAX_CONSECUTIVE_LOSSES:
            # Cooldown expiró → reset
            st["consecutive_losses"] = 0
            log.info(RC.fmt(RC.ENGINE_STRATEGY_COOLDOWN_EXPIRED, strategy=strategy))
        return False, 0.0

    def consecutive(self, strategy: str) -> int:
        return self._state.get(strategy, {}).get("consecutive_losses", 0)

    def to_dict(self) -> Dict:
        return {k: dict(v) for k, v in self._state.items()}

    def from_dict(self, d: Dict) -> None:
        for s in VALID_STRATEGY_TYPES:
            if s in d:
                self._state[s].update(d[s])


# ══════════════════════════════════════════════════════════
#  RISK MANAGER PRINCIPAL
# ══════════════════════════════════════════════════════════

class RiskManager:
    """
    Gestor de riesgo con Kill-Switches deterministas.
    can_open() debe ser la primera llamada ANTES de cualquier lógica de IA.
    """

    def __init__(self, learner=None) -> None:
        self.learner = learner

        # ── Estado volátil ────────────────────────────────────────────────────
        self.daily_pnl:          float = 0.0
        self._day_start_ts:      int   = _utc_day_start()
        self.consecutive_losses: int   = 0
        self.open_symbols:       set   = set()

        # ── KS-2: Daily DD kill-switch ────────────────────────────────────────
        self._dd_locked:         bool  = False
        self._dd_unlock_ts:      int   = 0      # próximas 00:00 UTC

        # ── KS-4: Strategy cooldowns ──────────────────────────────────────────
        self.strategy = _StrategyTracker()

        # ── Circuit breaker global (demasiadas pérdidas globales) ─────────────
        self._global_cb:         bool  = False
        self._global_cb_until:   float = 0.0
        self.MAX_CONSECUTIVE_GLOBAL:   int   = 6
        self.GLOBAL_CB_PAUSE_SEC:      int   = 3600

        # ── Umbrales configurables ────────────────────────────────────────────
        self.DAILY_DD_KILL_PCT  = DAILY_DD_KILL_PCT   # -5.0
        self.MAX_EXPOSURE_PCT   = MAX_EXPOSURE_PCT     # 15.0

        # ── Cargar estado persistente ─────────────────────────────────────────
        self._load()

        log.info(
            f"RiskManager v3 | "
            f"daily_pnl={self.daily_pnl:+.2f} | "
            f"dd_locked={self._dd_locked} | "
            f"consec={self.consecutive_losses}"
        )

    # ══════════════════════════════════════════════════════
    #  PERSISTENCIA
    # ══════════════════════════════════════════════════════

    def _save(self) -> None:
        state = {
            "daily_pnl":         self.daily_pnl,
            "day_start_ts":      self._day_start_ts,
            "dd_locked":         self._dd_locked,
            "dd_unlock_ts":      self._dd_unlock_ts,
            "consecutive_losses":self.consecutive_losses,
            "global_cb":         self._global_cb,
            "global_cb_until":   self._global_cb_until,
            "strategy":          self.strategy.to_dict(),
            "saved_at":          int(time.time()),
        }
        try:
            with open(_STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            log.debug(RC.fmt(RC.RISK_STATE_SAVED))
        except Exception as e:
            log.error(f"Error guardando risk_state: {e}")

    def _load(self) -> None:
        if not os.path.exists(_STATE_FILE):
            return
        try:
            with open(_STATE_FILE) as f:
                s = json.load(f)

            saved_day = int(s.get("day_start_ts", 0))
            today     = _utc_day_start()

            if saved_day >= today:
                # Mismo día UTC → restaurar estado completo
                self.daily_pnl          = float(s.get("daily_pnl", 0.0))
                self._day_start_ts      = saved_day
                self._dd_locked         = bool(s.get("dd_locked", False))
                self._dd_unlock_ts      = int(s.get("dd_unlock_ts", 0))
                self.consecutive_losses = int(s.get("consecutive_losses", 0))
                self._global_cb         = bool(s.get("global_cb", False))
                self._global_cb_until   = float(s.get("global_cb_until", 0.0))
                self.strategy.from_dict(s.get("strategy", {}))
                log.info(
                    RC.fmt(RC.RISK_STATE_LOADED,
                           daily_pnl=f"{self.daily_pnl:+.2f}",
                           dd_locked=self._dd_locked)
                )
            else:
                # Nuevo día UTC → reset de métricas diarias, preservar el resto
                log.info(RC.fmt(RC.RISK_DAILY_DD_RESET, prev_day_pnl=f"{s.get('daily_pnl',0):+.2f}"))
                self.daily_pnl     = 0.0
                self._day_start_ts = today
                self._dd_locked    = False
                self._dd_unlock_ts = 0
                # consecutive_losses y global_cb se preservan entre días
                self.consecutive_losses = int(s.get("consecutive_losses", 0))
                self._global_cb         = bool(s.get("global_cb", False))
                self._global_cb_until   = float(s.get("global_cb_until", 0.0))
                self.strategy.from_dict(s.get("strategy", {}))
        except Exception as e:
            log.error(f"Error cargando risk_state: {e}")

    # ══════════════════════════════════════════════════════
    #  RESET DIARIO UTC
    # ══════════════════════════════════════════════════════

    def _check_daily_reset(self) -> None:
        """Resetea métricas diarias si cambió el día UTC. Llamado en can_open()."""
        today = _utc_day_start()
        if today > self._day_start_ts:
            prev = self.daily_pnl
            self.daily_pnl     = 0.0
            self._day_start_ts = today
            self._dd_locked    = False
            self._dd_unlock_ts = 0
            self._save()
            log.info(RC.fmt(RC.RISK_DAILY_DD_RESET,
                             prev_pnl=f"{prev:+.2f}", reset="00:00 UTC"))

    # ══════════════════════════════════════════════════════
    #  KS-2: DAILY DRAWDOWN KILL-SWITCH
    # ══════════════════════════════════════════════════════

    def _eval_daily_dd(self, balance: float) -> Tuple[bool, str]:
        """
        Evalúa el Kill-Switch de Daily Drawdown.
        Retorna (blocked=True, reason_msg) si está activo.
        """
        if self._dd_locked:
            secs_left = max(0, _utc_day_start() + 86400 - int(time.time()))
            h, m = divmod(secs_left // 60, 60)
            return True, RC.fmt(
                RC.TRADE_BLOCKED_DAILY_DD,
                daily_pnl=f"{self.daily_pnl:+.2f}",
                resets_in=f"{h}h{m}m UTC"
            )

        if balance <= 0:
            return False, ""

        pct = (self.daily_pnl / balance) * 100.0
        if pct <= self.DAILY_DD_KILL_PCT:
            self._dd_locked    = True
            self._dd_unlock_ts = _utc_day_start() + 86400
            self._save()
            msg = RC.fmt(
                RC.RISK_DAILY_DD_LOCK,
                pct=f"{pct:.2f}%",
                limit=f"{self.DAILY_DD_KILL_PCT}%",
                daily_pnl=f"{self.daily_pnl:+.2f}",
                unlocks="00:00 UTC"
            )
            log.warning(msg)
            return True, msg

        return False, ""

    # ══════════════════════════════════════════════════════
    #  KS-5: MAX TOTAL EXPOSURE
    # ══════════════════════════════════════════════════════

    def _eval_exposure(
        self,
        balance: float,
        open_positions: Dict[str, Dict],
    ) -> Tuple[bool, str]:
        """
        Calcula margen total utilizado.
        margen = entry_price × qty / leverage  por posición.
        Bloquea si total_margin / balance >= MAX_EXPOSURE_PCT / 100.
        """
        if balance <= 0 or not open_positions:
            return False, ""

        total_margin = 0.0
        for pos in open_positions.values():
            ep  = float(pos.get("entry_price") or 0)
            qty = float(pos.get("qty")         or 0)
            lev = float(pos.get("leverage")    or 1)
            if ep > 0 and qty > 0 and lev > 0:
                total_margin += (ep * qty) / lev

        exposure_pct = (total_margin / balance) * 100.0
        log.debug(
            RC.fmt(RC.RISK_EXPOSURE_CHECKED,
                   margin=f"{total_margin:.2f} USDT",
                   pct=f"{exposure_pct:.1f}%",
                   limit=f"{self.MAX_EXPOSURE_PCT}%")
        )

        if exposure_pct >= self.MAX_EXPOSURE_PCT:
            msg = RC.fmt(
                RC.TRADE_BLOCKED_MAX_EXPOSURE,
                total_margin=f"{total_margin:.2f} USDT",
                exposure=f"{exposure_pct:.1f}%",
                limit=f"{self.MAX_EXPOSURE_PCT}%",
                open_positions=len(open_positions)
            )
            log.warning(msg)
            return True, msg

        return False, ""

    # ══════════════════════════════════════════════════════
    #  API PRINCIPAL: can_open()
    # ══════════════════════════════════════════════════════

    def can_open(
        self,
        symbol:         str,
        score:          float,
        balance:        float         = 0.0,
        open_positions: Optional[Dict[str, Dict]] = None,
        strategy_type:  str           = "NORMAL",
        news_freeze:    bool          = False,
    ) -> Tuple[bool, str]:
        """
        Evaluación completa de Kill-Switches.
        Retorna (True, "") si se puede operar, (False, reason_msg) si no.

        ORDEN DETERMINISTA (más crítico primero):
          1. Reset diario UTC
          2. Circuit breaker global de pérdidas consecutivas
          3. KS-2  Daily DD kill-switch
          4. KS-3  News freeze (pasado como parámetro externo)
          5. KS-4  Strategy cooldown
          6. KS-5  Max total exposure
        """
        open_positions = open_positions or {}

        # ── 1. Reset diario ────────────────────────────────────────────────────
        self._check_daily_reset()

        # ── 2. Circuit breaker global ──────────────────────────────────────────
        if self._global_cb:
            if time.time() < self._global_cb_until:
                rem = int(self._global_cb_until - time.time())
                return False, RC.fmt(
                    RC.TRADE_BLOCKED_CIRCUIT_BREAKER,
                    symbol=symbol,
                    type="GLOBAL_LOSS_STREAK",
                    remaining_s=rem
                )
            else:
                self._global_cb         = False
                self.consecutive_losses = 0
                self._save()
                log.info(RC.fmt(RC.RISK_CIRCUIT_RESET))

        # ── 3. KS-2: Daily DD kill-switch ──────────────────────────────────────
        if balance > 0:
            blocked, msg = self._eval_daily_dd(balance)
            if blocked:
                return False, msg

        # ── 4. KS-3: News freeze (el bot_autonomous ya consultó news.is_news_freeze_active) ──
        if news_freeze:
            return False, RC.fmt(RC.TRADE_BLOCKED_NEWS_WINDOW, symbol=symbol)

        # ── 5. KS-4: Strategy cooldown ─────────────────────────────────────────
        stype = strategy_type.upper()
        if stype not in VALID_STRATEGY_TYPES:
            stype = "NORMAL"
        in_cd, remaining = self.strategy.is_in_cooldown(stype)
        if in_cd:
            h, m = divmod(int(remaining) // 60, 60)
            return False, RC.fmt(
                RC.TRADE_BLOCKED_STRATEGY_COOLDOWN,
                strategy=stype,
                remaining=f"{h}h{m}m",
                symbol=symbol
            )

        # ── 6. KS-5: Max total exposure ────────────────────────────────────────
        if balance > 0 and open_positions:
            blocked, msg = self._eval_exposure(balance, open_positions)
            if blocked:
                return False, msg

        return True, ""

    # ══════════════════════════════════════════════════════
    #  REGISTRO DE EVENTOS
    # ══════════════════════════════════════════════════════

    def on_open(self, symbol: str) -> None:
        self.open_symbols.add(symbol)

    def on_close(
        self,
        symbol:        str,
        pnl:           float,
        strategy_type: str = "NORMAL",
    ) -> None:
        """
        Registra cierre de posición.
        Actualiza daily PnL, consecutive losses global y strategy tracker.
        Persiste estado tras cada cierre.
        """
        self.open_symbols.discard(symbol)
        self.daily_pnl = round(self.daily_pnl + pnl, 4)

        is_win = pnl > 0.5

        if is_win:
            if self.consecutive_losses > 0:
                log.info(
                    RC.fmt(RC.RISK_CONSECUTIVE_RESET,
                           prev_streak=self.consecutive_losses,
                           symbol=symbol)
                )
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            log.warning(
                RC.fmt(RC.RISK_CONSECUTIVE_LOSS,
                       streak=self.consecutive_losses,
                       pnl=f"{pnl:+.2f}",
                       symbol=symbol)
            )
            # Circuit breaker global
            if self.consecutive_losses >= self.MAX_CONSECUTIVE_GLOBAL:
                self._global_cb       = True
                self._global_cb_until = time.time() + self.GLOBAL_CB_PAUSE_SEC
                log.warning(
                    RC.fmt(RC.RISK_CIRCUIT_ARMED,
                           consecutive=self.consecutive_losses,
                           pause_min=self.GLOBAL_CB_PAUSE_SEC // 60)
                )

        # Strategy tracker
        stype = strategy_type.upper()
        if stype not in VALID_STRATEGY_TYPES:
            stype = "NORMAL"
        self.strategy.record(stype, is_win)
        self._save()

    # ══════════════════════════════════════════════════════
    #  STATUS
    # ══════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        secs_to_reset = max(0, _utc_day_start() + 86400 - int(time.time()))
        h, m = divmod(secs_to_reset // 60, 60)

        strategy_status: Dict = {}
        for s in VALID_STRATEGY_TYPES:
            in_cd, rem = self.strategy.is_in_cooldown(s)
            strategy_status[s] = {
                "consecutive_losses": self.strategy.consecutive(s),
                "in_cooldown":        in_cd,
                "remaining_s":        int(rem),
            }

        return {
            "daily_pnl":          round(self.daily_pnl, 2),
            "dd_locked":          self._dd_locked,
            "dd_kill_pct":        self.DAILY_DD_KILL_PCT,
            "utc_reset_in":       f"{h}h{m}m",
            "consecutive_losses": self.consecutive_losses,
            "global_cb":          self._global_cb,
            "global_cb_until":    self._global_cb_until,
            "global_cb_rem_s":    max(0, int(self._global_cb_until - time.time())),
            "open_symbols":       list(self.open_symbols),
            "max_exposure_pct":   self.MAX_EXPOSURE_PCT,
            "strategy":           strategy_status,
        }
