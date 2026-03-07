"""
bot_autonomous.py  v3
════════════════════════════════════════════════════════════════
Bot autónomo con:
  - AI como Lead Risk Manager (leverage dinámico desde DeepSeek)
  - Hard-limits matemáticos: leverage clamped a maxLeverage de Bybit
  - Qty con math.floor + cap a maxOrderQty (via client.safe_qty)
  - Notificaciones Telegram en cada evento relevante
"""

import json, logging, os, time, threading, uuid, asyncio, math
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
import notify_prefs

# ── Config ─────────────────────────────────────────────────────────────────────
BYBIT_API_KEY      = os.getenv("BYBIT_API_KEY",   "").strip()
BYBIT_API_SECRET   = os.getenv("BYBIT_API_SECRET","").strip()
PAPER_TRADING      = os.getenv("PAPER_TRADING", "true").lower() in ("1","true","yes")
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC",   "30"))
MONITOR_INTERVAL   = int(os.getenv("MONITOR_INTERVAL_SEC","10"))
AUTO_SCAN_ENABLED  = os.getenv("AUTO_SCAN", "true").lower() in ("1","true","yes")
MIN_VOLUME_USDT    = float(os.getenv("MIN_VOLUME_USDT", "5000000"))

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "").strip()

FIXED_WATCHLIST: List[str] = [
    s.strip() for s in
    os.getenv("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
    if s.strip()
]

PROBLEMATIC_SYMBOLS = {"BARDUSDT", "POWERUSDT"}

# ── Límites de seguridad globales del bot ──────────────────────────────────────
# El leverage final = min(ai_suggested, bybit_max, BOT_MAX_LEVERAGE)
BOT_MAX_LEVERAGE    = int(os.getenv("BOT_MAX_LEVERAGE",    "50"))   # hard ceiling del bot
BOT_MAX_RISK_PCT    = float(os.getenv("BOT_MAX_RISK_PCT",  "2.0"))  # % máximo del balance por trade
BOT_MIN_LEVERAGE    = int(os.getenv("BOT_MIN_LEVERAGE",    "1"))    # leverage mínimo


# ══════════════════════════════════════════════════════════
#  NOTIFICADOR TELEGRAM
# ══════════════════════════════════════════════════════════

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self._queue: List[str] = []
        self._lock   = threading.Lock()
        self._active = bool(token and chat_id)
        if self._active:
            threading.Thread(target=self._worker, daemon=True, name="tg-notify").start()
            log.info("TelegramNotifier activo")
        else:
            log.info("TelegramNotifier desactivado (sin TOKEN o CHAT_ID)")

    def send(self, text: str):
        if not self._active:
            return
        with self._lock:
            self._queue.append(text)

    def send_direct(self, text: str) -> bool:
        import requests as req
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r = req.post(url, json={
                "chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML",
            }, timeout=10)
            data = r.json()
            if data.get("ok"):
                log.info(f"✅ Telegram OK → chat_id={self.chat_id}")
                return True
            log.error(f"❌ Telegram error: {data.get('description')} (chat_id={self.chat_id})")
            return False
        except Exception as e:
            log.error(f"❌ Telegram excepción: {e}")
            return False

    def _worker(self):
        import requests as req
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            with self._lock:
                msgs = self._queue[:]
                self._queue.clear()
            for text in msgs:
                try:
                    r = req.post(url, json={
                        "chat_id":    self.chat_id,
                        "text":       text[:4000],
                        "parse_mode": "HTML",
                    }, timeout=10)
                    data = r.json()
                    if not data.get("ok"):
                        log.error(f"❌ TG error: {data.get('description')}")
                    time.sleep(0.35)
                except Exception as e:
                    log.warning(f"TG send error: {e}")
            time.sleep(0.5)


# ══════════════════════════════════════════════════════════
#  BOT AUTÓNOMO
# ══════════════════════════════════════════════════════════

class AutonomousBot:
    def __init__(self):
        if not BYBIT_API_KEY or not BYBIT_API_SECRET:
            raise ValueError("BYBIT_API_KEY y BYBIT_API_SECRET requeridos en .env")

        self.client   = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET, paper=PAPER_TRADING)
        self.learner  = LearningEngine()
        self.risk_mgr = RiskManager(self.learner)
        self.tg       = TelegramNotifier(TG_TOKEN, TG_CHAT_ID)
        self.news     = NewsEngine(telegram_notifier=self.tg, scan_interval=120)

        self.open_positions: Dict[str, Dict] = {}
        self.cooldowns:      Dict[str, float] = {}
        self.running = False
        self._lock   = threading.Lock()

        self._sync_positions()

        mode = "🟡 PAPER" if PAPER_TRADING else "🔴 REAL"
        msg = (
            f"🤖 <b>Bot iniciado — {mode}</b>\n"
            f"Watchlist: {', '.join(FIXED_WATCHLIST)}\n"
            f"Scan: cada {SCAN_INTERVAL_SEC}s | Monitor: cada {MONITOR_INTERVAL}s\n"
            f"Hard limits: maxLev={BOT_MAX_LEVERAGE}x | riskPct={BOT_MAX_RISK_PCT}%\n"
            f"TFs: {len(ALL_TF)} ({', '.join(ALL_TF)})"
        )
        if self.tg._active:
            ok = self.tg.send_direct(msg)
            if not ok:
                log.error("❌ Telegram no pudo enviar — verifica TELEGRAM_CHAT_ID")
        log.info(f"Bot iniciado — {mode} — {FIXED_WATCHLIST}")

    # ── Sincronización de posiciones ───────────────────────────────────────────

    def _sync_positions(self):
        try:
            new_pos = {}
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
                new_pos[sym] = {
                    "trade_id":    f"sync_{sym}",
                    "symbol":      sym,
                    "side":        side,
                    "entry_price": ep,
                    "qty":         float(p.get("size", 0)),
                    "leverage":    int(float(p.get("leverage", 10))),
                    "tp":          float(p.get("takeProfit", 0)) or None,
                    "sl":          float(p.get("stopLoss",   0)) or None,
                    "open_ts":     int(time.time()),
                    "peak_price":  ep,
                    "atr":         None,
                    "ai_decision": None,
                }

            old_keys = set(self.open_positions.keys())
            new_keys = set(new_pos.keys())

            for sym in new_keys - old_keys:
                pos = new_pos[sym]
                log.info(f"📡 Nueva posición: {sym} {pos['side']} @ {pos['entry_price']}")
                fake_analysis = {
                    "composite_score": 0.0, "confidence": 0.5,
                    "signal": pos["side"], "entry_mode": "SYNCED",
                    "atr": 0, "squeeze": False, "vol_spike": False,
                    "tf_details": {}, "smc_summary": {},
                    "smc_sweep": False, "smc_ob_hit": False,
                    "smc_fvg_fill": False, "smc_vwap_retest": False,
                }
                self.learner.record_open(
                    pos["trade_id"], sym, pos["side"],
                    pos["entry_price"], pos["qty"], pos["leverage"],
                    pos["tp"] or 0, pos["sl"] or 0, fake_analysis
                )
                self.risk_mgr.on_open(sym)

            for sym in old_keys - new_keys:
                pos = self.open_positions[sym]
                log.warning(f"👻 {sym}: cerrado externamente")
                pnl = 0.0
                try:
                    closed = self.client.get_closed_pnl(sym, limit=3)
                    if closed:
                        pnl = float(closed[0].get("closedPnl", 0))
                except Exception:
                    pass
                reason = "TP" if pnl >= 0 else "SL"
                self.learner.record_close(pos.get("trade_id", f"sync_{sym}"),
                                          self.client.get_mark_price(sym), pnl, reason)
                self.risk_mgr.on_close(sym, pnl)
                result_e = "✅" if pnl >= 0 else "❌"
                self.tg.send(
                    f"{result_e} <b>{sym}</b> cerrado por {reason}\n"
                    f"PnL: <code>{pnl:+.2f} USDT</code>"
                )
                with self._lock:
                    self.open_positions.pop(sym, None)

            with self._lock:
                self.open_positions.update(new_pos)

            if old_keys != new_keys:
                log.info(f"Posiciones sync: {sorted(self.open_positions.keys()) or 'ninguna'}")

        except Exception as e:
            log.error(f"_sync_positions: {e}")

    # ── Utilidades ─────────────────────────────────────────────────────────────

    def _in_cooldown(self, sym: str) -> bool:
        return time.time() < self.cooldowns.get(sym, 0)

    def _set_cooldown(self, sym: str):
        self.cooldowns[sym] = time.time() + self.learner.params.get("cooldown_seconds", 60)

    def _get_balance(self) -> float:
        try:
            return self.client.get_usdt_balance()
        except Exception as e:
            log.error(f"Error obteniendo balance: {e}")
            return 0.0

    # ── CALCULAR LEVERAGE FINAL CON TRIPLE CAPA DE SEGURIDAD ──────────────────

    def _resolve_leverage(self, symbol: str, ai_suggested: int) -> Tuple[int, str]:
        """
        Resuelve el leverage final con 3 capas de protección:
          1. ai_suggested: lo que la IA recomienda
          2. bybit_max:    lo que Bybit permite para ese símbolo
          3. BOT_MAX_LEVERAGE: hard ceiling del bot (config)

        Retorna (leverage_final, log_str).
        """
        # Capa 1: leverage mínimo global del bot
        lev = max(BOT_MIN_LEVERAGE, ai_suggested)

        # Capa 2: maxLeverage real del símbolo en Bybit
        bybit_max = self.client.safe_leverage(symbol, 9999)  # pedir máximo → nos devuelve el cap real
        # Nota: safe_leverage(symbol, 9999) internamente llama get_instrument_info
        # y clampea a maxLeverage, por lo que bybit_max = maxLeverage del símbolo.
        info      = self.client.get_instrument_info(symbol)
        bybit_max = info["max_leverage"]

        # Capa 3: hard ceiling global del bot
        final = min(lev, bybit_max, BOT_MAX_LEVERAGE)
        final = max(BOT_MIN_LEVERAGE, final)

        parts = []
        if final != ai_suggested:
            if ai_suggested > bybit_max:
                parts.append(f"bybit_max={bybit_max}")
            if ai_suggested > BOT_MAX_LEVERAGE:
                parts.append(f"bot_cap={BOT_MAX_LEVERAGE}")
        log_str = (f"lev: AI={ai_suggested}x → final={final}x"
                   + (f" (limitado por {', '.join(parts)})" if parts else ""))
        return final, log_str

    # ── CALCULAR QTY CON HARD-LIMITS ──────────────────────────────────────────

    def _calc_qty(self, symbol: str, balance: float, mark: float,
                  atr_v: float, leverage: int) -> Tuple[float, str]:
        """
        Calcula la cantidad de contratos con:
          1. Tamaño basado en riesgo máximo (BOT_MAX_RISK_PCT del balance)
          2. math.floor al qtyStep del instrumento
          3. Cap a maxOrderQty
          4. Floor desde minOrderQty

        Retorna (qty_final, log_str) o (0.0, error_msg).
        """
        if balance <= 0 or mark <= 0:
            return 0.0, "balance o mark_price inválido"

        # Tamaño de posición = (balance * riesgo_pct * leverage) / precio
        risk_usdt = balance * (BOT_MAX_RISK_PCT / 100.0)
        raw_qty   = (risk_usdt * leverage) / mark

        # También calculamos qty desde el learning engine como referencia
        learner_qty = self.learner.calculate_position_size(balance, mark, atr_v, leverage)
        # Usar el menor de los dos (más conservador)
        qty_before_safe = min(raw_qty, learner_qty) if learner_qty > 0 else raw_qty

        # Aplicar hard-limits matemáticos (math.floor + min/max)
        safe, err = self.client.safe_qty(symbol, qty_before_safe)

        if safe <= 0:
            return 0.0, f"qty={qty_before_safe:.6f} → {err}"

        log_str = (f"qty: risk={risk_usdt:.2f} USDT  lev={leverage}x  "
                   f"raw={qty_before_safe:.6f} → safe={safe:.6f}")
        return safe, log_str

    # ── Abrir trade ────────────────────────────────────────────────────────────

    def try_open_trade(self, analysis: Dict) -> bool:
        sym = analysis["symbol"]

        if sym in PROBLEMATIC_SYMBOLS:
            return False
        if self._in_cooldown(sym):
            return False

        sig   = analysis["signal"]
        score = analysis["composite_score"]
        conf  = analysis["confidence"]
        mark  = analysis["mark_price"]
        atr_v = analysis.get("atr") or 0

        params     = self.learner.get_params()
        entry_mode = analysis.get("entry_mode", "STANDARD")
        threshold  = analysis.get("threshold", 4.0)

        # ── Filtros básicos ────────────────────────────────────────────────────
        if sig == "FLAT":
            return False
        if abs(score) < threshold:
            log.debug(f"Skip {sym}: score {score:+.2f} < {threshold:.1f} [{entry_mode}]")
            return False
        if conf < params.get("min_confidence", 0.40):
            return False
        with self._lock:
            if sym in self.open_positions:
                return False
            if len(self.open_positions) >= params.get("max_open_positions", 3):
                return False

        ok, reason = self.learner.should_trade_symbol(sym)
        if not ok:
            log.info(f"Skip {sym}: {reason}")
            return False
        if not self.risk_mgr.can_open(sym, score):
            return False

        # ── Filtro de noticias ─────────────────────────────────────────────────
        news_bias = self.news.get_news_bias(sym)
        if news_bias["should_block"]:
            log.info(f"🚫 {sym} bloqueado por noticias negativas")
            return False

        news_adj       = news_bias["news_score"] * 0.5 + news_bias["fg_adj"] * 0.3
        adjusted_score = score + news_adj
        if sig == "LONG"  and news_bias["direction"] == "BEARISH": adjusted_score -= 0.5
        if sig == "SHORT" and news_bias["direction"] == "BULLISH": adjusted_score -= 0.5
        if abs(adjusted_score) < threshold:
            log.info(f"Skip {sym}: score ajustado {score:.2f}→{adjusted_score:.2f}")
            return False

        # ── FILTRO DE IA — Risk Manager principal ─────────────────────────────
        _pending_id = str(uuid.uuid4())[:8]
        ai_decision = ai_filter.should_trade(
            analysis,
            symbol_stats=self.learner.get_symbol_stats(sym),
            news_bias=news_bias,
            recent_news=self.news.get_recent_news(5),
            trade_id=_pending_id,
        )

        approve    = bool(ai_decision.get("approve", True))
        ai_conf    = float(ai_decision.get("confidence", 0.5))
        ai_lev     = int(ai_decision.get("recommended_leverage", 10))
        reasoning  = ai_decision.get("reasoning", "")
        smc_anal   = ai_decision.get("smc_analysis", "")
        news_imp   = ai_decision.get("news_impact", "NEUTRAL")

        if not approve and ai_conf >= 0.55:
            log.info(f"🤖 IA rechazó {sym}: {reasoning}")
            notify("ai_decisions",
                   f"🤖 <b>IA rechazó trade</b>\n"
                   f"Par: {sym}  {sig}\n"
                   f"Score: {score:+.2f}  Conf: {conf:.0%}\n"
                   f"SMC: {smc_anal}\n"
                   f"Noticias: {news_imp}\n"
                   f"Razón: {reasoning}")
            return False

        # ── RESOLVER LEVERAGE FINAL (triple capa) ─────────────────────────────
        final_leverage, lev_log = self._resolve_leverage(sym, ai_lev)
        log.info(f"[{sym}] {lev_log}")

        # ── BALANCE Y QTY ─────────────────────────────────────────────────────
        balance = self._get_balance()
        if balance < 5:
            log.warning(f"Balance insuficiente: {balance:.2f} USDT")
            return False

        qty, qty_log = self._calc_qty(sym, balance, mark, atr_v, final_leverage)
        if qty <= 0:
            log.warning(f"Skip {sym}: {qty_log}")
            return False
        log.info(f"[{sym}] {qty_log}")

        # ── TP / SL usando safe_price ──────────────────────────────────────────
        params     = self.learner.get_params()
        tp_mult    = params.get("tp_atr_mult", 2.5)
        sl_mult    = params.get("sl_atr_mult", 1.2)
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

        # ── EJECUTAR: set leverage SEGURO → place_order SEGURO ────────────────
        lev_resp = self.client.set_leverage(sym, final_leverage)
        lev_ok   = lev_resp.get("retCode", -1) in (0, 110043)  # 110043 = unchanged = OK
        if not lev_ok:
            log.warning(f"[{sym}] set_leverage warning: {lev_resp.get('retMsg')} → continúo")

        side_str = "Buy" if sig == "LONG" else "Sell"
        resp     = self.client.place_order(sym, side_str, qty, tp=tp, sl=sl)
        rc       = resp.get("retCode", -1)

        if rc != 0:
            err_msg = resp.get("retMsg", "")
            log.error(f"Order error {sym}: [{rc}] {err_msg}")
            notify_dev(
                f"❌ Order error {sym} [{rc}] {err_msg}\n"
                f"qty={qty} lev={final_leverage}x"
            )
            return False

        trade_id = _pending_id
        pos_data = {
            "trade_id":    trade_id,
            "symbol":      sym,
            "side":        sig,
            "entry_price": mark,
            "qty":         qty,
            "leverage":    final_leverage,
            "tp":          tp,
            "sl":          sl,
            "open_ts":     int(time.time()),
            "peak_price":  mark,
            "atr":         atr_v,
            "ai_decision": ai_decision,    # ← guardamos la decisión completa de la IA
        }
        with self._lock:
            self.open_positions[sym] = pos_data

        self.learner.record_open(trade_id, sym, sig, mark, qty,
                                 final_leverage, tp or 0, sl or 0, analysis)
        self.risk_mgr.on_open(sym)
        self._set_cooldown(sym)

        # ── Notificación Telegram ──────────────────────────────────────────────
        rr = abs((tp - mark) / (mark - sl)) if (tp and sl and abs(mark - sl) > 0) else 0
        aligned_txt = "✅ Perfectamente alineado" if analysis.get("aligned") else "⚠️ Parcialmente alineado"

        # Badges SMC para el mensaje
        smc_badges = []
        if analysis.get("smc_sweep"):       smc_badges.append("🌊 Sweep")
        if analysis.get("smc_ob_hit"):      smc_badges.append("🧱 OB")
        if analysis.get("smc_fvg_fill"):    smc_badges.append("🪟 FVG")
        if analysis.get("smc_vwap_retest"): smc_badges.append("🎯 VWAP")

        fg     = news_bias["fear_greed"]
        fg_lbl = news_bias["fg_label"]
        news_d = news_bias["direction"]
        news_e = "🟢" if news_d == "BULLISH" else ("🔴" if news_d == "BEARISH" else "⚪")

        msg = (
            f"{'🟢' if sig=='LONG' else '🔴'} <b>TRADE ABIERTO</b>\n"
            f"Par: <b>{sym}</b>  {sig}  x{final_leverage}\n"
            f"Entrada: <code>{mark:.4f}</code>  Qty: {qty}\n"
            f"TP: <code>{tp}</code>  SL: <code>{sl}</code>\n"
            f"R:R ≈ 1:{rr:.1f}\n"
            f"Score: {score:+.2f}  Conf: {conf:.0%}  [{entry_mode}]\n"
            f"{aligned_txt}\n"
            f"SMC: {' '.join(smc_badges) if smc_badges else 'sin setup'}\n"
            f"🤖 IA: {smc_anal[:60]}\n"
            f"   Noticias: {news_imp} | {news_e} {news_d}\n"
            f"   Leverage IA={ai_lev}x → final={final_leverage}x\n"
            f"Fear & Greed: {fg} — {fg_lbl}\n"
            f"Balance: {balance:.2f} USDT"
        )
        self.tg.send(msg)

        if notify_prefs.is_enabled("analysis"):
            self.tg.send(format_analysis_for_tg(analysis))

        if notify_prefs.is_enabled("ai_decisions"):
            ai_s = ai_filter.get_stats()
            if ai_s["calls"] > 0:
                self.tg.send(
                    f"🤖 <b>AI Risk Manager stats</b>\n"
                    f"✅ {ai_s['approved']} aprobados  🚫 {ai_s['rejected']} rechazados\n"
                    f"Tasa: {ai_s['approval_rate']:.1f}%  Tiempo: {ai_s['avg_ms']}ms\n"
                    f"Leverage cap IA: {ai_s['lev_cap']}x  Bot cap: {BOT_MAX_LEVERAGE}x"
                )

        log.info(
            f"✅ ABIERTO {sym} {sig} @ {mark:.4f}  "
            f"TP={tp}  SL={sl}  qty={qty}  lev={final_leverage}x  "
            f"score={score:.2f}  [AI_lev={ai_lev}x]"
        )
        return True

    # ── Cerrar trade ───────────────────────────────────────────────────────────

    def try_close_trade(self, sym: str, reason: str = "MANUAL",
                        pnl_override: float = None) -> bool:
        try:
            real      = self.client.get_positions(sym)
            real_size = sum(float(p.get("size", 0)) for p in real)
        except Exception:
            real_size = None

        with self._lock:
            pos = self.open_positions.get(sym)

        if real_size == 0:
            log.warning(f"{sym}: no hay posición en exchange, limpiando local")
            with self._lock:
                self.open_positions.pop(sym, None)
            return False

        if not pos:
            log.warning(f"No hay posición local en {sym}")
            return False

        try:
            self.client.cancel_all_orders(sym)
            resp = self.client.close_position(sym, pos["side"], pos["qty"])
            if isinstance(resp, dict) and resp.get("retCode") == 110017:
                log.warning(f"⚠️ {sym}: ya cerrada (110017)")
                with self._lock:
                    self.open_positions.pop(sym, None)
                return False
            if isinstance(resp, dict) and resp.get("retCode", -1) != 0:
                log.error(f"Close error {sym}: {resp.get('retMsg')}")
                return False
        except Exception as e:
            log.error(f"Excepción cerrando {sym}: {e}")
            return False

        mark = self.client.get_mark_price(sym)
        if pnl_override is not None:
            pnl = pnl_override
        else:
            entry = pos["entry_price"]
            pnl   = ((mark - entry) if pos["side"] == "LONG" else (entry - mark)) * pos["qty"]

        self.learner.record_close(pos["trade_id"], mark, pnl, reason)
        self.risk_mgr.on_close(sym, pnl)

        # ── Guardar outcome para que la IA aprenda ─────────────────────────────
        try:
            dur_s    = int(time.time()) - pos.get("open_ts", int(time.time()))
            entry_p  = pos.get("entry_price", 0) or mark
            raw_pct  = (mark - entry_p) / entry_p * 100 if entry_p > 0 else 0
            pnl_pct  = raw_pct if pos["side"] == "LONG" else -raw_pct
            result_s = "WIN" if pnl > 0.5 else ("LOSS" if pnl < -0.5 else "BREAKEVEN")
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
        dur_str = f"{dur_s//60}m {dur_s%60}s" if dur_s < 3600 else f"{dur_s//3600}h {(dur_s%3600)//60}m"
        result_emoji = "✅" if pnl > 0 else "❌"
        self.tg.send(
            f"{result_emoji} <b>TRADE CERRADO</b>\n"
            f"Par: <b>{sym}</b>  {pos['side']}  x{pos['leverage']}\n"
            f"Entrada: <code>{pos['entry_price']:.4f}</code>  "
            f"Cierre: <code>{mark:.4f}</code>\n"
            f"PnL: <code>{pnl:+.2f} USDT</code> {'🟢' if pnl >= 0 else '🔴'}  [{reason}]\n"
            f"Duración: {dur_str}\n"
            f"Balance aprox: {self._get_balance():.2f} USDT"
        )
        log.info(f"{'✅' if pnl>0 else '❌'} CERRADO {sym}  PnL={pnl:+.2f}  [{reason}]")
        return True

    # ── Monitor de posiciones ──────────────────────────────────────────────────

    def _monitor_loop(self):
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

    def _check_position(self, sym: str):
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
        info   = self.client.get_instrument_info(sym)
        tick   = info["tick_size"]

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
                        log.debug(f"Trail SL {sym}: {new_sl:.4f}")
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
                        log.debug(f"Trail SL {sym}: {new_sl:.4f}")
                    except Exception:
                        pass

        # ── Verificar cierre externo ───────────────────────────────────────────
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
                reason   = "TP" if pnl >= 0 else "SL"
                mark_now = self.client.get_mark_price(sym) or pos["entry_price"]
                with self._lock:
                    pos = self.open_positions.get(sym)
                if pos:
                    self.learner.record_close(pos["trade_id"], mark_now, pnl, reason)
                    self.risk_mgr.on_close(sym, pnl)
                    dur_s   = int(time.time()) - pos.get("open_ts", int(time.time()))
                    dur_str = f"{dur_s//60}m {dur_s%60}s"
                    result_e = "✅" if pnl >= 0 else "❌"
                    self.tg.send(
                        f"{result_e} <b>CERRADO {sym}</b> [{reason}]\n"
                        f"PnL: <code>{pnl:+.2f} USDT</code>  Duración: {dur_str}\n"
                        f"Balance: {self._get_balance():.2f} USDT"
                    )
                    with self._lock:
                        self.open_positions.pop(sym, None)
                    log.info(f"{'✅' if pnl>=0 else '❌'} {sym} [{reason}] PnL={pnl:+.2f}")
        except Exception as e:
            log.debug(f"_check_position cierre externo {sym}: {e}")

    # ── Escáner de mercado ─────────────────────────────────────────────────────

    def _scan_loop(self):
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
                        smc_flags = (
                            ("⚡SQUEEZE" if a.get("squeeze") else "") +
                            ("🌊SWEEP"   if a.get("smc_sweep")       else "") +
                            ("🧱OB"      if a.get("smc_ob_hit")      else "") +
                            ("🪟FVG"     if a.get("smc_fvg_fill")    else "") +
                            ("🎯VWAP"    if a.get("smc_vwap_retest") else "")
                        )
                        log.info(
                            f"📊 {sym}: {sig:5s}  score={a['composite_score']:+.2f}  "
                            f"conf={a['confidence']:.0%}  "
                            f"macro={a['macro_bias']}  mid={a['mid_bias']}  "
                            f"entry={a['entry_bias']}  {smc_flags}"
                        )
                        if sig != "FLAT":
                            from tg_controller import is_paused
                            if not is_paused():
                                self.try_open_trade(a)
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
                    opps = scan_best_opportunities(self.client, top_n=5,
                                                   min_volume_usdt=MIN_VOLUME_USDT)
                    if opps:
                        lines = ["<b>🔍 Top oportunidades SMC del mercado</b>"]
                        for o in opps:
                            e = "🟢" if o["signal"] == "LONG" else "🔴"
                            smc = f"{'🌊' if o.get('smc_sweep') else ''}{'🧱' if o.get('smc_ob_hit') else ''}"
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

    # ── Control ────────────────────────────────────────────────────────────────

    def start(self):
        self.running = True
        self.news.start()
        threading.Thread(target=self._scan_loop,    daemon=True, name="scanner").start()
        threading.Thread(target=self._monitor_loop, daemon=True, name="monitor").start()
        log.info("🚀 Bot autónomo en marcha")

    def stop(self):
        self.running = False
        self.news.stop()
        self.tg.send("⛔ <b>Bot detenido manualmente</b>")
        log.info("⛔ Bot detenido")

    def get_status(self) -> Dict:
        bal  = self._get_balance()
        perf = self.learner.get_performance_summary()
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
            "risk":           self.risk_mgr.get_status(),
            "news":           self.news.get_status(),
            "ts":             int(time.time()),
            "ai_filter":      ai_filter.get_stats(),
            "leverage_config": {
                "bot_max":   BOT_MAX_LEVERAGE,
                "bot_min":   BOT_MIN_LEVERAGE,
                "risk_pct":  BOT_MAX_RISK_PCT,
                "ai_cap":    ai_filter.lev_cap,
            },
        }

    def force_close_all(self):
        with self._lock:
            syms = list(self.open_positions.keys())
        for sym in syms:
            self.try_close_trade(sym, reason="CLOSE_ALL")

    def add_to_watchlist(self, sym: str):
        if sym not in FIXED_WATCHLIST:
            FIXED_WATCHLIST.append(sym)
            self.tg.send(f"➕ {sym} añadido al watchlist")

    def remove_from_watchlist(self, sym: str):
        if sym in FIXED_WATCHLIST:
            FIXED_WATCHLIST.remove(sym)
            self.tg.send(f"➖ {sym} removido del watchlist")

    def send_daily_summary(self):
        perf = self.learner.get_performance_summary()
        risk = self.risk_mgr.get_status()
        ai_s = ai_filter.get_stats()
        msg  = (
            f"📈 <b>Resumen diario</b>\n"
            f"Trades: {perf['total_trades']}  WR: {perf['win_rate']:.1f}%\n"
            f"PnL hoy: {risk['daily_pnl']:+.2f} USDT\n"
            f"PnL total: {perf['total_pnl']:+.2f} USDT\n"
            f"Mejor: +{perf['best_trade']:.2f}  Peor: {perf['worst_trade']:.2f}\n"
            f"Balance: {self._get_balance():.2f} USDT\n"
            f"🤖 IA: {ai_s['approved']} aprobados, {ai_s['rejected']} rechazados "
            f"({ai_s['approval_rate']:.1f}%)"
        )
        self.tg.send(msg)


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
            s = bot.get_status()
            p = s["performance"]
            ai = s["ai_filter"]
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"Bal:{s['balance_usdt']:.2f} | "
                f"Pos:{s['open_positions']} | "
                f"Trades:{p['total_trades']} | "
                f"WR:{p['win_rate']:.1f}% | "
                f"PnL:{p['total_pnl']:+.2f} | "
                f"AI:{ai['approved']}/{ai['calls']} aprob"
            )
            time.sleep(60)
    except KeyboardInterrupt:
        bot.stop()
