"""
ai_filter.py v2 — Filtro de IA con memoria y noticias completas
- Consulta DeepSeek con contexto de noticias detallado
- Guarda cada decisión en ai_memory.db
- Consulta el historial de la DB para mejorar análisis futuros
- Registra outcome real cuando el trade se cierra

.env:
    DEEPSEEK_API_KEY=sk-xxx
    AI_FILTER_ENABLED=true
    AI_FILTER_MIN_CONFIDENCE=0.60
    AI_FILTER_TIMEOUT=10
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
AI_MIN_CONFIDENCE = float(os.getenv("AI_FILTER_MIN_CONFIDENCE", "0.60"))
AI_TIMEOUT        = int(os.getenv("AI_FILTER_TIMEOUT", "10"))
DEEPSEEK_URL      = "https://api.deepseek.com/chat/completions"
MODEL             = "deepseek-chat"

_cache: Dict[str, Dict] = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 30

SYSTEM_PROMPT = """Eres un trader cuantitativo senior especializado en futuros perpetuos de criptomonedas.
Recibes señales de un bot algorítmico con análisis técnico multi-timeframe, contexto de noticias
y tu historial de decisiones anteriores en ese par. Tu tarea: decidir si aprobar o rechazar el trade.

REGLAS ESTRICTAS:
1. Responde SOLO con un objeto JSON válido, sin markdown, sin texto adicional.
2. Estructura exacta:
   {"approve": true/false, "confidence": 0.0-1.0, "reasoning": "max 150 chars", "warnings": ["string"], "key_factors": ["factor1"]}
3. "confidence": tu certeza en la decision (< 0.5 = muy inseguro).
4. "warnings": 0-3 alertas concretas. "key_factors": 1-3 factores principales.

CRITERIOS PARA APROBAR:
- Score > umbral del modo (AGGRESSIVE: 2.5, MOMENTUM: 3.0, STANDARD: 3.5)
- Macro y entry bias alineados con la direccion
- Noticias no contradicen la senal
- Historial del par con WR > 35% (o sin historial = neutral)
- Fear & Greed no extremo en contra (F&G=10 + LONG = muy arriesgado)

CRITERIOS PARA RECHAZAR:
- Macro y entry en direcciones opuestas
- Noticia critica reciente en contra del trade
- Historial del par con WR < 25% en 8+ trades
- F&G extremo (< 20 para LONG, > 85 para SHORT)
- Squeeze sin confirmacion clara de direccion
- Tus rechazos anteriores en este par acertaron

APRENDE DEL HISTORIAL: Analiza tus decisiones pasadas. Si rechazaste y el mercado te dio la razon,
refuerza ese criterio. Si aprobaste y fue perdida, se mas estricto en condiciones similares."""


class AIFilter:
    def __init__(self):
        self.enabled  = AI_FILTER_ENABLED and bool(DEEPSEEK_API_KEY)
        self.min_conf = AI_MIN_CONFIDENCE
        self.timeout  = AI_TIMEOUT
        self.stats    = {"calls":0,"approved":0,"rejected":0,"errors":0,"total_ms":0,"cache_hits":0}
        if AI_FILTER_ENABLED and not DEEPSEEK_API_KEY:
            log.warning("AI_FILTER_ENABLED=true pero DEEPSEEK_API_KEY vacio — desactivado")
            self.enabled = False
        ai_memory.init_db()
        log.info(f"AIFilter {'✅ Activo model='+MODEL if self.enabled else '⚫ Desactivado'}")

    # ── API publica ───────────────────────────────────────────────────────────

    def should_trade(self, analysis: Dict, symbol_stats: Dict = None,
                     news_bias: Dict = None, recent_news: List[Dict] = None,
                     trade_id: str = None) -> Tuple[bool, str]:
        if not self.enabled:
            return True, "AI filter desactivado"
        sym    = analysis.get("symbol", "")
        signal = analysis.get("signal", "")
        cached = self._get_cache(sym, signal)
        if cached:
            self.stats["cache_hits"] += 1
            return bool(cached["approve"]), f"[cache] {cached.get('reasoning','')}"
        symbol_history = ai_memory.get_symbol_history(sym, limit=12)
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
            log.warning(f"AIFilter [{sym}] API error → fail-open ({elapsed_ms}ms)")
            return True, "DeepSeek no disponible — aprobado"
        approve   = bool(result.get("approve", True))
        ai_conf   = float(result.get("confidence", 0.5))
        reasoning = result.get("reasoning", "sin razon")
        warnings  = result.get("warnings", [])
        key_factors = result.get("key_factors", [])
        if ai_conf < 0.45:
            log.info(f"AIFilter [{sym}] IA indecisa ({ai_conf:.0%}) → fail-open")
            if trade_id:
                ai_memory.save_decision(trade_id, analysis, result,
                                        news_bias or {}, symbol_stats or {}, recent_news or [])
            return True, f"IA indecisa ({ai_conf:.0%}) — aprobado"
        if trade_id:
            ai_memory.save_decision(trade_id, analysis, result,
                                    news_bias or {}, symbol_stats or {}, recent_news or [])
        self._set_cache(sym, signal, result)
        warn_str    = " | ".join(warnings)    if warnings    else ""
        factors_str = " • ".join(key_factors) if key_factors else ""
        if not approve and ai_conf >= self.min_conf:
            self.stats["rejected"] += 1
            reason = reasoning
            if warn_str:    reason += f" ⚠️ {warn_str}"
            if factors_str: reason += f" [{factors_str}]"
            log.info(f"🚫 [{sym}] RECHAZADO ({ai_conf:.0%}) {reasoning} [{elapsed_ms}ms]")
            return False, reason
        self.stats["approved"] += 1
        reason = reasoning + (f" (⚠️ {warn_str})" if warn_str else "")
        log.info(f"✅ [{sym}] APROBADO ({ai_conf:.0%}) {reasoning} [{elapsed_ms}ms]")
        return True, reason

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
        return s

    def get_accuracy_report(self, symbol: str = None) -> Dict:
        return ai_memory.get_ai_accuracy(symbol)

    # ── Construccion del prompt ───────────────────────────────────────────────

    def _build_prompt(self, analysis: Dict, sym_stats: Dict, news_bias: Dict,
                      recent_news: List[Dict], symbol_history: List[Dict]) -> str:
        sym  = analysis.get("symbol","")
        sig  = analysis.get("signal","")
        score = analysis.get("composite_score", 0)
        conf  = analysis.get("confidence", 0)
        mode  = analysis.get("entry_mode","STANDARD")
        mark  = analysis.get("mark_price", 0)
        atr   = analysis.get("atr") or 0
        tp    = analysis.get("tp")
        sl    = analysis.get("sl")
        rr_str = "N/A"
        if tp and sl and mark and abs(mark - sl) > 0:
            rr_str = f"1:{abs((tp - mark) / (mark - sl)):.1f}"

        # TF scores
        tf_details = analysis.get("tf_details", {})
        tf_lines = []
        for tf in ["1m","5m","15m","30m","1h","4h","1D"]:
            d = tf_details.get(tf)
            if d:
                s_val = d.get("score", 0)
                sq = "⚡" if d.get("squeeze") else ""
                tf_lines.append(f"  {tf:>4}: {s_val:+.1f} {'▲' if s_val>0 else '▼'}{sq}  RSI={d.get('rsi','-')}")

        # Historial del simbolo
        hist_str = "Sin historial."
        if sym_stats.get("trades", 0) >= 3:
            t = sym_stats["trades"]; w = sym_stats.get("wins",0)
            pnl = sym_stats.get("total_pnl",0)
            hist_str = (f"{t} trades | WR={w/t*100:.0f}% | PnL total={pnl:+.2f} USDT | "
                        f"Mejor={sym_stats.get('best',0):+.2f} | Peor={sym_stats.get('worst',0):+.2f}")

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
                lines.append(
                    f"  {h.get('side','?'):5s} {'✅IA' if h.get('ai_approved') else '🚫IA'}"
                    f" → {h.get('result','?'):9s} PnL={pnl_str} "
                    f"F&G={h.get('fear_greed','?')} | {(h.get('ai_reasoning') or '')[:60]}"
                )
            ai_hist = "\n".join(lines)

        fg_val  = news_bias.get("fear_greed", 50)
        n_dir   = news_bias.get("news_direction") or news_bias.get("direction","NEUTRAL")
        n_score = news_bias.get("news_score", 0)
        block   = news_bias.get("should_block", False)
        alerts  = news_bias.get("recent_alerts", 0)

        return f"""=== SENAL A REVISAR ===
Par: {sym} | Direccion: {sig} | Modo: {mode}
Score: {score:+.2f} | Confianza tecnica: {conf:.0%} | R:R: {rr_str}
Precio: {mark} | ATR: {atr:.4f} | TP: {tp} | SL: {sl}
Alineacion: {"COMPLETA ✅" if analysis.get("aligned") else "PARCIAL ⚠️"}
Macro: {analysis.get("macro_bias","?")} | Mid: {analysis.get("mid_bias","?")} | Entry: {analysis.get("entry_bias","?")}
Squeeze: {"SI ⚡" if analysis.get("squeeze") else "no"} | Vol spike: {"SI 💥" if analysis.get("vol_spike") else "no"}

=== SCORES TIMEFRAME ===
{chr(10).join(tf_lines) if tf_lines else "  No disponible"}

=== NOTICIAS Y SENTIMIENTO ===
Sentimiento {sym}: {n_dir} (score={n_score:+.2f}) | Alertas criticas 2h: {alerts}
Fear & Greed: {fg_val}/100 — {news_bias.get("fg_label","Neutral")}
{"⛔ BLOQUEO ACTIVO: noticia muy negativa reciente" if block else ""}

Noticias recientes:
{news_section}

=== HISTORIAL DEL PAR ===
{hist_str}

=== TUS DECISIONES ANTERIORES EN {sym} ===
{ai_hist}

Analiza TODO y decide. Responde SOLO con el JSON."""

    # ── DeepSeek ──────────────────────────────────────────────────────────────

    def _call_deepseek(self, user_message: str) -> Optional[Dict]:
        headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": MODEL, "max_tokens": 300, "temperature": 0.15,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
        }
        try:
            resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
                raw = raw.strip()
            result = json.loads(raw)
            if "approve" not in result:
                log.warning(f"DeepSeek sin 'approve': {raw[:120]}")
                return None
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
            log.warning(f"DeepSeek JSON invalido: {e}")
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
            if entry: del _cache[key]
        return None

    def _set_cache(self, symbol: str, signal: str, result: Dict):
        with _cache_lock:
            _cache[f"{symbol}:{signal}"] = {"result": result, "ts": time.time()}


ai_filter = AIFilter()
