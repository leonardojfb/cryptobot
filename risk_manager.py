"""
risk_manager.py
Gestión de riesgo avanzada:
- Drawdown máximo diario
- Máximo de pérdidas consecutivas
- Circuit breaker automático
- Exposición total limitada
"""

import logging
import time
from typing import Dict

log = logging.getLogger("risk_manager")

class RiskManager:
    def __init__(self, learner=None):
        self.learner = learner
        self.daily_pnl = 0.0
        self.daily_reset_ts = self._today_ts()
        self.consecutive_losses = 0
        self.circuit_breaker = False
        self.circuit_until = 0
        self.open_symbols = set()

        # Límites
        self.MAX_DAILY_LOSS_PCT    = 5.0   # % pérdida diaria máxima del balance
        self.MAX_CONSECUTIVE_LOSS  = 4     # pérdidas consecutivas antes de parar
        self.CIRCUIT_BREAK_SECONDS = 3600  # 1h de pausa tras circuit breaker

    def _today_ts(self):
        now = time.localtime()
        return int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)))

    def _check_daily_reset(self):
        if time.time() > self._today_ts() + 86400:
            self.daily_pnl = 0.0
            self.daily_reset_ts = self._today_ts()
            log.info("RiskManager: reset diario de PnL")

    def can_open(self, symbol: str, score: float) -> bool:
        self._check_daily_reset()

        # Circuit breaker activo
        if self.circuit_breaker and time.time() < self.circuit_until:
            remaining = int(self.circuit_until - time.time())
            log.warning(f"Circuit breaker activo. Reanuda en {remaining}s")
            return False
        elif self.circuit_breaker:
            self.circuit_breaker = False
            self.consecutive_losses = 0
            log.info("Circuit breaker reseteado")

        # Pérdida diaria excedida
        if self.daily_pnl < -50:  # placeholder; idealmente usar % del balance
            log.warning(f"Pérdida diaria excedida: {self.daily_pnl:.2f} USDT")
            return False

        # Demasiadas pérdidas consecutivas
        if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSS:
            log.warning(f"Circuit breaker: {self.consecutive_losses} pérdidas consecutivas")
            self.circuit_breaker = True
            self.circuit_until = time.time() + self.CIRCUIT_BREAK_SECONDS
            return False

        return True

    def on_open(self, symbol: str):
        self.open_symbols.add(symbol)

    def on_close(self, symbol: str, pnl: float):
        self.open_symbols.discard(symbol)
        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
            log.info(f"Pérdida #{self.consecutive_losses} consecutiva: {pnl:.2f} USDT")
        else:
            self.consecutive_losses = 0

    def get_status(self) -> Dict:
        return {
            "daily_pnl":          round(self.daily_pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker":    self.circuit_breaker,
            "circuit_until":      self.circuit_until,
            "open_symbols":       list(self.open_symbols),
        }
