"""
ai_filter.py v4 — IA como Lead Institutional Quant & Risk Manager
══════════════════════════════════════════════════════════════════
v4 añade: Active Trade Management (ATM)
  → evaluate_open_position(): evalúa posiciones ABIERTAS y decide la acción
  → Recibe el wakeup reason (BAR_CLOSE / EMERGENCY_VOLATILITY / EMERGENCY_NEWS)
  → Recibe el strategy_type (FAST / NORMAL / INSTITUTIONAL)
  → La IA adapta su paciencia y agresividad según la estrategia y el motivo

JSON de respuesta ATM:
  {
    "action":     "HOLD|MOVE_SL_TO_BREAKEVEN|CLOSE|TRAIL_STOP|PARTIAL_CLOSE",
    "confidence": 0.0-1.0,
    "new_sl":     float | null,   ← solo para TRAIL_STOP
    "reasoning":  "max 200 chars"
  }

.env (sin cambios respecto a v3):
    DEEPSEEK_API_KEY=sk-xxx
    AI_FILTER_ENABLED=true
    AI_FILTER_MIN_CONFIDENCE=0.55
    AI_FILTER_TIMEOUT=15
    AI_MAX_LEVERAGE_CAP=50
"""

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv
import ai_memory

load_dotenv()
log = logging.getLogger("ai_filter")

DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "").strip()
AI_FILTER_ENABLED = os.getenv("AI_FILTER_ENABLED", "true").lower() in ("1","true","yes")
AI_MIN_CONFIDENCE = float(os.getenv("AI_FILTER_MIN_CONFIDENCE", "0.55"))
AI_TIMEOUT        = int(os.getenv("AI_FILTER_TIMEOUT", "15"))
AI_MAX_LEV_CAP    = int(os.getenv("AI_MAX_LEVERAGE_CAP", "50"))
DEEPSEEK_URL      = "https://api.deepseek.com/chat/completions"
MODEL             = "deepseek-chat"

# Confianza mínima para que la IA tome una acción no-HOLD en ATM
# (más alto que el umbral de entrada para ser más conservadores con posiciones abiertas)
ATM_MIN_CONFIDENCE = 0.65

_cache: Dict[str, Dict] = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 30

_FAIL_OPEN_RESULT = {
    "smc_analysis":         "Sin análisis — AI no disponible",
    "news_impact":          "NEUTRAL",
    "approve":              True,
    "confidence":           0.5,
    "recommended_leverage": 5,
    "reasoning":            "AI no disponible — fail-open con leverage conservador",
}

# ATM fail-safe: HOLD (no hacer nada si la IA falla)
# Es más seguro no tocar una posición abierta que cerrarla por error
_FAIL_OPEN_ATM = {
    "action":     "HOLD",
    "confidence": 0.5,
    "new_sl":     None,
    "reasoning":  "IA no disponible — manteniendo posición sin cambios",
    "_source":    "fail_open",
}

VALID_ATM_ACTIONS = frozenset([
    "HOLD", "MOVE_SL_TO_BREAKEVEN", "CLOSE", "TRAIL_STOP", "PARTIAL_CLOSE"
])

# ── System Prompt de ENTRADA (trades nuevos) ──────────────────────────────────
SYSTEM_PROMPT = """Eres el Lead Institutional Quant & Risk Manager de un hedge fund de criptomonedas.
Recibes señales de un bot algorítmico con análisis SMC (Smart Money Concepts), datos de order flow,
noticias en tiempo real y tu historial de decisiones anteriores.

TU RESPONSABILIDAD:
1. Analizar los datos crudos SMC para determinar si el setup es institucional válido
2. Evaluar el impacto de las noticias en el riesgo del trade
3. Aprobar o rechazar el trade
4. Asignar dinámicamente el apalancamiento óptimo según la calidad del setup y el riesgo

CONCEPTOS SMC QUE DEBES ENTENDER:
- Order Block (OB): zona de oferta/demanda institucional. Precio en OB fresco = alta probabilidad
- Fair Value Gap (FVG): imbalance no rellenado. Precio en FVG = instituciones rellenan
- Liquidity Sweep: stop hunt antes del movimiento real. Sweep fresco + OB = setup premium
- VWAP Retest: precio justo institucional. Rebote en VWAP = entry preciso
- BoS (Break of Structure): confirmación de continuación. ChoCH: posible reversión

REGLAS DE APALANCAMIENTO DINÁMICO:
Alta calidad (APPROVE con leverage alto 20x-50x):
  - Sweep + OB frescos + VWAP retest + noticias neutrales/positivas + score > 5.0
  - Alineación macro completa + ChoCH/BoS confirmado + FVG fill
  
Media calidad (APPROVE con leverage moderado 10x-20x):
  - 2-3 setups SMC coincidentes + noticias neutras
  - Score 3.5-5.0 con alineación parcial
  
Baja calidad / Mayor incertidumbre (APPROVE con leverage bajo 3x-10x):
  - Solo 1 setup SMC + noticias contradictorias o inciertas
  - Score 2.5-3.5 en modo AGGRESSIVE/MOMENTUM
  - Mercado de alta volatilidad (ATR muy elevado vs precio)
  - Fear & Greed extremo (< 20 o > 85)

RECHAZAR si:
  - Macro opuesta a la señal
  - Noticia crítica negativa reciente (HIGH_NEGATIVE) + trade en esa dirección
  - Sin ningún setup SMC confirmado en TFs relevantes
  - Historial del par WR < 25% en últimos 10+ trades
  - Tus rechazos anteriores en condiciones similares resultaron correctos

REGLAS ESTRICTAS:
1. Responde SOLO con un objeto JSON válido, sin markdown, sin texto adicional.
2. Estructura EXACTA — no puedes omitir ningún campo:
   {"smc_analysis": "...", "news_impact": "...", "approve": true/false, "confidence": 0.0-1.0, "recommended_leverage": int, "reasoning": "max 200 chars"}
3. "recommended_leverage": entero entre 1 y 100. Nunca 0.
4. "smc_analysis": describe el setup SMC en máximo 100 chars.
5. "news_impact": exactamente uno de: HIGH_POSITIVE, MODERATE_POSITIVE, NEUTRAL, MODERATE_NEGATIVE, HIGH_NEGATIVE
6. "confidence": tu certeza en la decisión (< 0.50 = muy inseguro → reduce leverage).
7. Aprende del historial: si rechazaste y el mercado te dio la razón, refuerza ese criterio."""


# ── System Prompt de ATM (gestión de posiciones abiertas) ─────────────────────
ATM_SYSTEM_PROMPT = """Eres el Active Trade Manager institucional de un hedge fund de criptomonedas.
Tienes una posición ABIERTA y debes gestionar activamente su riesgo y rentabilidad.

ACCIONES DISPONIBLES (responde con EXACTAMENTE una de estas):
- HOLD: mantener la posición sin cambios
- MOVE_SL_TO_BREAKEVEN: mover stop-loss al precio de entrada (elimina riesgo, solo si en ganancia)
- TRAIL_STOP: actualizar trailing stop a nivel más favorable (DEBES proveer new_sl válido)
- PARTIAL_CLOSE: cerrar 50% de la posición para asegurar ganancias parciales
- CLOSE: cerrar TODA la posición inmediatamente

════════════════════════════════════════════════════════
FILOSOFÍA POR TIPO DE ESTRATEGIA
════════════════════════════════════════════════════════

FAST (scalping 5m) — Paciencia MÍNIMA:
  → Duración máxima esperada: 4-6 velas (20-30 min)
  → Al primer cierre de vela en contra de la estructura: CLOSE
  → Con ganancia ≥ 1.0x ATR: MOVE_SL_TO_BREAKEVEN
  → Con ganancia ≥ 2.0x ATR: PARTIAL_CLOSE o TRAIL_STOP agresivo
  → No tolerar retrocesos > 0.5x ATR cuando ya se está en ganancia

NORMAL (swing 15m) — Balance entre protección y dejar correr:
  → Mover a breakeven cuando ganancia ≥ 1.5x ATR
  → PARTIAL_CLOSE cuando ganancia ≥ 2.5x ATR
  → CLOSE si la estructura de 15m se rompe contra el trade (BoS contrario)
  → Tolerar retrocesos hasta 1.0x ATR si la macro está alineada

INSTITUTIONAL (posición 4h) — Máxima paciencia:
  → Tolerar retrocesos hasta 2.0x ATR (respiración normal del mercado)
  → Solo CLOSE ante: ChoCH en 4h confirmado, cambio de régimen macro,
    noticia HIGH_NEGATIVE directamente relacionada con el activo
  → TRAIL_STOP cuando ganancia ≥ 3.0x ATR para proteger ganancias grandes
  → NEVER cerrar por un simple pullback o corrección técnica menor

════════════════════════════════════════════════════════
LÓGICA SEGÚN MOTIVO DE DESPERTAR (wakeup_reason)
════════════════════════════════════════════════════════

ATM_WAKEUP_BAR_CLOSE (evaluación rutinaria):
  → Analizar si la estructura del timeframe base sigue intacta
  → Si la estructura favorece el trade: HOLD o actualizar trailing
  → Si hay señales de debilidad confirmadas: según estrategia

ATM_WAKEUP_EMERGENCY_VOLATILITY (spike de precio anormal):
  → PRIORIDAD ABSOLUTA: proteger capital
  → Si el movimiento brusco es EN CONTRA del trade:
    FAST: CLOSE inmediato
    NORMAL: CLOSE si supera 1.5x ATR en contra
    INSTITUTIONAL: MOVE_SL_TO_BREAKEVEN si en ganancia, CLOSE si en pérdida > 2x ATR
  → Si el movimiento brusco es A FAVOR del trade:
    TRAIL_STOP agresivo para asegurar las ganancias

ATM_WAKEUP_EMERGENCY_NEWS (noticia crítica):
  → Si la noticia es contraria al trade (HIGH_NEGATIVE / MODERATE_NEGATIVE):
    FAST: CLOSE inmediato
    NORMAL: PARTIAL_CLOSE o CLOSE según magnitud
    INSTITUTIONAL: evaluar si afecta la tesis macro del trade
  → Si la noticia es favorable (HIGH_POSITIVE):
    Considerar TRAIL_STOP para asegurar ganancias si ya en profit

════════════════════════════════════════════════════════
REGLAS GENERALES PARA TODAS LAS ESTRATEGIAS
════════════════════════════════════════════════════════
- NUNCA mover SL a breakeven si estás en pérdida (empeoraría el SL actual)
- Para TRAIL_STOP: new_sl debe ser MÁS FAVORABLE que el SL actual
  (LONG: new_sl > sl_actual y < precio_actual)
  (SHORT: new_sl < sl_actual y > precio_actual)
- Si confidence < 0.65: preferir HOLD sobre cualquier acción agresiva
- Si el precio ya alcanzó o superó el TP: CLOSE
- Si el precio ya tocó o cruzó el SL: CLOSE

FORMATO DE RESPUESTA — SOLO JSON válido, sin markdown, sin texto extra:
{"action":"HOLD","confidence":0.0-1.0,"new_sl":null,"reasoning":"máximo 200 chars"}

El campo "new_sl" debe ser un número float solo si action=="TRAIL_STOP", null en todos los demás casos."""


class AIFilter:
    def __init__(self):
        self.enabled  = AI_FILTER_ENABLED and bool(DEEPSEEK_API_KEY)
        self.min_conf = AI_MIN_CONFIDENCE
        self.timeout  = AI_TIMEOUT
        self.lev_cap  = AI_MAX_LEV_CAP
        # Stats separados para entry y ATM
        self.stats = {
            "calls": 0, "approved": 0, "rejected": 0, "errors": 0,
            "total_ms": 0, "cache_hits": 0,
        }
        self.atm_stats = {
            "calls": 0, "errors": 0, "total_ms": 0,
            "hold": 0, "move_sl_be": 0, "close": 0,
            "trail_stop": 0, "partial_close": 0,
        }
        if AI_FILTER_ENABLED and not DEEPSEEK_API_KEY:
            log.warning("AI_FILTER_ENABLED=true pero DEEPSEEK_API_KEY vacío — desactivado")
            self.enabled = False
        ai_memory.init_db()
        log.info(f"AIFilter v4 {'✅ Activo model='+MODEL if self.enabled else '⚫ Desactivado'}")

    # ══════════════════════════════════════════════════════
    #  ENTRADA: should_trade() — evalúa señal para abrir trade
    # ══════════════════════════════════════════════════════

    def should_trade(
        self,
        analysis: Dict,
        symbol_stats: Dict = None,
        news_bias: Dict = None,
        recent_news: List[Dict] = None,
        trade_id: str = None,
    ) -> Dict:
        if not self.enabled:
            return {**_FAIL_OPEN_RESULT, "_source": "disabled",
                    "recommended_leverage": 5}

        sym    = analysis.get("symbol", "")
        signal = analysis.get("signal", "")

        cached = self._get_cache(sym, signal)
        if cached:
            self.stats["cache_hits"] += 1
            return {**cached, "_source": "cache"}

        symbol_history = ai_memory.get_symbol_history(sym, limit=12)
        user_msg = self._build_prompt(
            analysis, symbol_stats or {}, news_bias or {},
            recent_news or [], symbol_history
        )

        t0         = time.time()
        result     = self._call_deepseek(user_msg, system=SYSTEM_PROMPT)
        elapsed_ms = int((time.time() - t0) * 1000)
        self.stats["calls"]    += 1
        self.stats["total_ms"] += elapsed_ms

        if result is None:
            self.stats["errors"] += 1
            log.warning(f"AIFilter [{sym}] API error → fail-open con lev=5 ({elapsed_ms}ms)")
            return {**_FAIL_OPEN_RESULT, "_source": "fail_open"}

        approve    = bool(result.get("approve", True))
        confidence = float(result.get("confidence", 0.5))
        rec_lev    = int(result.get("recommended_leverage", 5))
        news_imp   = str(result.get("news_impact", "NEUTRAL"))

        rec_lev = max(1, min(rec_lev, self.lev_cap))
        result["recommended_leverage"] = rec_lev

        valid_impacts = {
            "HIGH_POSITIVE", "MODERATE_POSITIVE", "NEUTRAL",
            "MODERATE_NEGATIVE", "HIGH_NEGATIVE"
        }
        if news_imp not in valid_impacts:
            result["news_impact"] = "NEUTRAL"

        if confidence < 0.45:
            log.info(f"AIFilter [{sym}] IA indecisa ({confidence:.0%}) → fail-open lev={min(5, rec_lev)}")
            result["approve"]              = True
            result["recommended_leverage"] = min(5, rec_lev)
            result["_source"]              = "fail_open_indecise"
            if trade_id:
                ai_memory.save_decision(trade_id, analysis, result,
                                        news_bias or {}, symbol_stats or {}, recent_news or [])
            return result

        if trade_id:
            ai_memory.save_decision(trade_id, analysis, result,
                                    news_bias or {}, symbol_stats or {}, recent_news or [])

        self._set_cache(sym, signal, result)

        if not approve and confidence >= self.min_conf:
            self.stats["rejected"] += 1
            log.info(
                f"🚫 [{sym}] RECHAZADO ({confidence:.0%}) "
                f"{result.get('reasoning','')} [{elapsed_ms}ms]"
            )
        else:
            self.stats["approved"] += 1
            log.info(
                f"✅ [{sym}] APROBADO ({confidence:.0%}) "
                f"lev={rec_lev}x {result.get('reasoning','')} [{elapsed_ms}ms]"
            )

        result["_source"] = "ai"
        return result

    # ══════════════════════════════════════════════════════
    #  ATM: evaluate_open_position() — gestiona trades abiertos
    # ══════════════════════════════════════════════════════

    def evaluate_open_position(
        self,
        pos:          Dict,
        current_price: float,
        wakeup_reason: str,
        news_bias:     Dict = None,
        recent_news:   List[Dict] = None,
    ) -> Dict:
        """
        Evalúa una posición ABIERTA y decide la acción a tomar.

        Parámetros:
          pos            — dict completo de la posición (entry_price, side, qty,
                           leverage, tp, sl, atr, strategy_type, tf_minutes, ...)
          current_price  — precio de mercado actual
          wakeup_reason  — RC.ATM_WAKEUP_* que explica por qué se llama a la IA
          news_bias      — sentimiento de noticias del símbolo
          recent_news    — últimas noticias relevantes

        Retorna:
          {
            "action":     "HOLD|MOVE_SL_TO_BREAKEVEN|CLOSE|TRAIL_STOP|PARTIAL_CLOSE",
            "confidence": float,
            "new_sl":     float | None,
            "reasoning":  str,
            "_source":    str,
          }

        Fail-safe: si la IA no responde, retorna HOLD (no tocar la posición).
        """
        if not self.enabled:
            return {**_FAIL_OPEN_ATM, "_source": "disabled"}

        sym = pos.get("symbol", "?")

        t0         = time.time()
        user_msg   = self._build_atm_prompt(pos, current_price, wakeup_reason,
                                             news_bias or {}, recent_news or [])
        result     = self._call_deepseek(user_msg, system=ATM_SYSTEM_PROMPT)
        elapsed_ms = int((time.time() - t0) * 1000)

        self.atm_stats["calls"]    += 1
        self.atm_stats["total_ms"] += elapsed_ms

        if result is None:
            self.atm_stats["errors"] += 1
            log.warning(
                f"ATM [{sym}] API error → HOLD fail-safe ({elapsed_ms}ms)"
            )
            return {**_FAIL_OPEN_ATM, "_source": "fail_open"}

        # ── Validar y normalizar respuesta ─────────────────────────────────
        action     = str(result.get("action", "HOLD")).upper().strip()
        confidence = float(result.get("confidence", 0.5))
        new_sl_raw = result.get("new_sl")
        reasoning  = str(result.get("reasoning", "sin razón"))

        # Acción no reconocida → HOLD
        if action not in VALID_ATM_ACTIONS:
            log.warning(
                f"ATM [{sym}] acción inválida '{action}' → HOLD"
            )
            action = "HOLD"

        # Baja confianza → HOLD conservador
        if confidence < ATM_MIN_CONFIDENCE and action != "HOLD":
            log.info(
                f"ATM [{sym}] confianza baja ({confidence:.0%}) → HOLD forzado "
                f"(era {action})"
            )
            action = "HOLD"

        # new_sl solo tiene sentido con TRAIL_STOP
        if action != "TRAIL_STOP":
            new_sl_raw = None

        # Parsear new_sl como float
        new_sl: Optional[float] = None
        if new_sl_raw is not None:
            try:
                new_sl = float(new_sl_raw)
            except (TypeError, ValueError):
                log.warning(f"ATM [{sym}] new_sl inválido: {new_sl_raw!r} → ignorado")
                action = "HOLD"

        # Actualizar stats
        action_key = {
            "HOLD":                "hold",
            "MOVE_SL_TO_BREAKEVEN":"move_sl_be",
            "CLOSE":               "close",
            "TRAIL_STOP":          "trail_stop",
            "PARTIAL_CLOSE":       "partial_close",
        }.get(action, "hold")
        self.atm_stats[action_key] += 1

        log.info(
            f"ATM [{sym}] wakeup={wakeup_reason.split('_')[-1]} | "
            f"action={action} ({confidence:.0%}) | {reasoning[:80]} "
            f"[{elapsed_ms}ms]"
        )

        return {
            "action":     action,
            "confidence": confidence,
            "new_sl":     new_sl,
            "reasoning":  reasoning,
            "_source":    "ai",
        }

    # ── Constructor de prompt ATM ──────────────────────────────────────────────

    def _build_atm_prompt(
        self,
        pos:           Dict,
        current_price: float,
        wakeup_reason: str,
        news_bias:     Dict,
        recent_news:   List[Dict],
    ) -> str:
        sym          = pos.get("symbol", "?")
        side         = pos.get("side", "?")
        entry        = float(pos.get("entry_price", 0) or 0)
        qty          = float(pos.get("qty", 0)         or 0)
        leverage     = int(pos.get("leverage", 1)      or 1)
        tp           = pos.get("tp")
        sl           = pos.get("sl")
        atr          = float(pos.get("atr", 0)         or 0)
        strategy     = pos.get("strategy_type", "NORMAL")
        tf_minutes   = pos.get("tf_minutes", 15)
        open_ts      = int(pos.get("open_ts", time.time()))
        sl_at_be     = pos.get("sl_at_breakeven", False)

        # PnL no realizado
        if entry > 0 and qty > 0:
            pnl_usdt = ((current_price - entry) if side == "LONG"
                        else (entry - current_price)) * qty
            margin   = (entry * qty) / leverage
            pnl_pct  = (pnl_usdt / margin * 100) if margin > 0 else 0
        else:
            pnl_usdt = pnl_pct = 0.0

        # ATR como % del precio
        atr_pct = (atr / entry * 100) if (entry > 0 and atr > 0) else 0

        # Pnl en múltiplos de ATR
        pnl_atr_mult = (abs(pnl_usdt) / (atr * qty)) if (atr > 0 and qty > 0) else 0

        # Distancia del precio actual al SL
        sl_dist_str = "N/A"
        if sl and entry > 0:
            sl_dist_usdt  = abs(current_price - sl)
            sl_dist_pct   = sl_dist_usdt / entry * 100
            sl_dist_atr   = sl_dist_usdt / atr if atr > 0 else 0
            sl_dist_str   = f"{sl_dist_pct:.2f}% ({sl_dist_atr:.1f}x ATR)"

        # Duración del trade
        dur_s   = int(time.time()) - open_ts
        dur_str = (f"{dur_s//60}m {dur_s%60}s" if dur_s < 3600
                   else f"{dur_s//3600}h {(dur_s%3600)//60}m")

        # Descripción del wakeup reason
        wakeup_desc = {
            "ATM_WAKEUP_BAR_CLOSE":
                f"Cierre rutinario de vela de {tf_minutes} minutos",
            "ATM_WAKEUP_EMERGENCY_VOLATILITY":
                "🚨 Spike de precio anormal — evalúa proteger capital PRIMERO",
            "ATM_WAKEUP_EMERGENCY_NEWS":
                "📰 Noticia crítica detectada — evalúa impacto en la tesis del trade",
        }.get(wakeup_reason, wakeup_reason)

        # Guía de estrategia condensada
        strategy_guides = {
            "FAST": (
                "FAST (5m scalping): Paciencia mínima. "
                f"BE si PnL ≥ 1x ATR ({1*atr_pct:.1f}%). "
                f"PARTIAL si PnL ≥ 2x ATR ({2*atr_pct:.1f}%). "
                "Primer cierre de vela contrario → CLOSE."
            ),
            "NORMAL": (
                "NORMAL (15m): Balance. "
                f"BE si PnL ≥ 1.5x ATR ({1.5*atr_pct:.1f}%). "
                f"PARTIAL si PnL ≥ 2.5x ATR ({2.5*atr_pct:.1f}%). "
                "CLOSE si BoS contrario en 15m."
            ),
            "INSTITUTIONAL": (
                "INSTITUTIONAL (4h): Máxima paciencia. "
                f"Tolerar retrocesos hasta 2x ATR ({2*atr_pct:.1f}%). "
                f"TRAIL si PnL ≥ 3x ATR ({3*atr_pct:.1f}%). "
                "Solo CLOSE ante ChoCH en 4h o noticia HIGH_NEGATIVE."
            ),
        }
        strategy_guide = strategy_guides.get(strategy, strategy_guides["NORMAL"])

        # Noticias recientes
        news_dir   = news_bias.get("direction", "NEUTRAL")
        n_score    = news_bias.get("news_score", 0)
        fg         = news_bias.get("fear_greed", 50)
        fg_lbl     = news_bias.get("fg_label", "Neutral")
        should_blk = news_bias.get("should_block", False)
        n_alerts   = news_bias.get("recent_alerts", 0)

        news_section = "Sin noticias disponibles."
        if recent_news:
            lines = []
            for n in recent_news[:4]:
                age = int((time.time() - n.get("ts", time.time())) / 60)
                lines.append(
                    f"  [{n.get('direction','?'):7s} {n.get('sentiment_score',0):+.2f}]"
                    f" hace {age}min → \"{n.get('title','')[:100]}\""
                )
            news_section = "\n".join(lines)

        # Movimiento desde la última evaluación
        last_eval_price = float(pos.get("last_eval_price", entry) or entry)
        move_since_eval = (
            abs(current_price - last_eval_price) / last_eval_price * 100
            if last_eval_price > 0 else 0
        )
        move_direction = ""
        if last_eval_price > 0 and current_price != last_eval_price:
            favorable = (
                (current_price > last_eval_price and side == "LONG") or
                (current_price < last_eval_price and side == "SHORT")
            )
            move_direction = "📈 A favor" if favorable else "📉 En contra"

        return f"""=== GESTIÓN ACTIVA DE POSICIÓN ABIERTA ===
Par: {sym} | Dirección: {side} | Estrategia: {strategy} | TF base: {tf_minutes}m
Motivo del despertar: {wakeup_reason}
  → {wakeup_desc}

=== ESTADO DE LA POSICIÓN ===
Precio entrada: {entry:.4f} | Precio actual: {current_price:.4f}
PnL no realizado: {pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%) [{pnl_atr_mult:.1f}x ATR]
TP objetivo: {tp or "N/A"} | SL actual: {sl or "N/A"}
  SL en breakeven: {"✅ SÍ" if sl_at_be else "❌ NO"}
  Distancia al SL: {sl_dist_str}
ATR: {atr:.4f} ({atr_pct:.2f}% del precio)
Duración trade: {dur_str} | Qty: {qty} | Leverage: {leverage}x
Movimiento desde última eval: {move_since_eval:.2f}% {move_direction}

=== CONTEXTO DE MERCADO ===
Sentimiento {sym}: {news_dir} (score={n_score:+.2f}) | Alertas críticas 2h: {n_alerts}
Fear & Greed: {fg}/100 — {fg_lbl}
{"⛔ ALERTA CRÍTICA DE NOTICIAS ACTIVA" if should_blk else ""}

Noticias recientes:
{news_section}

=== GUÍA DE ESTRATEGIA {strategy} ===
{strategy_guide}

Analiza la situación completa y decide la MEJOR acción.
RECUERDA: confidence < 0.65 → HOLD automático (sé conservador).
Responde SOLO con JSON: {{"action":"...","confidence":0.0-1.0,"new_sl":null,"reasoning":"..."}}"""

    # ── Constructor de prompt de ENTRADA ──────────────────────────────────────

    def _build_prompt(self, analysis: Dict, sym_stats: Dict, news_bias: Dict,
                      recent_news: List[Dict], symbol_history: List[Dict]) -> str:
        sym   = analysis.get("symbol", "")
        sig   = analysis.get("signal", "")
        score = analysis.get("composite_score", 0)
        conf  = analysis.get("confidence", 0)
        mode  = analysis.get("entry_mode", "STANDARD")
        mark  = analysis.get("mark_price", 0)
        atr   = analysis.get("atr") or 0
        tp    = analysis.get("tp")
        sl    = analysis.get("sl")

        atr_pct   = (atr / mark * 100) if (mark and atr) else 0
        vol_label = ("EXTREMA" if atr_pct > 5 else
                     "ALTA"    if atr_pct > 2 else
                     "NORMAL"  if atr_pct > 0.5 else "BAJA")

        rr_str = "N/A"
        if tp and sl and mark and abs(mark - sl) > 0:
            rr_str = f"1:{abs((tp - mark) / (mark - sl)):.1f}"

        smc_sum   = analysis.get("smc_summary", {})
        smc_sweep = analysis.get("smc_sweep", False)
        smc_ob    = analysis.get("smc_ob_hit", False)
        smc_fvg   = analysis.get("smc_fvg_fill", False)
        smc_vwap  = analysis.get("smc_vwap_retest", False)
        setups_n  = smc_sum.get("setups_count", 0)

        setup_quality = (
            "PREMIUM ⭐⭐⭐" if (smc_sweep and smc_ob and setups_n >= 3) else
            "BUENO ⭐⭐"     if setups_n >= 2 else
            "BÁSICO ⭐"      if setups_n >= 1 else
            "SIN SETUP SMC ❌"
        )

        tf_evidence_str = "Sin evidencia SMC por TF."
        tf_ev = smc_sum.get("tf_evidence", {})
        if tf_ev:
            lines = []
            for label, ev in tf_ev.items():
                parts = [f"{k}: {v}" for k, v in ev.items()]
                lines.append(f"  {label}: {' '.join(parts)}")
            tf_evidence_str = "\n".join(lines)

        tf_details = analysis.get("tf_details", {})
        tf_lines   = []
        for tf in ["1m","5m","15m","30m","1h","4h","1D"]:
            d = tf_details.get(tf)
            if d:
                tf_lines.append(
                    f"  {tf:>4}: score={d.get('score',0):+.1f}  "
                    f"RSI={d.get('rsi','-')}  pd_zone={d.get('pd_zone','?')}"
                )

        hist_str = "Sin historial."
        if sym_stats.get("trades", 0) >= 3:
            t = sym_stats["trades"]; w = sym_stats.get("wins", 0)
            pnl = sym_stats.get("total_pnl", 0)
            hist_str = (
                f"{t} trades | WR={w/t*100:.0f}% | PnL={pnl:+.2f} USDT | "
                f"Best={sym_stats.get('best',0):+.2f} | "
                f"Worst={sym_stats.get('worst',0):+.2f}"
            )

        news_section = "Sin noticias disponibles."
        if recent_news:
            lines = []
            for n in recent_news[:5]:
                age = int((time.time() - n.get("ts", time.time())) / 60)
                lines.append(
                    f"  [{n.get('direction','?'):7s} {n.get('sentiment_score',0):+.2f}]"
                    f" hace {age}min [{n.get('source','')}]\n"
                    f"  \"{n.get('title','')[:100]}\""
                )
            news_section = "\n".join(lines)

        ai_hist = "Sin historial de IA para este par."
        if symbol_history:
            lines = []
            for h in symbol_history[:8]:
                pnl_h   = h.get("pnl_usdt")
                pnl_str = f"{pnl_h:+.2f}" if pnl_h is not None else "abierto"
                lev_h   = h.get("leverage", "?")
                lines.append(
                    f"  {h.get('side','?'):5s} "
                    f"{'✅IA' if h.get('ai_approved') else '🚫IA'}"
                    f" lev={lev_h}x → {h.get('result','?'):9s} "
                    f"PnL={pnl_str} | "
                    f"{(h.get('ai_reasoning') or '')[:60]}"
                )
            ai_hist = "\n".join(lines)

        fg_val  = news_bias.get("fear_greed", 50)
        n_dir   = news_bias.get("news_direction") or news_bias.get("direction", "NEUTRAL")
        n_score = news_bias.get("news_score", 0)
        block   = news_bias.get("should_block", False)
        alerts  = news_bias.get("recent_alerts", 0)
        fg_lbl  = news_bias.get("fg_label", "Neutral")

        base_lev_hint = self._calc_base_leverage_hint(
            setups_n, score, atr_pct, fg_val, news_bias
        )

        return f"""=== SEÑAL SMC A REVISAR ===
Par: {sym} | Dirección: {sig} | Modo: {mode}
Score compuesto: {score:+.2f} | Confianza técnica: {conf:.0%} | R:R: {rr_str}
Precio: {mark} | ATR: {atr:.4f} ({atr_pct:.2f}% del precio) | Volatilidad: {vol_label}
Alineación: {"COMPLETA ✅" if analysis.get("aligned") else "PARCIAL ⚠️"}
Macro: {analysis.get("macro_bias","?")} | Mid: {analysis.get("mid_bias","?")} | Entry: {analysis.get("entry_bias","?")}

=== ANÁLISIS SMC (datos crudos) ===
Calidad del setup: {setup_quality}
• Liquidity Sweep: {"✅ DETECTADO" if smc_sweep else "❌"}
• Order Block Hit: {"✅ DETECTADO" if smc_ob else "❌"}
• FVG Fill:        {"✅ DETECTADO" if smc_fvg else "❌"}
• VWAP Retest:     {"✅ DETECTADO" if smc_vwap else "❌"}

Evidencia por timeframe:
{tf_evidence_str}

Scores por TF:
{chr(10).join(tf_lines) if tf_lines else "  No disponible"}

=== NOTICIAS Y MACRO ===
Sentimiento {sym}: {n_dir} (score={n_score:+.2f}) | Alertas críticas 2h: {alerts}
Fear & Greed: {fg_val}/100 — {fg_lbl}
{"⛔ BLOQUEO ACTIVO: noticia muy negativa" if block else ""}

Noticias recientes:
{news_section}

=== HISTORIAL DEL PAR ===
{hist_str}

=== TUS DECISIONES ANTERIORES EN {sym} ===
{ai_hist}

=== GUÍA DE APALANCAMIENTO ===
Setup PREMIUM (Sweep+OB+VWAP, score>5, noticias neutras): 20x-50x
Setup BUENO (2+ setups SMC, score 3.5-5): 10x-20x
Setup BÁSICO o incierto: 3x-10x
Alta volatilidad (ATR={atr:.4f} = {atr_pct:.1f}%): reducir leverage
F&G extremo (<20 LONG o >85 SHORT): máximo 5x-10x
Sugerencia base del sistema: ~{base_lev_hint}x

Analiza TODO y decide. Responde SOLO con el JSON exacto."""

    def _calc_base_leverage_hint(self, setups_n: int, score: float,
                                  atr_pct: float, fg: int,
                                  news_bias: Dict) -> int:
        lev = 10
        if setups_n >= 3:   lev = 30
        elif setups_n == 2: lev = 20
        elif setups_n == 1: lev = 12
        else:               lev = 5

        if abs(score) > 6:   lev = int(lev * 1.5)
        elif abs(score) > 4: lev = int(lev * 1.2)
        elif abs(score) < 3: lev = int(lev * 0.6)

        if atr_pct > 5:   lev = int(lev * 0.4)
        elif atr_pct > 2: lev = int(lev * 0.65)
        elif atr_pct > 1: lev = int(lev * 0.85)

        if fg < 15 or fg > 90: lev = min(lev, 8)
        elif fg < 25 or fg > 80: lev = min(lev, 15)

        if news_bias.get("should_block"):              lev = min(lev, 3)
        elif news_bias.get("recent_alerts", 0) > 2:   lev = min(lev, 8)

        return max(1, min(lev, self.lev_cap))

    # ── Llamada a DeepSeek ────────────────────────────────────────────────────

    def _call_deepseek(self, user_message: str,
                       system: str = SYSTEM_PROMPT) -> Optional[Dict]:
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       MODEL,
            "max_tokens":  400,
            "temperature": 0.10,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user_message},
            ],
        }
        try:
            resp = requests.post(DEEPSEEK_URL, headers=headers,
                                 json=payload, timeout=self.timeout)
            resp.raise_for_status()
            raw  = resp.json()["choices"][0]["message"]["content"].strip()

            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
                raw = raw.strip()

            result = json.loads(raw)

            # Garantizar tipos correctos para los campos presentes
            if "approve" in result:
                result["approve"] = bool(result["approve"])
            if "confidence" in result:
                result["confidence"] = float(result["confidence"])
            if "recommended_leverage" in result:
                result["recommended_leverage"] = max(1, int(result["recommended_leverage"]))

            # Rellenar campos faltantes de entry (not ATM)
            entry_required = {
                "smc_analysis", "news_impact", "approve",
                "confidence", "recommended_leverage", "reasoning"
            }
            atm_required = {"action", "confidence", "reasoning"}

            missing_entry = entry_required - set(result.keys())
            missing_atm   = atm_required   - set(result.keys())

            if missing_entry and "action" not in result:
                defaults = {
                    "smc_analysis":         "N/A",
                    "news_impact":          "NEUTRAL",
                    "approve":              True,
                    "confidence":           0.5,
                    "recommended_leverage": 5,
                    "reasoning":            "respuesta parcial de IA",
                }
                for k in missing_entry:
                    result[k] = defaults[k]
            elif missing_atm and "action" in result:
                if "confidence" not in result: result["confidence"] = 0.5
                if "reasoning"  not in result: result["reasoning"]  = "N/A"
                if "new_sl"     not in result: result["new_sl"]     = None

            return result

        except requests.Timeout:
            log.warning(f"DeepSeek timeout ({self.timeout}s)")
        except requests.HTTPError as e:
            msg = f"DeepSeek HTTP {e.response.status_code}: {e.response.text[:150]}"
            log.error(msg)
            try:
                from tg_controller import notify_dev
                notify_dev(msg)
            except Exception:
                pass
        except json.JSONDecodeError as e:
            log.warning(f"DeepSeek JSON inválido: {e}")
        except Exception as e:
            log.error(f"DeepSeek error: {e}")
        return None

    # ── Outcomes ──────────────────────────────────────────────────────────────

    def record_outcome(self, trade_id: str, symbol: str, side: str,
                       entry_price: float, close_price: float,
                       pnl_usdt: float, pnl_pct: float, result: str,
                       close_reason: str, duration_s: int, leverage: int,
                       ts_open: int):
        try:
            ai_memory.save_outcome(
                trade_id=trade_id, symbol=symbol, side=side,
                entry_price=entry_price, close_price=close_price,
                pnl_usdt=pnl_usdt, pnl_pct=pnl_pct, result=result,
                close_reason=close_reason, duration_s=duration_s,
                leverage=leverage, ts_open=ts_open,
                ts_close=int(time.time())
            )
        except Exception as e:
            log.error(f"record_outcome {symbol}: {e}")

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        s = dict(self.stats)
        s["avg_ms"]        = int(s["total_ms"] / s["calls"]) if s["calls"] else 0
        s["approval_rate"] = round(s["approved"] / s["calls"] * 100, 1) if s["calls"] else 0
        s["enabled"]       = self.enabled
        s["lev_cap"]       = self.lev_cap
        s["atm"]           = dict(self.atm_stats)
        if self.atm_stats["calls"] > 0:
            s["atm"]["avg_ms"] = int(
                self.atm_stats["total_ms"] / self.atm_stats["calls"]
            )
        return s

    def get_accuracy_report(self, symbol: str = None) -> Dict:
        return ai_memory.get_ai_accuracy(symbol)

    # ── Cache (solo para entry, no para ATM) ──────────────────────────────────

    def _get_cache(self, symbol: str, signal: str) -> Optional[Dict]:
        key = f"{symbol}:{signal}"
        with _cache_lock:
            entry = _cache.get(key)
            if entry and (time.time() - entry["ts"]) < CACHE_TTL:
                return entry["result"]
            if entry:
                del _cache[key]
        return None

    def _set_cache(self, symbol: str, signal: str, result: Dict):
        with _cache_lock:
            _cache[f"{symbol}:{signal}"] = {
                "result": result, "ts": time.time()
            }


ai_filter = AIFilter()
