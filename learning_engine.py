"""
learning_engine.py v3 — Motor de aprendizaje + tracking por strategy_type
══════════════════════════════════════════════════════════════════════════
Cambios vs v2:
  - record_open()  acepta y persiste strategy_type
  - record_close() retorna strategy_type para pasarlo al RiskManager
  - should_trade_symbol usa RC codes
  - get_performance_summary incluye breakdown por strategy_type
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from reason_codes import (
    RC, VALID_STRATEGY_TYPES, ENTRY_MODE_TO_STRATEGY
)

log = logging.getLogger("learning_engine")

MEMORY_FILE = "bot_memory.json"
STATS_FILE  = "strategy_stats.json"

DEFAULT_PARAMS: Dict[str, Any] = {
    "min_score_long":      3.0,
    "min_score_short":     3.0,
    "min_confidence":      0.30,
    "risk_pct_per_trade":  2.0,
    "max_leverage":        50,
    "default_leverage":    20,
    "tp_atr_mult":         2.2,
    "sl_atr_mult":         1.0,
    "use_trailing":        True,
    "trail_atr_mult":      0.8,
    "cooldown_seconds":    20,
    "max_open_positions":  5,
    "tf_weights": {
        "1":0.04,"3":0.05,"5":0.07,"15":0.09,
        "30":0.11,"60":0.14,"120":0.13,
        "240":0.14,"360":0.09,"720":0.07,"D":0.05,"W":0.02,
    },
    "max_daily_loss_pct":  8.0,
    "max_daily_loss_usdt": 5000,
}


class LearningEngine:
    def __init__(self) -> None:
        self.memory = self._load(MEMORY_FILE, {
            "trade_history":  [],
            "learned_params": {},
            "symbol_stats":   {},
            "mode_stats":     {},
            "strategy_stats": {s: {"wins":0,"losses":0,"total_pnl":0.0,"trades":0}
                                for s in VALID_STRATEGY_TYPES},
            "session_pnl":    0.0,
        })
        self.stats  = self._load(STATS_FILE, {})
        self.params: Dict[str, Any] = {
            **DEFAULT_PARAMS,
            **self.memory.get("learned_params", {}),
        }
        n = len([t for t in self.memory.get("trade_history", []) if t.get("result")])
        log.info(f"LearningEngine v3 — {n} trades cerrados")

    # ── Persistencia ──────────────────────────────────────────────────────────

    def _load(self, path: str, default: Dict) -> Dict:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return default

    def _save(self) -> None:
        try:
            self.memory["learned_params"] = self.params
            with open(MEMORY_FILE, "w") as f:
                json.dump(self.memory, f, indent=2)
            with open(STATS_FILE,  "w") as f:
                json.dump(self.stats,  f, indent=2)
        except Exception as e:
            log.error(f"Error guardando LearningEngine: {e}")

    # ── Registro de trades ─────────────────────────────────────────────────────

    def record_open(
        self,
        trade_id:    str,
        symbol:      str,
        side:        str,
        entry_price: float,
        qty:         float,
        leverage:    int,
        tp:          float,
        sl:          float,
        analysis:    Dict,
    ) -> None:
        entry_mode    = analysis.get("entry_mode", "STANDARD")
        strategy_type = ENTRY_MODE_TO_STRATEGY.get(entry_mode, "NORMAL")

        rec: Dict[str, Any] = {
            "trade_id":     trade_id,
            "symbol":       symbol,
            "side":         side,
            "entry_price":  entry_price,
            "qty":          qty,
            "leverage":     leverage,
            "tp":           tp,
            "sl":           sl,
            "open_ts":      int(time.time()),
            "close_ts":     None,
            "close_price":  None,
            "pnl_usdt":     None,
            "pnl_pct":      None,
            "result":       None,
            "close_reason": None,
            "entry_mode":   entry_mode,
            "strategy_type":strategy_type,   # ← nuevo campo
            "analysis_snapshot": {
                "composite_score": analysis.get("composite_score"),
                "confidence":      analysis.get("confidence"),
                "signal":          analysis.get("signal"),
                "entry_mode":      entry_mode,
                "strategy_type":   strategy_type,
                "atr":             analysis.get("atr"),
                "squeeze":         analysis.get("squeeze"),
                "vol_spike":       analysis.get("vol_spike"),
                "smc_sweep":       analysis.get("smc_sweep"),
                "smc_ob_hit":      analysis.get("smc_ob_hit"),
                "smc_fvg_fill":    analysis.get("smc_fvg_fill"),
                "smc_vwap_retest": analysis.get("smc_vwap_retest"),
                "tf_scores": {
                    tf: d.get("score")
                    for tf, d in (analysis.get("tf_details") or {}).items()
                },
            },
        }
        self.memory["trade_history"].append(rec)
        self._save()

    def record_close(
        self,
        trade_id:    str,
        close_price: float,
        pnl_usdt:    float,
        close_reason: str,
    ) -> Optional[str]:
        """
        Registra el cierre de un trade.
        RETORNA el strategy_type del trade para que el caller
        pueda pasarlo a risk_manager.on_close().
        """
        for rec in self.memory["trade_history"]:
            if rec["trade_id"] != trade_id:
                continue
            rec["close_ts"]    = int(time.time())
            rec["close_price"] = close_price
            rec["pnl_usdt"]    = pnl_usdt
            rec["close_reason"]= close_reason
            rec["result"]      = (
                "WIN"  if pnl_usdt >  0.5 else
                "LOSS" if pnl_usdt < -0.5 else
                "BREAKEVEN"
            )
            entry = rec.get("entry_price") or close_price
            if entry:
                pct = (close_price - entry) / entry * 100
                rec["pnl_pct"] = round(
                    pct if rec["side"] == "LONG" else -pct, 3
                )
            strategy_type = rec.get("strategy_type", "NORMAL")
            self._save()
            self._learn(rec)
            log.info(
                f"[{RC.RISK_EXPOSURE_CHECKED}] "
                f"trade_id={trade_id} pnl={pnl_usdt:+.2f} "
                f"result={rec['result']} strategy={strategy_type}"
            )
            return strategy_type

        log.warning(f"Trade {trade_id} no encontrado en historial")
        return None

    # ── Motor de aprendizaje ───────────────────────────────────────────────────

    def _learn(self, rec: Dict) -> None:
        sym           = rec.get("symbol", "")
        result        = rec.get("result", "BREAKEVEN")
        strategy_type = rec.get("strategy_type", "NORMAL")

        # Stats por símbolo
        ss = self.memory.setdefault("symbol_stats", {})
        if sym not in ss:
            ss[sym] = {"wins":0,"losses":0,"total_pnl":0.0,"trades":0,"best":0.0,"worst":0.0}
        s = ss[sym]
        s["trades"]    += 1
        s["total_pnl"]  = round(s["total_pnl"] + (rec.get("pnl_usdt") or 0), 4)
        s["best"]       = max(s.get("best",  0), rec.get("pnl_usdt") or 0)
        s["worst"]      = min(s.get("worst", 0), rec.get("pnl_usdt") or 0)
        if result == "WIN":    s["wins"]   += 1
        elif result == "LOSS": s["losses"] += 1

        # Stats por modo de entrada
        mode = rec.get("entry_mode", "STANDARD")
        ms   = self.memory.setdefault("mode_stats", {})
        if mode not in ms:
            ms[mode] = {"wins":0,"losses":0,"total_pnl":0.0,"trades":0}
        ms[mode]["trades"]   += 1
        ms[mode]["total_pnl"] = round(ms[mode]["total_pnl"] + (rec.get("pnl_usdt") or 0), 4)
        if result == "WIN":    ms[mode]["wins"]   += 1
        elif result == "LOSS": ms[mode]["losses"] += 1

        # Stats por strategy_type ← NUEVO
        sts = self.memory.setdefault(
            "strategy_stats",
            {s: {"wins":0,"losses":0,"total_pnl":0.0,"trades":0}
             for s in VALID_STRATEGY_TYPES}
        )
        sk = strategy_type if strategy_type in VALID_STRATEGY_TYPES else "NORMAL"
        if sk not in sts:
            sts[sk] = {"wins":0,"losses":0,"total_pnl":0.0,"trades":0}
        sts[sk]["trades"]   += 1
        sts[sk]["total_pnl"] = round(sts[sk]["total_pnl"] + (rec.get("pnl_usdt") or 0), 4)
        if result == "WIN":    sts[sk]["wins"]   += 1
        elif result == "LOSS": sts[sk]["losses"] += 1

        # Ajuste cada 5 trades
        closed = [t for t in self.memory["trade_history"] if t.get("result")]
        if len(closed) >= 5 and len(closed) % 5 == 0:
            self._adjust(closed)

    def _adjust(self, closed: List[Dict]) -> None:
        recent    = closed[-30:]
        wins      = [t for t in recent if t["result"] == "WIN"]
        losses    = [t for t in recent if t["result"] == "LOSS"]
        wr        = len(wins) / len(recent) if recent else 0.5

        win_pnls  = [float(t["pnl_usdt"]) for t in wins   if t.get("pnl_usdt") is not None]
        loss_pnls = [abs(float(t["pnl_usdt"])) for t in losses if t.get("pnl_usdt") is not None]
        avg_win   = float(np.mean(win_pnls))  if win_pnls  else 1.0
        avg_loss  = float(np.mean(loss_pnls)) if loss_pnls else 1.0
        rr        = avg_win / avg_loss if avg_loss > 0 else 1.0

        kelly = (wr * rr - (1 - wr)) / rr if rr > 0 else 0
        kelly = max(0.005, min(0.05, kelly))
        self.params["risk_pct_per_trade"] = round(kelly * 100, 2)

        if wr > 0.65 and rr > 1.5:
            self.params["min_score_long"]  = max(2.0, self.params["min_score_long"]  - 0.3)
            self.params["min_score_short"] = max(2.0, self.params["min_score_short"] - 0.3)
        elif wr < 0.40 or rr < 1.0:
            self.params["min_score_long"]  = min(6.0, self.params["min_score_long"]  + 0.5)
            self.params["min_score_short"] = min(6.0, self.params["min_score_short"] + 0.5)

        if wr > 0.60 and rr > 2.0:
            self.params["default_leverage"]   = min(50, self.params["default_leverage"]   + 5)
            self.params["max_open_positions"] = min(8,  self.params["max_open_positions"] + 1)
        elif wr < 0.40:
            self.params["default_leverage"]   = max(5,  self.params["default_leverage"]   - 5)
            self.params["max_open_positions"] = max(2,  self.params["max_open_positions"] - 1)

        if rr < 1.5:
            self.params["tp_atr_mult"] = min(4.0, self.params["tp_atr_mult"] + 0.15)
        elif rr > 3.0:
            self.params["tp_atr_mult"] = max(1.5, self.params["tp_atr_mult"] - 0.10)

        self._save()
        log.info(
            f"[LEARNING] WR={wr:.0%} RR={rr:.2f} Kelly={kelly:.1%} | "
            f"Risk={self.params['risk_pct_per_trade']:.2f}% "
            f"Lev={self.params['default_leverage']}x "
            f"Thresh={self.params['min_score_long']:.1f} "
            f"MaxPos={self.params['max_open_positions']}"
        )

    # ── API pública ────────────────────────────────────────────────────────────

    def get_params(self) -> Dict:
        return dict(self.params)

    def get_symbol_stats(self, symbol: Optional[str] = None) -> Dict:
        s = self.memory.get("symbol_stats", {})
        return s.get(symbol, {}) if symbol else s

    def get_mode_stats(self) -> Dict:
        return self.memory.get("mode_stats", {})

    def get_strategy_stats(self) -> Dict:
        return self.memory.get("strategy_stats", {})

    def get_performance_summary(self) -> Dict:
        history = self.memory.get("trade_history", [])
        closed  = [t for t in history if t.get("result")]
        wins    = [t for t in closed  if t["result"] == "WIN"]
        losses  = [t for t in closed  if t["result"] == "LOSS"]
        pnls    = [float(t["pnl_usdt"]) for t in closed if t.get("pnl_usdt") is not None]
        wp      = [float(t["pnl_usdt"]) for t in wins   if t.get("pnl_usdt") is not None]
        lp      = [float(t["pnl_usdt"]) for t in losses if t.get("pnl_usdt") is not None]
        total   = len(closed)

        # Breakdown por strategy_type
        strat_summary: Dict = {}
        for s in VALID_STRATEGY_TYPES:
            st = self.memory.get("strategy_stats", {}).get(s, {})
            t  = st.get("trades", 0)
            w  = st.get("wins",   0)
            strat_summary[s] = {
                "trades":    t,
                "wins":      w,
                "losses":    st.get("losses", 0),
                "win_rate":  round(w / t * 100, 1) if t > 0 else 0.0,
                "total_pnl": round(st.get("total_pnl", 0.0), 2),
            }

        return {
            "total_trades":   total,
            "open_trades":    len(history) - total,
            "wins":           len(wins),
            "losses":         len(losses),
            "win_rate":       round(len(wins) / total * 100, 1) if total > 0 else 0.0,
            "total_pnl":      round(sum(pnls), 2) if pnls else 0.0,
            "avg_win":        round(float(np.mean(wp)), 2) if wp else 0.0,
            "avg_loss":       round(float(np.mean(lp)), 2) if lp else 0.0,
            "best_trade":     round(max(pnls), 2) if pnls else 0.0,
            "worst_trade":    round(min(pnls), 2) if pnls else 0.0,
            "strategy_stats": strat_summary,
            "current_params": self.params,
        }

    def should_trade_symbol(self, symbol: str) -> Tuple[bool, str]:
        s = self.get_symbol_stats(symbol)
        if not s or s.get("trades", 0) < 5:
            return True, "sin historial"
        wr = s["wins"] / s["trades"]
        if wr < 0.20 and s["trades"] >= 8:
            return False, RC.fmt(
                RC.TRADE_BLOCKED_SYMBOL_WR,
                symbol=symbol, wr=f"{wr:.0%}", trades=s["trades"]
            )
        return True, f"WR {wr:.0%}"

    def calculate_position_size(
        self,
        balance:  float,
        price:    float,
        atr:      float,
        leverage: int,
    ) -> float:
        risk_pct  = self.params.get("risk_pct_per_trade", 2.0) / 100
        sl_mult   = self.params.get("sl_atr_mult", 1.0)
        risk_usdt = balance * risk_pct
        if price <= 0:
            return 0.0
        if atr > 0 and sl_mult > 0:
            qty = risk_usdt / (atr * sl_mult)
        else:
            qty = (risk_usdt * leverage) / price
        max_qty = (balance * 0.08 * leverage) / price
        return round(min(qty, max_qty), 4)
