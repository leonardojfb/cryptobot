"""
reason_codes.py — Fuente Única de Verdad para Códigos de Razón
══════════════════════════════════════════════════════════════════════
Todo log.warning, log.info y mensaje de Telegram usa estos códigos.
Nunca escribas strings literales de bloqueo fuera de este módulo.

USO:
    from reason_codes import RC
    log.warning(RC.fmt(RC.TRADE_BLOCKED_DAILY_DD, pct="-5.2%", sym="BTCUSDT"))
    tg.send(RC.tg(RC.API_CIRCUIT_BREAKER_ACTIVATED, errors=3, pause_min=15))
"""
from __future__ import annotations
from typing import Any

# ── Constantes de duración reutilizables ──────────────────────────────────────
STRATEGY_MAX_CONSECUTIVE_LOSSES: int   = 4       # pérdidas → cooldown de estrategia
STRATEGY_COOLDOWN_HOURS:         int   = 12      # horas de cooldown por estrategia
STRATEGY_COOLDOWN_SEC:           int   = STRATEGY_COOLDOWN_HOURS * 3600

DAILY_DD_KILL_PCT:               float = -5.0    # % drawdown diario → kill-switch
MAX_EXPOSURE_PCT:                float = 15.0    # % margen → bloqueo por exposición

API_CB_MAX_ERRORS:               int   = 3       # errores consecutivos → circuit breaker
API_CB_WINDOW_SEC:               int   = 5 * 60  # ventana de tiempo para contar errores
API_CB_PAUSE_SEC:                int   = 15 * 60 # pausa tras activar circuit breaker

NEWS_FREEZE_PRE_MIN:             int   = 30      # minutos ANTES del evento macro
NEWS_FREEZE_POST_MIN:            int   = 30      # minutos DESPUÉS del evento macro

# Mapeo entry_mode → strategy_type (para cooldown por estrategia)
ENTRY_MODE_TO_STRATEGY: dict[str, str] = {
    "AGGRESSIVE": "FAST",
    "MOMENTUM":   "FAST",
    "STANDARD":   "NORMAL",
    "SYNCED":     "NORMAL",
}
VALID_STRATEGY_TYPES: tuple[str, ...] = ("FAST", "NORMAL", "INSTITUTIONAL")


class _RC:
    """Contenedor de todos los Reason Codes del sistema."""

    # ── BLOQUEOS DE TRADE ─────────────────────────────────────────────────────
    TRADE_BLOCKED_DAILY_DD          = "TRADE_BLOCKED_DAILY_DD"
    TRADE_BLOCKED_NEWS_WINDOW       = "TRADE_BLOCKED_NEWS_WINDOW"
    TRADE_BLOCKED_MAX_EXPOSURE      = "TRADE_BLOCKED_MAX_EXPOSURE"
    TRADE_BLOCKED_CIRCUIT_BREAKER   = "TRADE_BLOCKED_CIRCUIT_BREAKER"
    TRADE_BLOCKED_STRATEGY_COOLDOWN = "TRADE_BLOCKED_STRATEGY_COOLDOWN"
    TRADE_BLOCKED_SCORE_LOW         = "TRADE_BLOCKED_SCORE_LOW"
    TRADE_BLOCKED_CONF_LOW          = "TRADE_BLOCKED_CONF_LOW"
    TRADE_BLOCKED_MAX_POSITIONS     = "TRADE_BLOCKED_MAX_POSITIONS"
    TRADE_BLOCKED_COOLDOWN          = "TRADE_BLOCKED_COOLDOWN"
    TRADE_BLOCKED_AI_REJECTION      = "TRADE_BLOCKED_AI_REJECTION"
    TRADE_BLOCKED_NEWS_SENTIMENT    = "TRADE_BLOCKED_NEWS_SENTIMENT"
    TRADE_BLOCKED_SCORE_ADJ         = "TRADE_BLOCKED_SCORE_ADJ"
    TRADE_BLOCKED_SYMBOL_WR         = "TRADE_BLOCKED_SYMBOL_WR"
    TRADE_BLOCKED_QTY_INVALID       = "TRADE_BLOCKED_QTY_INVALID"
    TRADE_BLOCKED_BALANCE_LOW       = "TRADE_BLOCKED_BALANCE_LOW"

    # ── CIERRES DE POSICIÓN ───────────────────────────────────────────────────
    TRADE_CLOSED_TP                 = "TRADE_CLOSED_TP"
    TRADE_CLOSED_SL                 = "TRADE_CLOSED_SL"
    TRADE_CLOSED_MANUAL             = "TRADE_CLOSED_MANUAL"
    TRADE_CLOSED_EXTERNAL           = "TRADE_CLOSED_EXTERNAL"
    TRADE_CLOSED_CLOSE_ALL          = "TRADE_CLOSED_CLOSE_ALL"
    TRADE_CLOSED_POSITION_GHOST     = "TRADE_CLOSED_POSITION_GHOST"
    TRADE_CLOSED_TRAILING_SL        = "TRADE_CLOSED_TRAILING_SL"

    # ── MOTOR DE RIESGO ───────────────────────────────────────────────────────
    RISK_DAILY_DD_LOCK              = "RISK_DAILY_DD_LOCK"
    RISK_DAILY_DD_RESET             = "RISK_DAILY_DD_RESET"
    RISK_CONSECUTIVE_LOSS           = "RISK_CONSECUTIVE_LOSS"
    RISK_CONSECUTIVE_RESET          = "RISK_CONSECUTIVE_RESET"
    RISK_CIRCUIT_ARMED              = "RISK_CIRCUIT_ARMED"
    RISK_CIRCUIT_RESET              = "RISK_CIRCUIT_RESET"
    RISK_EXPOSURE_CHECKED           = "RISK_EXPOSURE_CHECKED"
    RISK_STATE_SAVED                = "RISK_STATE_SAVED"
    RISK_STATE_LOADED               = "RISK_STATE_LOADED"

    # ── COOLDOWN POR ESTRATEGIA ───────────────────────────────────────────────
    ENGINE_FAST_COOLDOWN_4_LOSSES        = "ENGINE_FAST_COOLDOWN_4_LOSSES"
    ENGINE_NORMAL_COOLDOWN_4_LOSSES      = "ENGINE_NORMAL_COOLDOWN_4_LOSSES"
    ENGINE_INSTITUTIONAL_COOLDOWN_4_LOSSES = "ENGINE_INSTITUTIONAL_COOLDOWN_4_LOSSES"
    ENGINE_STRATEGY_COOLDOWN_EXPIRED     = "ENGINE_STRATEGY_COOLDOWN_EXPIRED"

    # ── API / EXCHANGE ────────────────────────────────────────────────────────
    API_CIRCUIT_BREAKER_ACTIVATED   = "API_CIRCUIT_BREAKER_ACTIVATED"
    API_CIRCUIT_BREAKER_LIFTED      = "API_CIRCUIT_BREAKER_LIFTED"
    API_ORDER_ERROR                 = "API_ORDER_ERROR"
    API_ORDER_SUCCESS               = "API_ORDER_SUCCESS"
    API_LEVERAGE_ERROR              = "API_LEVERAGE_ERROR"
    API_NETWORK_ERROR               = "API_NETWORK_ERROR"
    API_CONSECUTIVE_ERRORS          = "API_CONSECUTIVE_ERRORS"

    # ── NOTICIAS / MACRO ──────────────────────────────────────────────────────
    NEWS_FREEZE_PRE_EVENT           = "NEWS_FREEZE_PRE_EVENT"
    NEWS_FREEZE_POST_EVENT          = "NEWS_FREEZE_POST_EVENT"
    NEWS_FREEZE_LIFTED              = "NEWS_FREEZE_LIFTED"
    NEWS_MACRO_EVENT_UPCOMING       = "NEWS_MACRO_EVENT_UPCOMING"
    NEWS_CRITICAL_ALERT             = "NEWS_CRITICAL_ALERT"

    # ── SISTEMA ───────────────────────────────────────────────────────────────
    SYSTEM_BOT_STARTED              = "SYSTEM_BOT_STARTED"
    SYSTEM_BOT_STOPPED              = "SYSTEM_BOT_STOPPED"
    SYSTEM_PAUSED                   = "SYSTEM_PAUSED"

    # ── Descripciones legibles (para Telegram) ────────────────────────────────
    _DESC: dict[str, str] = {
        "TRADE_BLOCKED_DAILY_DD":
            "🔒 Trade bloqueado: Daily Drawdown Kill-Switch activo",
        "TRADE_BLOCKED_NEWS_WINDOW":
            "❄️ Trade bloqueado: Ventana de congelamiento macro activa",
        "TRADE_BLOCKED_MAX_EXPOSURE":
            "⚠️ Trade bloqueado: Exposición total supera el 15% del balance",
        "TRADE_BLOCKED_CIRCUIT_BREAKER":
            "🚨 Trade bloqueado: API Circuit Breaker activo",
        "TRADE_BLOCKED_STRATEGY_COOLDOWN":
            "⏸ Trade bloqueado: Estrategia en cooldown por 4 pérdidas consecutivas",
        "RISK_DAILY_DD_LOCK":
            "🔒 KILL-SWITCH: Daily Drawdown -5% alcanzado — bloqueado hasta 00:00 UTC",
        "RISK_DAILY_DD_RESET":
            "✅ Kill-Switch diario reseteado — nuevo día UTC",
        "ENGINE_FAST_COOLDOWN_4_LOSSES":
            "⏸ Estrategia FAST en cooldown 12h: 4 pérdidas consecutivas",
        "ENGINE_NORMAL_COOLDOWN_4_LOSSES":
            "⏸ Estrategia NORMAL en cooldown 12h: 4 pérdidas consecutivas",
        "ENGINE_INSTITUTIONAL_COOLDOWN_4_LOSSES":
            "⏸ Estrategia INSTITUTIONAL en cooldown 12h: 4 pérdidas consecutivas",
        "API_CIRCUIT_BREAKER_ACTIVATED":
            "🚨 API Circuit Breaker ACTIVADO: 3 errores en 5 min → pausa 15 min",
        "API_CIRCUIT_BREAKER_LIFTED":
            "✅ API Circuit Breaker LEVANTADO — operaciones reanudadas",
        "NEWS_FREEZE_PRE_EVENT":
            "❄️ News Freeze: 30 min ANTES de evento macro HIGH_IMPACT",
        "NEWS_FREEZE_POST_EVENT":
            "❄️ News Freeze: 30 min DESPUÉS de evento macro HIGH_IMPACT",
        "NEWS_FREEZE_LIFTED":
            "✅ News Freeze levantado — ventana macro cerrada",
    }

    def desc(self, code: str) -> str:
        return self._DESC.get(code, code)

    def fmt(self, code: str, **kw: Any) -> str:
        """Para logs: '[CODE] Descripción | k=v | k=v'"""
        base = f"[{code}] {self.desc(code)}"
        if kw:
            base += " | " + " | ".join(f"{k}={v}" for k, v in kw.items())
        return base

    def tg(self, code: str, **kw: Any) -> str:
        """Para Telegram: negrita + parámetros en lista."""
        body = f"<b>[{code}]</b>\n{self.desc(code)}"
        if kw:
            body += "\n" + "\n".join(f"  • <code>{k}</code>: {v}"
                                      for k, v in kw.items())
        return body


RC = _RC()
