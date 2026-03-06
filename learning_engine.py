"""
learning_engine.py v2 — Motor de aprendizaje para máximo profit
"""

import json, logging, os, time
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

log = logging.getLogger("learning_engine")
MEMORY_FILE = "bot_memory.json"
STATS_FILE  = "strategy_stats.json"

# ── Parámetros iniciales AGRESIVOS (paper trading con 50k) ────────────────────
DEFAULT_PARAMS = {
    # Umbrales de score (el motor v3 ya los ajusta por modo)
    "min_score_long":   3.0,
    "min_score_short":  3.0,
    "min_confidence":   0.30,

    # Riesgo: 2% del balance por trade con leverage alto
    "risk_pct_per_trade": 2.0,
    "max_leverage":       50,
    "default_leverage":   20,    # 20x para maximizar profit

    # TP/SL adaptativos por modo (el analysis engine los ajusta)
    "tp_atr_mult":  2.2,
    "sl_atr_mult":  1.0,

    # Trailing stop agresivo
    "use_trailing":    True,
    "trail_atr_mult":  0.8,      # trail más ajustado = captura más profit

    # Cooldown reducido = más oportunidades
    "cooldown_seconds":    20,

    # Más posiciones simultáneas = más profit potencial
    "max_open_positions":  5,

    # Pesos de timeframes (se ajustan automáticamente)
    "tf_weights": {
        "1": 0.04, "3": 0.05, "5": 0.07, "15": 0.09,
        "30": 0.11, "60": 0.14, "120": 0.13,
        "240": 0.14, "360": 0.09, "720": 0.07, "D": 0.05, "W": 0.02,
    },

    # Límites de drawdown diario
    "max_daily_loss_pct":  8.0,   # % del balance
    "max_daily_loss_usdt": 5000,  # USDT absoluto
}


class LearningEngine:
    def __init__(self):
        self.memory = self._load(MEMORY_FILE, {
            "trade_history": [], "learned_params": {},
            "symbol_stats": {}, "tf_stats": {}, "session_pnl": 0.0,
        })
        self.stats  = self._load(STATS_FILE, {})
        self.params = {**DEFAULT_PARAMS, **self.memory.get("learned_params", {})}
        n = len([t for t in self.memory.get("trade_history", []) if t.get("result")])
        log.info(f"LearningEngine v2 — {n} trades cerrados en historial")

    def _load(self, path, default):
        if os.path.exists(path):
            try:
                with open(path) as f: return json.load(f)
            except Exception: pass
        return default

    def _save(self):
        try:
            self.memory["learned_params"] = self.params
            with open(MEMORY_FILE, "w") as f: json.dump(self.memory, f, indent=2)
            with open(STATS_FILE, "w")  as f: json.dump(self.stats, f, indent=2)
        except Exception as e:
            log.error(f"Error guardando: {e}")

    # ── Registro de trades ────────────────────────────────────────────────────

    def record_open(self, trade_id, symbol, side, entry_price, qty,
                    leverage, tp, sl, analysis):
        rec = {
            "trade_id": trade_id, "symbol": symbol, "side": side,
            "entry_price": entry_price, "qty": qty, "leverage": leverage,
            "tp": tp, "sl": sl, "open_ts": int(time.time()),
            "close_ts": None, "close_price": None,
            "pnl_usdt": None, "pnl_pct": None,
            "result": None, "close_reason": None,
            "entry_mode": analysis.get("entry_mode", "STANDARD"),
            "analysis_snapshot": {
                "composite_score": analysis.get("composite_score"),
                "confidence":      analysis.get("confidence"),
                "signal":          analysis.get("signal"),
                "entry_mode":      analysis.get("entry_mode"),
                "atr":             analysis.get("atr"),
                "squeeze":         analysis.get("squeeze"),
                "vol_spike":       analysis.get("vol_spike"),
                "tf_scores": {
                    tf: d.get("score")
                    for tf, d in (analysis.get("tf_details") or {}).items()
                },
            },
        }
        self.memory["trade_history"].append(rec)
        self._save()

    def record_close(self, trade_id, close_price, pnl_usdt, close_reason):
        for rec in self.memory["trade_history"]:
            if rec["trade_id"] != trade_id: continue
            rec["close_ts"]    = int(time.time())
            rec["close_price"] = close_price
            rec["pnl_usdt"]    = pnl_usdt
            rec["close_reason"]= close_reason
            rec["result"]      = "WIN" if pnl_usdt > 0.5 else ("LOSS" if pnl_usdt < -0.5 else "BREAKEVEN")
            entry = rec.get("entry_price") or close_price
            if entry:
                pct = (close_price - entry) / entry * 100
                rec["pnl_pct"] = round(pct if rec["side"] == "LONG" else -pct, 3)
            self._save()
            self._learn(rec)
            log.info(f"Trade cerrado: {trade_id}  PnL={pnl_usdt:.2f}  [{rec['result']}]")
            return
        log.warning(f"Trade {trade_id} no encontrado")

    # ── Motor de aprendizaje ──────────────────────────────────────────────────

    def _learn(self, rec):
        sym    = rec.get("symbol", "")
        result = rec.get("result", "BREAKEVEN")

        # Stats por símbolo
        ss = self.memory.setdefault("symbol_stats", {})
        if sym not in ss:
            ss[sym] = {"wins":0, "losses":0, "total_pnl":0.0, "trades":0, "best":0.0, "worst":0.0}
        s = ss[sym]
        s["trades"] += 1
        s["total_pnl"] += rec.get("pnl_usdt", 0)
        s["best"]   = max(s.get("best", 0),   rec.get("pnl_usdt", 0))
        s["worst"]  = min(s.get("worst", 0),  rec.get("pnl_usdt", 0))
        if result == "WIN":   s["wins"]   += 1
        elif result == "LOSS": s["losses"] += 1

        # Stats por modo de entrada
        mode = rec.get("entry_mode", "STANDARD")
        ms = self.memory.setdefault("mode_stats", {})
        if mode not in ms:
            ms[mode] = {"wins":0, "losses":0, "total_pnl":0.0, "trades":0}
        ms[mode]["trades"] += 1
        ms[mode]["total_pnl"] += rec.get("pnl_usdt", 0)
        if result == "WIN":   ms[mode]["wins"]   += 1
        elif result == "LOSS": ms[mode]["losses"] += 1

        # Ajuste cada 5 trades (más frecuente para adaptarse rápido)
        closed = [t for t in self.memory["trade_history"] if t.get("result")]
        if len(closed) >= 5 and len(closed) % 5 == 0:
            self._adjust(closed)

    def _adjust(self, closed):
        recent = closed[-30:]  # últimos 30 trades
        wins   = [t for t in recent if t["result"] == "WIN"]
        losses = [t for t in recent if t["result"] == "LOSS"]
        wr     = len(wins) / len(recent) if recent else 0.5

        win_pnls  = [t["pnl_usdt"] for t in wins   if t.get("pnl_usdt")]
        loss_pnls = [abs(t["pnl_usdt"]) for t in losses if t.get("pnl_usdt")]
        avg_win   = float(np.mean(win_pnls))  if win_pnls  else 1.0
        avg_loss  = float(np.mean(loss_pnls)) if loss_pnls else 1.0
        rr        = avg_win / avg_loss if avg_loss > 0 else 1.0

        # ── Kelly Criterion: f* = (WR×R - (1-WR)) / R
        kelly = (wr * rr - (1 - wr)) / rr if rr > 0 else 0
        kelly = max(0.005, min(0.05, kelly))  # entre 0.5% y 5%
        self.params["risk_pct_per_trade"] = round(kelly * 100, 2)

        # ── Ajustar umbral de score
        if wr > 0.65 and rr > 1.5:
            self.params["min_score_long"]  = max(2.0, self.params["min_score_long"]  - 0.3)
            self.params["min_score_short"] = max(2.0, self.params["min_score_short"] - 0.3)
        elif wr < 0.40 or rr < 1.0:
            self.params["min_score_long"]  = min(6.0, self.params["min_score_long"]  + 0.5)
            self.params["min_score_short"] = min(6.0, self.params["min_score_short"] + 0.5)

        # ── Ajustar leverage según performance
        if wr > 0.60 and rr > 2.0:
            self.params["default_leverage"] = min(50, self.params["default_leverage"] + 5)
        elif wr < 0.40:
            self.params["default_leverage"] = max(5,  self.params["default_leverage"] - 5)

        # ── Ajustar TP/SL
        if rr < 1.5:
            self.params["tp_atr_mult"] = min(4.0, self.params["tp_atr_mult"] + 0.15)
        elif rr > 3.0:
            self.params["tp_atr_mult"] = max(1.5, self.params["tp_atr_mult"] - 0.1)

        # ── Ajustar max posiciones según capital utilizado eficientemente
        if wr > 0.60:
            self.params["max_open_positions"] = min(8, self.params["max_open_positions"] + 1)
        elif wr < 0.35:
            self.params["max_open_positions"] = max(2, self.params["max_open_positions"] - 1)

        self._save()
        log.info(
            f"[LEARNING] WR={wr:.0%} RR={rr:.2f} Kelly={kelly:.1%} | "
            f"Riesgo={self.params['risk_pct_per_trade']:.2f}% "
            f"Lev={self.params['default_leverage']}x "
            f"Thresh={self.params['min_score_long']:.1f} "
            f"MaxPos={self.params['max_open_positions']}"
        )

    # ── API pública ───────────────────────────────────────────────────────────

    def get_params(self) -> Dict:
        return dict(self.params)

    def get_symbol_stats(self, symbol=None) -> Dict:
        s = self.memory.get("symbol_stats", {})
        return s.get(symbol, {}) if symbol else s

    def get_mode_stats(self) -> Dict:
        return self.memory.get("mode_stats", {})

    def get_performance_summary(self) -> Dict:
        history = self.memory.get("trade_history", [])
        closed  = [t for t in history if t.get("result")]
        wins    = [t for t in closed if t["result"] == "WIN"]
        losses  = [t for t in closed if t["result"] == "LOSS"]
        pnls    = [t["pnl_usdt"] for t in closed if t.get("pnl_usdt") is not None]
        return {
            "total_trades": len(closed),
            "open_trades":  len(history) - len(closed),
            "wins":  len(wins), "losses": len(losses),
            "win_rate":   round(len(wins)/len(closed)*100, 1) if closed else 0,
            "total_pnl":  round(sum(pnls), 2),
            "avg_win":    round(float(np.mean([t["pnl_usdt"] for t in wins])),  2) if wins   else 0,
            "avg_loss":   round(float(np.mean([t["pnl_usdt"] for t in losses])),2) if losses else 0,
            "best_trade": round(max(pnls), 2) if pnls else 0,
            "worst_trade":round(min(pnls), 2) if pnls else 0,
            "current_params": self.params,
        }

    def should_trade_symbol(self, symbol: str) -> Tuple[bool, str]:
        s = self.get_symbol_stats(symbol)
        if not s or s.get("trades", 0) < 5:
            return True, "Sin historial"
        wr = s["wins"] / s["trades"]
        if wr < 0.20 and s["trades"] >= 8:
            return False, f"WR muy bajo ({wr:.0%})"
        return True, f"WR {wr:.0%}"

    def calculate_position_size(self, balance: float, price: float,
                                  atr: float, leverage: int) -> float:
        """
        Tamaño basado en Kelly Criterion:
        qty = (balance × risk%) × leverage / price
        """
        risk_pct  = self.params.get("risk_pct_per_trade", 2.0) / 100
        sl_mult   = self.params.get("sl_atr_mult", 1.0)
        risk_usdt = balance * risk_pct

        if price <= 0: return 0.0

        # Calcular qty por riesgo
        if atr > 0 and sl_mult > 0:
            risk_per_unit = atr * sl_mult
            qty = risk_usdt / risk_per_unit
        else:
            qty = (risk_usdt * leverage) / price

        # Cap: máximo 8% del balance como margen inicial
        max_qty = (balance * 0.08 * leverage) / price
        qty = min(qty, max_qty)

        return round(qty, 4)
