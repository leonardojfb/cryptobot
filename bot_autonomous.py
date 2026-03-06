"""
bot_autonomous.py  v2
Bot autónomo con notificaciones Telegram en CADA evento relevante.
"""

import json, logging, os, time, threading, uuid, asyncio
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

# ── Config ────────────────────────────────────────────────────────────────────
BYBIT_API_KEY      = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET   = os.getenv("BYBIT_API_SECRET", "").strip()
PAPER_TRADING      = os.getenv("PAPER_TRADING", "true").lower() in ("1","true","yes")
SCAN_INTERVAL_SEC  = int(os.getenv("SCAN_INTERVAL_SEC", "30"))
MONITOR_INTERVAL   = int(os.getenv("MONITOR_INTERVAL_SEC", "10"))
AUTO_SCAN_ENABLED  = os.getenv("AUTO_SCAN", "true").lower() in ("1","true","yes")
MIN_VOLUME_USDT    = float(os.getenv("MIN_VOLUME_USDT", "5000000"))

TG_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID",  "").strip()

FIXED_WATCHLIST: List[str] = [
    s.strip() for s in
    os.getenv("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",")
    if s.strip()
]

# Símbolos problemáticos conocidos (no operar/limpiar tracking)
PROBLEMATIC_SYMBOLS = {"BARDUSDT", "POWERUSDT"}   # símbolos con comportamiento inestable


# ══════════════════════════════════════════════════════════
#  NOTIFICADOR TELEGRAM  (sincrónico, hilo propio)
# ══════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    Envía mensajes a Telegram usando la HTTP API directamente (sin dependencia de
    python-telegram-bot en este módulo). Thread-safe mediante una cola.
    """
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
        """Envío sincrónico con log de resultado — útil para test"""
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
            else:
                log.error(f"❌ Telegram error: {data.get('description')} "
                          f"(chat_id={self.chat_id})")
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
                        log.error(f"❌ TG error: {data.get('description')} (chat_id={self.chat_id})")
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

        self.news  = NewsEngine(telegram_notifier=self.tg, scan_interval=120)
        self.open_positions: Dict[str, Dict] = {}
        self.cooldowns:      Dict[str, float] = {}
        self.running = False
        self._lock   = threading.Lock()
        self._instruments: Dict[str, Dict] = {}  # cache de filtros

        self._sync_positions()
        mode = "🟡 PAPER" if PAPER_TRADING else "🔴 REAL"
        msg = (f"🤖 <b>Bot iniciado — {mode}</b>\n"
               f"Watchlist: {', '.join(FIXED_WATCHLIST)}\n"
               f"Scan: cada {SCAN_INTERVAL_SEC}s | Monitor: cada {MONITOR_INTERVAL}s\n"
               f"TFs analizados: {len(ALL_TF)} ({', '.join(ALL_TF)})")
        # Test sincrónico — así sabemos inmediatamente si Telegram funciona
        if self.tg._active:
            ok = self.tg.send_direct(msg)
            if not ok:
                log.error(
                    "❌ Telegram no pudo enviar. Verifica TELEGRAM_CHAT_ID en .env\n"
                    "   Para obtener tu chat_id: escribe al bot @userinfobot en Telegram"
                )
        log.info(f"Bot iniciado — {mode} — {FIXED_WATCHLIST}")

    # ── Posiciones ────────────────────────────────────────────────────────────

    def _sync_positions(self):
        """Sincroniza posiciones locales con el exchange.
        - Registra posiciones NUEVAS en learning_engine (record_open)
        - Registra cierres de posiciones FANTASMAS con PnL real (record_close)
        - Sólo loggea si el estado cambia
        """
        try:
            new_pos = {}
            for p in self.client.get_positions():
                try:
                    if float(p.get("size", 0)) == 0:
                        continue
                except Exception:
                    continue
                sym  = p["symbol"]
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
                }

            old_keys = set(self.open_positions.keys())
            new_keys = set(new_pos.keys())

            # ── Posiciones NUEVAS (abiertas externamente / al arrancar) ───────
            for sym in new_keys - old_keys:
                pos = new_pos[sym]
                log.info(f"📡 Nueva posición detectada: {sym} {pos['side']} @ {pos['entry_price']}")
                # Registrar en learning_engine para tracking correcto
                fake_analysis = {
                    "composite_score": 0.0, "confidence": 0.5,
                    "signal": pos["side"], "entry_mode": "SYNCED",
                    "atr": 0, "squeeze": False, "vol_spike": False, "tf_details": {},
                }
                self.learner.record_open(
                    pos["trade_id"], sym, pos["side"],
                    pos["entry_price"], pos["qty"], pos["leverage"],
                    pos["tp"] or 0, pos["sl"] or 0, fake_analysis
                )
                self.risk_mgr.on_open(sym)

            # ── Posiciones FANTASMA (cerradas externamente: TP/SL hit) ────────
            for sym in old_keys - new_keys:
                pos = self.open_positions[sym]
                log.warning(f"👻 {sym}: posición cerrada externamente, registrando PnL...")
                # Obtener PnL real del exchange
                pnl = 0.0
                try:
                    closed = self.client.get_closed_pnl(sym, limit=3)
                    if closed:
                        pnl = float(closed[0].get("closedPnl", 0))
                except Exception:
                    pass
                reason = "TP" if pnl >= 0 else "SL"
                trade_id = pos.get("trade_id", f"sync_{sym}")
                self.learner.record_close(trade_id, self.client.get_mark_price(sym), pnl, reason)
                self.risk_mgr.on_close(sym, pnl)
                # Notificar
                result_e = "✅" if pnl >= 0 else "❌"
                self.tg.send(
                    f"{result_e} <b>{sym}</b> cerrado por {reason}\n"
                    f"PnL: <code>{pnl:+.2f} USDT</code>"
                )
                with self._lock:
                    self.open_positions.pop(sym, None)

            # ── Actualizar/añadir posiciones activas ──────────────────────────
            with self._lock:
                self.open_positions.update(new_pos)

            # Sólo loggear si cambió algo
            if old_keys != new_keys:
                log.info(f"Posiciones sync: {sorted(self.open_positions.keys()) or 'ninguna'}")

        except Exception as e:
            log.error(f"_sync_positions: {e}")

    # ── Utilidades ────────────────────────────────────────────────────────────

    def _in_cooldown(self, sym: str) -> bool:
        return time.time() < self.cooldowns.get(sym, 0)

    def _set_cooldown(self, sym: str):
        self.cooldowns[sym] = time.time() + self.learner.params.get("cooldown_seconds", 60)

    def _get_balance(self) -> float:
        """Obtiene balance USDT disponible para futuros (robusto para cuenta demo)"""
        try:
            return self.client.get_usdt_balance()
        except Exception as e:
            log.error(f"Error obteniendo balance: {e}")
            return 0.0

    def _get_filters(self, symbol: str) -> Dict:
        if symbol in self._instruments:
            return self._instruments[symbol]
        try:
            for inst in self.client.get_instruments():
                s = inst.get("symbol","")
                lot   = inst.get("lotSizeFilter",  {})
                price = inst.get("priceFilter",    {})
                self._instruments[s] = {
                    "qty_step":  float(lot.get("qtyStep",   0.001)),
                    "min_qty":   float(lot.get("minOrderQty", 0.001)),
                    "tick_size": float(price.get("tickSize", 0.01)),
                }
        except Exception:
            pass
        return self._instruments.get(symbol, {"qty_step": 0.001, "min_qty": 0.001, "tick_size": 0.01})

    def _round_qty(self, qty: float, step: float) -> float:
        if step <= 0: return round(qty, 3)
        return round(int(qty / step) * step, 10)

    def _round_price(self, price: float, tick: float) -> float:
        if tick <= 0: return round(price, 4)
        return round(round(price / tick) * tick, 10)

    # ── Abrir trade ───────────────────────────────────────────────────────────

    def try_open_trade(self, analysis: Dict) -> bool:
        sym   = analysis["symbol"]

        # Bloqueos baratos primero (sin llamadas API)
        if sym in PROBLEMATIC_SYMBOLS:
            return False
        if self._in_cooldown(sym):
            return False

        sig   = analysis["signal"]
        score = analysis["composite_score"]
        conf  = analysis["confidence"]
        mark  = analysis["mark_price"]
        atr_v = analysis.get("atr") or 0
        aligned  = analysis.get("aligned", False)
        params   = self.learner.get_params()
        # ── Modo de entrada: threshold dinámico ───────────────
        # AGGRESSIVE (squeeze) → 2.5 | MOMENTUM → 3.0 | STANDARD → params
        entry_mode = analysis.get("entry_mode", "STANDARD")
        if entry_mode == "AGGRESSIVE":
            threshold = 2.5
        elif entry_mode == "MOMENTUM":
            threshold = 3.0
        else:
            threshold = params.get("min_score_long" if sig=="LONG" else "min_score_short", 3.0)

        # ── Filtros de entrada ────────────────────────────────
        if sig == "FLAT": return False
        if abs(score) < threshold:
            log.debug(f"Skip {sym}: score {score:+.2f} < threshold {threshold:.1f} [{entry_mode}]")
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

        # ── Filtro de noticias ────────────────────────────
        news_bias = self.news.get_news_bias(sym)
        if news_bias["should_block"]:
            log.info(f"🚫 {sym} bloqueado por noticias negativas: "
                     f"score={news_bias['news_score']:.2f}")
            return False
        # Ajustar score compuesto con sentimiento de noticias y Fear&Greed
        news_adj = news_bias["news_score"] * 0.5 + news_bias["fg_adj"] * 0.3
        adjusted_score = score + news_adj
        # Si noticias son muy contrarias a la señal técnica, subir umbral
        if sig == "LONG"  and news_bias["direction"] == "BEARISH":
            adjusted_score -= 0.5
        if sig == "SHORT" and news_bias["direction"] == "BULLISH":
            adjusted_score -= 0.5
        if abs(adjusted_score) < threshold:
            log.info(f"Skip {sym}: score ajustado por noticias {score:.2f}→{adjusted_score:.2f}")
            return False

        # ── Filtro de IA (DeepSeek) ───────────────────────────────────────
        import uuid as _uuid
        _pending_id = str(_uuid.uuid4())[:8]
        ai_ok, ai_reason = ai_filter.should_trade(
            analysis,
            symbol_stats=self.learner.get_symbol_stats(sym),
            news_bias=self.news.get_news_bias(sym),
            recent_news=self.news.get_recent_news(5),
            trade_id=_pending_id,
        )
        if not ai_ok:
            log.info(f"🤖 AI rechazó {sym}: {ai_reason}")
            notify("ai_decisions",
                f"🤖 <b>IA rechazó trade</b>\n"
                f"Par: {sym}  {sig}\n"
                f"Score: {score:+.2f}  Conf: {conf:.0%}\n"
                f"Razón: {ai_reason}"
            )
            return False

        balance = self._get_balance()
        if balance < 5:
            log.warning(f"Balance insuficiente: {balance:.2f} USDT")
            return False

        leverage = min(params.get("default_leverage", 10), params.get("max_leverage", 20))
        qty = self.learner.calculate_position_size(balance, mark, atr_v, leverage)
        if qty <= 0: return False

        filt = self._get_filters(sym)
        qty  = self._round_qty(qty, filt["qty_step"])
        if qty < filt["min_qty"]: return False

        # TP / SL
        tp_mult = params.get("tp_atr_mult", 2.5)
        sl_mult = params.get("sl_atr_mult", 1.2)
        if atr_v > 0:
            if sig == "LONG":
                tp = self._round_price(mark + atr_v * tp_mult, filt["tick_size"])
                sl = self._round_price(mark - atr_v * sl_mult, filt["tick_size"])
            else:
                tp = self._round_price(mark - atr_v * tp_mult, filt["tick_size"])
                sl = self._round_price(mark + atr_v * sl_mult, filt["tick_size"])
        else:
            tp, sl = analysis.get("tp"), analysis.get("sl")

        # ── Ejecutar orden ────────────────────────────────────
        try:
            self.client.set_leverage(sym, leverage)
        except Exception:
            pass

        side_str = "Buy" if sig == "LONG" else "Sell"
        resp = self.client.place_order(sym, side_str, qty, tp=tp, sl=sl)

        if resp.get("retCode", -1) != 0:
            log.error(f"Order error {sym}: {resp.get('retMsg')}")
            return False

        trade_id = locals().get("_pending_id") or str(uuid.uuid4())[:8]
        pos_data = {
            "trade_id":    trade_id,
            "symbol":      sym,
            "side":        sig,
            "entry_price": mark,
            "qty":         qty,
            "leverage":    leverage,
            "tp":          tp,
            "sl":          sl,
            "open_ts":     int(time.time()),
            "peak_price":  mark,
            "atr":         atr_v,
        }
        with self._lock:
            self.open_positions[sym] = pos_data

        self.learner.record_open(trade_id, sym, sig, mark, qty, leverage,
                                 tp or 0, sl or 0, analysis)
        self.risk_mgr.on_open(sym)
        self._set_cooldown(sym)

        # ── Notificación Telegram ─────────────────────────────
        rr = abs((tp - mark) / (mark - sl)) if tp and sl and (mark - sl) != 0 else 0
        aligned_txt = "✅ Perfectamente alineado" if aligned else "⚠️ Parcialmente alineado"
        news_bias = self.news.get_news_bias(sym)
        fg = news_bias["fear_greed"]
        fg_lbl = news_bias["fg_label"]
        news_dir = news_bias["direction"]
        news_emoji = "🟢" if news_dir=="BULLISH" else ("🔴" if news_dir=="BEARISH" else "⚪")
        msg = (
            f"{'🟢' if sig=='LONG' else '🔴'} <b>TRADE ABIERTO</b>\n"
            f"Par: <b>{sym}</b>  {sig}  x{leverage}\n"
            f"Entrada: <code>{mark:.4f}</code>  Qty: {qty}\n"
            f"TP: <code>{tp}</code>  SL: <code>{sl}</code>\n"
            f"R:R ≈ 1:{rr:.1f}\n"
            f"Score: {score:+.2f}  Conf: {conf:.0%}\n"
            f"{aligned_txt}\n"
            f"Macro: {analysis.get('macro_bias')}  "
            f"Mid: {analysis.get('mid_bias')}  "
            f"Entry: {analysis.get('entry_bias')}\n"
            f"Balance: {balance:.2f} USDT\n"
            f"Noticias: {news_emoji} {news_dir} ({news_bias['news_score']:+.2f})\n"
            f"Fear & Greed: {fg} — {fg_lbl}"
        )
        self.tg.send(msg)
        # Análisis detallado en mensaje separado
        # Análisis técnico detallado (solo si pref activa)
        if notify_prefs.is_enabled("analysis"):
            self.tg.send(format_analysis_for_tg(analysis))
        # Stats IA solo si categoría activa
        if notify_prefs.is_enabled("ai_decisions"):
            ai_s = ai_filter.get_stats()
            if ai_s["calls"] > 0:
                self.tg.send(
                    f"🤖 <b>AI Filter stats</b>\n"
                    f"✅ {ai_s['approved']} aprobados  🚫 {ai_s['rejected']} rechazados\n"
                    f"Tasa: {ai_s['approval_rate']:.1f}%  Tiempo: {ai_s['avg_ms']}ms"
                )

        log.info(f"✅ ABIERTO {sym} {sig} @ {mark:.4f}  TP={tp}  SL={sl}  score={score:.2f}")
        return True

    # ── Cerrar trade ──────────────────────────────────────────────────────────

    def try_close_trade(self, sym: str, reason: str = "MANUAL",
                        pnl_override: float = None) -> bool:
        # Verificar estado real antes de intentar cerrar
        try:
            real = self.client.get_positions(sym)
            real_size = sum(float(p.get("size", 0)) for p in real)
        except Exception:
            real_size = None

        with self._lock:
            pos = self.open_positions.get(sym)

        if real_size == 0:
            # Si el exchange no muestra posición, limpiar tracking local
            log.warning(f"{sym}: no hay posición en exchange, limpiando tracking local")
            with self._lock:
                if sym in self.open_positions:
                    del self.open_positions[sym]
            return False

        if not pos:
            log.warning(f"No hay posición local en {sym}")
            return False

        try:
            self.client.cancel_all_orders(sym)
            resp = self.client.close_position(sym, pos["side"], pos["qty"])
            # Manejo race-condition 110017 (posición ya cerrada)
            if isinstance(resp, dict) and resp.get("retCode") == 110017:
                log.warning(f"⚠️ {sym}: Posición ya cerrada (110017)")
                with self._lock:
                    if sym in self.open_positions:
                        del self.open_positions[sym]
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
            pnl = ((mark - entry) if pos["side"] == "LONG" else (entry - mark)) * pos["qty"]

        self.learner.record_close(pos["trade_id"], mark, pnl, reason)
        self.risk_mgr.on_close(sym, pnl)

        # ── Guardar outcome en DB para que la IA aprenda ─────────────────
        try:
            _dur_s   = int(time.time()) - pos.get("open_ts", int(time.time()))
            _entry   = pos.get("entry_price", 0) or mark
            _raw_pct = (mark - _entry) / _entry * 100 if _entry > 0 else 0
            _pnl_pct = _raw_pct if pos["side"] == "LONG" else -_raw_pct
            _result  = "WIN" if pnl > 0.5 else ("LOSS" if pnl < -0.5 else "BREAKEVEN")
            ai_filter.record_outcome(
                trade_id    = pos["trade_id"],
                symbol      = sym,
                side        = pos["side"],
                entry_price = _entry,
                close_price = mark,
                pnl_usdt    = round(pnl, 4),
                pnl_pct     = round(_pnl_pct, 3),
                result      = _result,
                close_reason= reason,
                duration_s  = _dur_s,
                leverage    = pos.get("leverage", 1),
                ts_open     = pos.get("open_ts", int(time.time())),
            )
        except Exception as _e:
            log.warning(f"record_outcome {sym}: {_e}")

        with self._lock:
            del self.open_positions[sym]

        # ── Notificación Telegram ─────────────────────────────
        dur_s = int(time.time()) - pos.get("open_ts", int(time.time()))
        dur_str = f"{dur_s//60}m {dur_s%60}s" if dur_s < 3600 else f"{dur_s//3600}h {(dur_s%3600)//60}m"
        result_emoji = "✅" if pnl > 0 else "❌"
        msg = (
            f"{result_emoji} <b>TRADE CERRADO</b>\n"
            f"Par: <b>{sym}</b>  {pos['side']}  x{pos['leverage']}\n"
            f"Entrada: <code>{pos['entry_price']:.4f}</code>  "
            f"Cierre: <code>{mark:.4f}</code>\n"
            f"PnL: <code>{pnl:+.2f} USDT</code> {'🟢' if pnl >= 0 else '🔴'}  [{reason}]\n"
            f"Duración: {dur_str}\n"
            f"Balance aprox: {self._get_balance():.2f} USDT"
        )
        self.tg.send(msg)
        log.info(f"{'✅' if pnl>0 else '❌'} CERRADO {sym}  PnL={pnl:+.2f}  [{reason}]")
        return True

    # ── Monitor de posiciones ─────────────────────────────────────────────────

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
        if not pos: return

        mark = self.client.get_mark_price(sym)
        if not mark: return

        side   = pos["side"]
        atr_v  = pos.get("atr") or 0
        params = self.learner.get_params()
        filt   = self._get_filters(sym)

        # ── Trailing stop ─────────────────────────────────────
        if params.get("use_trailing", True) and atr_v:
            peak      = pos.get("peak_price", pos["entry_price"])
            trail_m   = params.get("trail_atr_mult", 1.0)
            if side == "LONG" and mark > peak:
                new_sl = self._round_price(mark - atr_v * trail_m, filt["tick_size"])
                if new_sl > (pos.get("sl") or 0):
                    try:
                        self.client.set_tp_sl(sym, sl=new_sl)
                        with self._lock:
                            self.open_positions[sym]["sl"] = new_sl
                            self.open_positions[sym]["peak_price"] = mark
                        log.debug(f"Trail SL {sym}: {new_sl:.4f}")
                    except Exception:
                        pass
            elif side == "SHORT" and mark < peak:
                new_sl = self._round_price(mark + atr_v * trail_m, filt["tick_size"])
                if new_sl < (pos.get("sl") or 999999):
                    try:
                        self.client.set_tp_sl(sym, sl=new_sl)
                        with self._lock:
                            self.open_positions[sym]["sl"] = new_sl
                            self.open_positions[sym]["peak_price"] = mark
                        log.debug(f"Trail SL {sym}: {new_sl:.4f}")
                    except Exception:
                        pass

        # ── Verificar cierre externo (TP/SL hit) ─────────────
        try:
            real = self.client.get_positions(sym)
            real_size = sum(float(p.get("size", 0)) for p in real)
            if real_size == 0 and sym in self.open_positions:
                # Posición cerrada en exchange (TP/SL hit o cierre manual)
                # Obtener PnL real antes de limpiar
                pnl = 0.0
                try:
                    closed = self.client.get_closed_pnl(sym, limit=3)
                    if closed:
                        pnl = float(closed[0].get("closedPnl", 0))
                except Exception:
                    pass
                reason = "TP" if pnl >= 0 else "SL"
                with self._lock:
                    pos = self.open_positions.get(sym)
                if pos:
                    # Registrar en learning_engine y risk_mgr
                    mark_now = self.client.get_mark_price(sym) or pos["entry_price"]
                    self.learner.record_close(pos["trade_id"], mark_now, pnl, reason)
                    self.risk_mgr.on_close(sym, pnl)
                    # Notificar
                    dur_s = int(time.time()) - pos.get("open_ts", int(time.time()))
                    dur_str = f"{dur_s//60}m {dur_s%60}s"
                    result_e = "✅" if pnl >= 0 else "❌"
                    self.tg.send(
                        f"{result_e} <b>CERRADO {sym}</b> [{reason}]\n"
                        f"PnL: <code>{pnl:+.2f} USDT</code>  Duración: {dur_str}\n"
                        f"Balance: {self._get_balance():.2f} USDT"
                    )
                    with self._lock:
                        self.open_positions.pop(sym, None)
                    log.info(f"{'✅' if pnl>=0 else '❌'} {sym} cerrado [{reason}] PnL={pnl:+.2f}")
        except Exception as e:
            log.debug(f"_check_position cierre externo {sym}: {e}")

    # ── Escáner de mercado ────────────────────────────────────────────────────

    def _scan_loop(self):
        log.info("Escáner iniciado")
        cycle = 0
        while self.running:
            try:
                # ── Sync al inicio de cada ciclo (1 sola llamada API) ─────
                try:
                    self._sync_positions()
                except Exception:
                    pass

                # ── Watchlist fija (análisis COMPLETO de todos los TF) ────
                for sym in list(FIXED_WATCHLIST):
                    if not self.running: break
                    try:
                        a = analyze_symbol(self.client, sym,
                                           timeframes=ALL_TF)
                        sig = a["signal"]
                        log.info(
                            f"📊 {sym}: {sig:5s}  score={a['composite_score']:+.2f}  "
                            f"conf={a['confidence']:.0%}  "
                            f"macro={a['macro_bias']}  mid={a['mid_bias']}  "
                            f"entry={a['entry_bias']}"
                            + ("  ⚡SQUEEZE" if a.get("squeeze") else "")
                        )
                        if sig != "FLAT":
                            from tg_controller import is_paused
                            if not is_paused():
                                self.try_open_trade(a)
                            else:
                                log.debug(f"Bot pausado, skip {sym}")
                        # Enviar señal solo si la pref está activa
                        if notify_prefs.is_enabled("signals"):
                            if sig != "FLAT":
                                self.tg.send(format_analysis_for_tg(a))
                            elif cycle % 10 == 0:
                                self.tg.send(format_analysis_for_tg(a))
                    except Exception as e:
                        log.warning(f"scan {sym}: {e}")
                        notify_dev(f"scan {sym}: {e}")
                    time.sleep(1.5)

                # ── Auto-scan del mercado (cada 3 ciclos para no saturar) ─
                if AUTO_SCAN_ENABLED and cycle % 3 == 0:
                    log.info("🔍 Auto-scan mercado...")
                    opps = scan_best_opportunities(self.client, top_n=5,
                                                   min_volume_usdt=MIN_VOLUME_USDT)
                    if opps:
                        # Notificar las mejores oportunidades aunque no se tradeen
                        lines = ["<b>🔍 Top oportunidades del mercado</b>"]
                        for o in opps:
                            e = "🟢" if o["signal"]=="LONG" else "🔴"
                            lines.append(
                                f"{e} {o['symbol']:12s} {o['signal']:5s}  "
                                f"score={o['composite_score']:+.2f}  conf={o['confidence']:.0%}"
                            )
                        self.tg.send("\n".join(lines))
                        for o in opps:
                            if not self.running: break
                            self.try_open_trade(o)

                cycle += 1

            except Exception as e:
                log.error(f"scan_loop: {e}")

            time.sleep(SCAN_INTERVAL_SEC)

    # ── Control ───────────────────────────────────────────────────────────────

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
        """Llama esto cada día para enviar resumen a Telegram"""
        perf = self.learner.get_performance_summary()
        risk = self.risk_mgr.get_status()
        msg  = (
            f"📈 <b>Resumen diario</b>\n"
            f"Trades: {perf['total_trades']}  WR: {perf['win_rate']:.1f}%\n"
            f"PnL hoy: {risk['daily_pnl']:+.2f} USDT\n"
            f"PnL total: {perf['total_pnl']:+.2f} USDT\n"
            f"Mejor: +{perf['best_trade']:.2f}  Peor: {perf['worst_trade']:.2f}\n"
            f"Balance: {self._get_balance():.2f} USDT"
        )
        self.tg.send(msg)


# ── Entry point directo ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                        handlers=[logging.FileHandler("bot.log","a","utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    bot = AutonomousBot()
    bot.start()
    try:
        while True:
            s = bot.get_status()
            p = s["performance"]
            print(f"[{time.strftime('%H:%M:%S')}] "
                  f"Bal:{s['balance_usdt']:.2f} | "
                  f"Pos:{s['open_positions']} | "
                  f"Trades:{p['total_trades']} | "
                  f"WR:{p['win_rate']:.1f}% | "
                  f"PnL:{p['total_pnl']:+.2f}")
            time.sleep(60)
    except KeyboardInterrupt:
        bot.stop()
