"""
ai_filter.py v3 — IA como Lead Institutional Quant & Risk Manager
══════════════════════════════════════════════════════════════════
La IA ya no solo aprueba/rechaza trades.
Ahora actúa como Risk Manager institucional que:
  1. Analiza datos crudos SMC (OB, FVG, Sweep, VWAP retest)
  2. Evalúa impacto de noticias y macro
  3. Decide si entrar al trade
  4. Asigna DINÁMICAMENTE el apalancamiento según calidad del setup y volatilidad
  5. Aprende de su historial de decisiones almacenado en DB

JSON de respuesta exigido:
  {
    "smc_analysis": "descripción del setup SMC detectado",
    "news_impact":  "HIGH_POSITIVE|MODERATE_POSITIVE|NEUTRAL|MODERATE_NEGATIVE|HIGH_NEGATIVE",
    "approve":      true|false,
    "confidence":   0.0-1.0,
    "recommended_leverage": int,   ← NUEVO: apalancamiento sugerido 1-100
    "reasoning":    "max 200 chars"
  }

.env:
    DEEPSEEK_API_KEY=sk-xxx
    AI_FILTER_ENABLED=true
    AI_FILTER_MIN_CONFIDENCE=0.55
    AI_FILTER_TIMEOUT=15
    AI_MAX_LEVERAGE_CAP=50        ← apalancamiento máximo global que la IA puede sugerir
"""

import json, logging, os, threading, time
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
import ai_memory

load_dotenv()
log = logging.getLogger("ai_filter")

DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "").strip()
AI_FILTER_ENABLED = os.getenv("AI_FILTER_ENABLED", "true").lower() in ("1","true","yes")
AI_MIN_CONFIDENCE = float(os.getenv("AI_FILTER_MIN_CONFIDENCE", "0.55"))
AI_TIMEOUT        = int(os.getenv("AI_FILTER_TIMEOUT", "15"))
AI_MAX_LEV_CAP    = int(os.getenv("AI_MAX_LEVERAGE_CAP", "50"))  # soft cap en la IA
DEEPSEEK_URL      = "https://api.deepseek.com/chat/completions"
MODEL             = "deepseek-chat"

_cache: Dict[str, Dict] = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 30   # segundos

# Resultado de rechazo por defecto (fail-open)
_FAIL_OPEN_RESULT = {
    "smc_analysis":        "Sin análisis — AI no disponible",
    "news_impact":         "NEUTRAL",
    "approve":             True,
    "confidence":          0.5,
    "recommended_leverage": 5,
    "reasoning":           "AI no disponible — fail-open con leverage conservador",
}

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


class AIFilter:
    def __init__(self):
        self.enabled  = AI_FILTER_ENABLED and bool(DEEPSEEK_API_KEY)
        self.min_conf = AI_MIN_CONFIDENCE
        self.timeout  = AI_TIMEOUT
        self.lev_cap  = AI_MAX_LEV_CAP
        self.stats    = {
            "calls": 0, "approved": 0, "rejected": 0, "errors": 0,
            "total_ms": 0, "cache_hits": 0,
        }
        if AI_FILTER_ENABLED and not DEEPSEEK_API_KEY:
            log.warning("AI_FILTER_ENABLED=true pero DEEPSEEK_API_KEY vacío — desactivado")
            self.enabled = False
        ai_memory.init_db()
        log.info(f"AIFilter {'✅ Activo model='+MODEL if self.enabled else '⚫ Desactivado'}")

    # ── API pública ───────────────────────────────────────────────────────────

    def should_trade(
        self,
        analysis: Dict,
        symbol_stats: Dict = None,
        news_bias: Dict = None,
        recent_news: List[Dict] = None,
        trade_id: str = None,
    ) -> Dict:
        """
        Decide si operar y con qué apalancamiento.

        Retorna dict con TODOS los campos de la respuesta IA:
        {
            "smc_analysis":         str,
            "news_impact":          str,
            "approve":              bool,
            "confidence":           float,
            "recommended_leverage": int,
            "reasoning":            str,
            "_source":              "ai" | "cache" | "fail_open" | "disabled",
        }
        """
        if not self.enabled:
            return {**_FAIL_OPEN_RESULT, "_source": "disabled",
                    "recommended_leverage": 5}

        sym    = analysis.get("symbol", "")
        signal = analysis.get("signal", "")

        # ── Cache ─────────────────────────────────────────────
        cached = self._get_cache(sym, signal)
        if cached:
            self.stats["cache_hits"] += 1
            return {**cached, "_source": "cache"}

        # ── Historial desde DB ────────────────────────────────
        symbol_history = ai_memory.get_symbol_history(sym, limit=12)

        # ── Construir prompt ──────────────────────────────────
        user_msg = self._build_prompt(
            analysis, symbol_stats or {}, news_bias or {},
            recent_news or [], symbol_history
        )

        t0 = time.time()
        result = self._call_deepseek(user_msg)
        elapsed_ms = int((time.time() - t0) * 1000)
        self.stats["calls"]    += 1
        self.stats["total_ms"] += elapsed_ms

        if result is None:
            self.stats["errors"] += 1
            log.warning(f"AIFilter [{sym}] API error → fail-open con lev=5 ({elapsed_ms}ms)")
            return {**_FAIL_OPEN_RESULT, "_source": "fail_open"}

        # ── Validar y normalizar respuesta ────────────────────
        approve    = bool(result.get("approve", True))
        confidence = float(result.get("confidence", 0.5))
        smc_anal   = str(result.get("smc_analysis", "N/A"))
        news_imp   = str(result.get("news_impact", "NEUTRAL"))
        reasoning  = str(result.get("reasoning", "sin razon"))
        rec_lev    = int(result.get("recommended_leverage", 5))

        # Hard cap del leverage en el valor máximo configurado
        rec_lev = max(1, min(rec_lev, self.lev_cap))
        result["recommended_leverage"] = rec_lev

        # Validar news_impact
        valid_impacts = {"HIGH_POSITIVE","MODERATE_POSITIVE","NEUTRAL","MODERATE_NEGATIVE","HIGH_NEGATIVE"}
        if news_imp not in valid_impacts:
            news_imp = "NEUTRAL"
            result["news_impact"] = "NEUTRAL"

        # ── IA muy indecisa → fail-open con leverage bajo ────
        if confidence < 0.45:
            log.info(f"AIFilter [{sym}] IA indecisa ({confidence:.0%}) → fail-open lev={min(5, rec_lev)}")
            result["approve"]             = True
            result["recommended_leverage"]= min(5, rec_lev)
            result["_source"]             = "fail_open_indecise"
            if trade_id:
                ai_memory.save_decision(trade_id, analysis, result,
                                        news_bias or {}, symbol_stats or {}, recent_news or [])
            return result

        # ── Guardar decisión en DB ────────────────────────────
        if trade_id:
            ai_memory.save_decision(trade_id, analysis, result,
                                    news_bias or {}, symbol_stats or {}, recent_news or [])

        # ── Cache ─────────────────────────────────────────────
        self._set_cache(sym, signal, result)

        if not approve and confidence >= self.min_conf:
            self.stats["rejected"] += 1
            log.info(f"🚫 [{sym}] RECHAZADO ({confidence:.0%}) {reasoning} [{elapsed_ms}ms]")
        else:
            self.stats["approved"] += 1
            log.info(f"✅ [{sym}] APROBADO ({confidence:.0%}) lev={rec_lev}x {reasoning} [{elapsed_ms}ms]")

        result["_source"] = "ai"
        return result

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
                leverage=leverage, ts_open=ts_open, ts_close=int(time.time())
            )
        except Exception as e:
            log.error(f"record_outcome {symbol}: {e}")

    def get_stats(self) -> Dict:
        s = dict(self.stats)
        s["avg_ms"]        = int(s["total_ms"] / s["calls"]) if s["calls"] else 0
        s["approval_rate"] = round(s["approved"] / s["calls"] * 100, 1) if s["calls"] else 0
        s["enabled"]       = self.enabled
        s["lev_cap"]       = self.lev_cap
        return s

    def get_accuracy_report(self, symbol: str = None) -> Dict:
        return ai_memory.get_ai_accuracy(symbol)

    # ── Construcción del prompt ────────────────────────────────────────────────

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

        # Volatilidad relativa (ATR / precio)
        atr_pct = (atr / mark * 100) if (mark and atr) else 0
        vol_label = "EXTREMA" if atr_pct > 5 else ("ALTA" if atr_pct > 2 else ("NORMAL" if atr_pct > 0.5 else "BAJA"))

        rr_str = "N/A"
        if tp and sl and mark and abs(mark - sl) > 0:
            rr_str = f"1:{abs((tp - mark) / (mark - sl)):.1f}"

        # SMC data cruda
        smc_sum   = analysis.get("smc_summary", {})
        smc_sweep = analysis.get("smc_sweep", False)
        smc_ob    = analysis.get("smc_ob_hit", False)
        smc_fvg   = analysis.get("smc_fvg_fill", False)
        smc_vwap  = analysis.get("smc_vwap_retest", False)
        setups_n  = smc_sum.get("setups_count", 0)

        # Setup label para la IA
        setup_quality = "PREMIUM ⭐⭐⭐" if (smc_sweep and smc_ob and setups_n >= 3) \
            else ("BUENO ⭐⭐" if setups_n >= 2 \
            else ("BÁSICO ⭐" if setups_n >= 1 else "SIN SETUP SMC ❌"))

        # TF evidence
        tf_evidence_str = "Sin evidencia SMC por TF."
        tf_ev = smc_sum.get("tf_evidence", {})
        if tf_ev:
            lines = []
            for label, ev in tf_ev.items():
                parts = [f"{k}: {v}" for k, v in ev.items()]
                lines.append(f"  {label}: {' | '.join(parts)}")
            tf_evidence_str = "\n".join(lines)

        # TF scores
        tf_details = analysis.get("tf_details", {})
        tf_lines   = []
        for tf in ["1m","5m","15m","30m","1h","4h","1D"]:
            d = tf_details.get(tf)
            if d:
                s_val = d.get("score", 0)
                tf_lines.append(
                    f"  {tf:>4}: score={s_val:+.1f}  RSI={d.get('rsi','-')}"
                    f"  pd_zone={d.get('pd_zone','?')}"
                )

        # Historial del símbolo
        hist_str = "Sin historial."
        if sym_stats.get("trades", 0) >= 3:
            t = sym_stats["trades"]; w = sym_stats.get("wins", 0)
            pnl = sym_stats.get("total_pnl", 0)
            hist_str = (f"{t} trades | WR={w/t*100:.0f}% | PnL={pnl:+.2f} USDT | "
                        f"Best={sym_stats.get('best',0):+.2f} | Worst={sym_stats.get('worst',0):+.2f}")

        # Noticias completas
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

        # Historial IA desde DB
        ai_hist = "Sin historial de IA para este par."
        if symbol_history:
            lines = []
            for h in symbol_history[:8]:
                pnl_h = h.get("pnl_usdt")
                pnl_str = f"{pnl_h:+.2f}" if pnl_h is not None else "abierto"
                lev_h   = h.get("leverage", "?")
                lines.append(
                    f"  {h.get('side','?'):5s} {'✅IA' if h.get('ai_approved') else '🚫IA'}"
                    f" lev={lev_h}x"
                    f" → {h.get('result','?'):9s} PnL={pnl_str} "
                    f"| {(h.get('ai_reasoning') or '')[:60]}"
                )
            ai_hist = "\n".join(lines)

        fg_val  = news_bias.get("fear_greed", 50)
        n_dir   = news_bias.get("news_direction") or news_bias.get("direction", "NEUTRAL")
        n_score = news_bias.get("news_score", 0)
        block   = news_bias.get("should_block", False)
        alerts  = news_bias.get("recent_alerts", 0)
        fg_lbl  = news_bias.get("fg_label", "Neutral")

        # Calcular apalancamiento sugerido base para orientar a la IA
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
Alta volatilidad (ATR>{atr:.4f} = {atr_pct:.1f}%): reducir leverage
F&G extremo (<20 LONG o >85 SHORT): máximo 5x-10x
Sugerencia base del sistema: ~{base_lev_hint}x

Analiza TODO y decide. Responde SOLO con el JSON exacto."""

    def _calc_base_leverage_hint(self, setups_n: int, score: float,
                                  atr_pct: float, fg: int,
                                  news_bias: Dict) -> int:
        """
        Calcula un leverage base sugerido para orientar a la IA.
        La IA puede desviarse, pero esto es una referencia contextual.
        """
        lev = 10  # base

        # Calidad del setup
        if setups_n >= 3:  lev = 30
        elif setups_n == 2: lev = 20
        elif setups_n == 1: lev = 12
        else:               lev = 5

        # Score alto = más confianza
        if abs(score) > 6:   lev = int(lev * 1.5)
        elif abs(score) > 4: lev = int(lev * 1.2)
        elif abs(score) < 3: lev = int(lev * 0.6)

        # Volatilidad: reducir fuerte si ATR alto
        if atr_pct > 5:   lev = int(lev * 0.4)
        elif atr_pct > 2: lev = int(lev * 0.65)
        elif atr_pct > 1: lev = int(lev * 0.85)

        # Fear & Greed extremo
        if fg < 15 or fg > 90: lev = min(lev, 8)
        elif fg < 25 or fg > 80: lev = min(lev, 15)

        # Noticias muy negativas
        if news_bias.get("should_block"):     lev = min(lev, 3)
        elif news_bias.get("recent_alerts", 0) > 2: lev = min(lev, 8)

        return max(1, min(lev, self.lev_cap))

    # ── DeepSeek ──────────────────────────────────────────────────────────────

    def _call_deepseek(self, user_message: str) -> Optional[Dict]:
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       MODEL,
            "max_tokens":  400,
            "temperature": 0.10,   # muy bajo = determinista y consistente
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
        }
        try:
            resp = requests.post(DEEPSEEK_URL, headers=headers,
                                 json=payload, timeout=self.timeout)
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()

            # Limpiar posibles markdown fences
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
                raw = raw.strip()

            result = json.loads(raw)

            # Validar campos requeridos
            required = {"approve", "confidence", "recommended_leverage",
                        "smc_analysis", "news_impact", "reasoning"}
            missing = required - set(result.keys())
            if missing:
                log.warning(f"DeepSeek respuesta incompleta, faltan: {missing}")
                # Rellenar defaults para campos faltantes
                defaults = {
                    "smc_analysis":         "N/A",
                    "news_impact":          "NEUTRAL",
                    "approve":              True,
                    "confidence":           0.5,
                    "recommended_leverage": 5,
                    "reasoning":            "respuesta parcial de IA",
                }
                for k in missing:
                    result[k] = defaults[k]

            # Forzar tipos correctos
            result["approve"]             = bool(result["approve"])
            result["confidence"]          = float(result["confidence"])
            result["recommended_leverage"]= max(1, int(result["recommended_leverage"]))

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

    # ── Cache ─────────────────────────────────────────────────────────────────

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
            _cache[f"{symbol}:{signal}"] = {"result": result, "ts": time.time()}


ai_filter = AIFilter()
