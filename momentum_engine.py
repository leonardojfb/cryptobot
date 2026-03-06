"""
momentum_engine.py
Minimal momentum trading engine used by the provided `main.py`.

This implementation is intentionally lightweight: it computes a simple
momentum score from recent klines and opens/monitors positions via the
BybitClient async wrappers (`async_*` methods added to `bybit_client.py`).
"""
import asyncio, logging, time
from dataclasses import dataclass
from typing import Dict, Callable, Any

log = logging.getLogger("momentum")

@dataclass
class ActivePosition:
    symbol: str
    entry_price: float
    size: float
    side: str  # 'long'|'short'
    entry_time: float
    confidence: float
    momentum_score: float
    initial_sl: float
    current_sl: float


class MomentumTrader:
    def __init__(self, client):
        self.client = client
        self.active_positions: Dict[str, ActivePosition] = {}
        # notify_telegram may be a synchronous function or an async coroutine function.
        # Use a loose Any return type to accept both callables.
        self.notify_telegram: Callable[[str], Any] = lambda *_: None

    async def _generate_momentum_signal(self, symbol: str):
        # Simple momentum: percent change from 5 bars ago to last
        try:
            klines = await self.client.async_get_klines(symbol, '1', limit=16)
            closes = [float(r['close']) for r in klines[-16:]] if klines else []
            if len(closes) < 6:
                return None
            prev = closes[-6]
            last = closes[-1]
            score = (last - prev) / prev * 100 if prev else 0
            confidence = min(max(abs(score) * 2, 10), 100)
            sig = 'LONG' if score > 0 else 'SHORT'
            return {'symbol': symbol, 'score': abs(score), 'confidence': confidence, 'signal': sig}
        except Exception as e:
            log.debug(f"momentum signal error {symbol}: {e}")
            return None

    async def _get_price(self, symbol: str) -> float:
        t = await self.client.async_get_ticker(symbol)
        try:
            return float(t.get('markPrice') or t.get('lastPrice') or 0)
        except Exception:
            return 0.0

    def _calculate_r(self, pos: ActivePosition, current_price: float) -> float:
        # R = (current - entry) / (entry - sl) for long; inverse for short
        entry = pos.entry_price
        sl = pos.current_sl or pos.initial_sl or entry
        if pos.side == 'long':
            denom = entry - sl
            if denom == 0: return 0
            return (current_price - entry) / denom
        else:
            denom = sl - entry
            if denom == 0: return 0
            return (entry - current_price) / denom

    def _estimate_pnl(self, pos: ActivePosition, size: float, price: float) -> float:
        # Simplified PnL (USDT): (price - entry) * size for long
        mult = 1 if pos.side == 'long' else -1
        return (price - pos.entry_price) * size * mult

    async def _open_momentum_position(self, symbol: str, signal: dict):
        # Basic sizing: fixed small size for safety
        balance = await self.client.async_get_balance()
        size = max(0.001, (balance * 0.01))
        side = 'Buy' if signal['signal'] == 'LONG' else 'Sell'
        try:
            resp = await self.client.async_create_order(symbol=symbol, side=side, qty=size,
                                                        order_type='Market', reduce_only=False)
            # If resp is dict from API keep it; some test envs return body
            entry = await self._get_price(symbol)
            pos = ActivePosition(symbol=symbol, entry_price=entry, size=size,
                                 side='long' if side=='Buy' else 'short', entry_time=time.time(),
                                 confidence=signal.get('confidence', 50), momentum_score=signal.get('score',0),
                                 initial_sl=entry*0.98, current_sl=entry*0.98)
            self.active_positions[symbol] = pos
            # Start monitoring in background
            asyncio.create_task(self.start_monitoring(symbol, pos))
            log.info(f"🚀 {symbol}: {signal.get('signal').lower()} abierto | Price: {entry:.4f} | Size: {size}")
            await asyncio.sleep(0.1)
            return resp
        except Exception as e:
            log.error(f"Error opening momentum position {symbol}: {e}")
            return None

    async def start_monitoring(self, symbol: str, pos: ActivePosition):
        log.info(f"🔥 {symbol}: Monitoreo MOMENTUM iniciado | Entry: {pos.entry_price}")
        try:
            while symbol in self.active_positions:
                price = await self._get_price(symbol)
                r = self._calculate_r(pos, price)
                # trailing example: if r > 2.0 move SL to breakeven
                if r >= 2.0:
                    new_sl = pos.entry_price
                    if new_sl != pos.current_sl:
                        await self.client.async_set_stop_loss(symbol, new_sl)
                        pos.current_sl = new_sl
                        log.info(f"🛡️ {symbol}: SL actualizado a {new_sl:.4f} (trail)")
                await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Monitor error {symbol}: {e}")

    async def _close_position_cleanup(self, symbol: str, reason: str = 'manual'):
        try:
            if symbol in self.active_positions:
                del self.active_positions[symbol]
            msg = f"Closed {symbol} ({reason})"
            try:
                res = self.notify_telegram(msg)
                # If the notifier returned a coroutine, await it.
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        except Exception as e:
            log.error(f"close cleanup error {symbol}: {e}")
