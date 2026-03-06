import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Optional, Type

from bybit_client import BybitClient

# The module exposes `TelegramBotClass` which is either a class that
# implements an async `send(message: str)` method, or `None` if the
# local notifier couldn't be imported. This avoids redeclaring a name
# while keeping backward-compatible usage `TelegramBotClass()`.
TelegramBotClass: Optional[Type[Any]] = None
try:
    from bot_autonomous import TelegramNotifier as _TelegramNotifier

    class _TelegramBotImpl:
        def __init__(self):
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            self._impl = _TelegramNotifier(token, chat)

        async def send(self, message: str):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._impl.send, message)

    TelegramBotClass = _TelegramBotImpl
except Exception:
    TelegramBotClass = None

from momentum_engine import MomentumTrader, ActivePosition
from config import TRADING_SYMBOLS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s'
)
logger = logging.getLogger('main')


class TradingSystem:
    def __init__(self):
        api_key = os.getenv('BYBIT_API_KEY', '')
        api_secret = os.getenv('BYBIT_API_SECRET', '')
        paper = os.getenv('PAPER_TRADING', 'true').lower() in ('1','true','yes')
        self.client = BybitClient(api_key, api_secret, paper=paper)
        self.telegram = TelegramBotClass() if TelegramBotClass else None
        self.momentum_trader = MomentumTrader(self.client)

    async def initialize(self):
        logger.info("🚀 Inicializando sistema de MOMENTUM TRADING...")
        # no-op connect (sync client wrappers handle requests)
        self.momentum_trader.notify_telegram = self.send_telegram
        await self.sync_existing_positions()
        logger.info("✅ Sistema listo")

    async def sync_existing_positions(self):
        positions = await self.client.async_get_positions()
        for pos in positions or []:
            size = float(pos.get('size', 0))
            if size == 0:
                continue
            symbol = pos['symbol']
            side = 'long' if pos.get('side') == 'Buy' else 'short'
            active_pos = ActivePosition(
                symbol=symbol,
                entry_price=float(pos.get('entryPrice', 0)) or 0,
                size=size,
                side=side,
                entry_time=datetime.now().timestamp(),
                confidence=50,
                momentum_score=0,
                initial_sl=float(pos.get('stopLoss', 0)) or 0,
                current_sl=float(pos.get('stopLoss', 0)) or 0,
            )
            await self.momentum_trader.start_monitoring(symbol, active_pos)
            logger.info(f"📡 {symbol}: Monitoreo iniciado para posición existente")

    async def send_telegram(self, message: str):
        try:
            if self.telegram:
                await self.telegram.send(message)
        except Exception as e:
            logger.warning(f"Telegram error: {e}")

    async def run(self):
        await self.initialize()
        tasks = [self.momentum_scanner(), self.heartbeat(), self.risk_manager()]
        await asyncio.gather(*tasks)

    async def momentum_scanner(self):
        while True:
            try:
                for symbol in TRADING_SYMBOLS:
                    if symbol in self.momentum_trader.active_positions:
                        continue
                    signal = await self.momentum_trader._generate_momentum_signal(symbol)
                    if signal and signal['score'] > 2.0:
                        logger.info(f"🎯 {symbol}: Señal MOMENTUM | Score: {signal['score']:.2f} | Conf: {signal['confidence']:.0f}%")
                        if len(self.momentum_trader.active_positions) >= 5:
                            await self.evaluate_rotation(symbol, signal)
                        else:
                            await self.momentum_trader._open_momentum_position(symbol, signal)
                    await asyncio.sleep(0.5)
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Scanner error: {e}")
                await asyncio.sleep(5)

    async def evaluate_rotation(self, new_symbol: str, new_signal: dict):
        worst_symbol = None
        worst_score = float('inf')
        for symbol, pos in self.momentum_trader.active_positions.items():
            current_price = await self.momentum_trader._get_price(symbol)
            current_r = self.momentum_trader._calculate_r(pos, current_price)
            position_score = current_r * pos.confidence / 100
            if position_score < worst_score:
                worst_score = position_score
                worst_symbol = symbol
        if worst_symbol and new_signal['score'] > worst_score * 1.5:
            logger.info(f"🔄 ROTACIÓN: Cerrando {worst_symbol} por {new_symbol}")
            await self.close_position_immediately(worst_symbol)
            await asyncio.sleep(1)
            await self.momentum_trader._open_momentum_position(new_symbol, new_signal)

    async def close_position_immediately(self, symbol: str):
        try:
            pos = self.momentum_trader.active_positions.get(symbol)
            if not pos:
                return
            await self.client.async_create_order(symbol=symbol,
                                                 side='Sell' if pos.side=='long' else 'Buy',
                                                 qty=pos.size, order_type='Market', reduce_only=True)
            await self.momentum_trader._close_position_cleanup(symbol, 'rotation')
        except Exception as e:
            logger.error(f"Error cerrando {symbol}: {e}")

    async def heartbeat(self):
        while True:
            await asyncio.sleep(60)
            active = len(self.momentum_trader.active_positions)
            balance = await self.client.async_get_balance()
            status_msg = f"💓 HEARTBEAT | Balance: {balance:.2f} USDT | Posiciones activas: {active}"
            logger.info(status_msg)
            for symbol, pos in list(self.momentum_trader.active_positions.items()):
                try:
                    price = await self.momentum_trader._get_price(symbol)
                    r = self.momentum_trader._calculate_r(pos, price)
                    pnl = self.momentum_trader._estimate_pnl(pos, pos.size, price)
                    logger.info(f"   📊 {symbol}: R={r:.2f} | PnL: {pnl:+.2f} | Size: {pos.size:.4f} | SL: {pos.current_sl:.4f}")
                except Exception:
                    pass

    async def risk_manager(self):
        while True:
            try:
                await asyncio.sleep(30)
                # simple risk checks
                if len(self.momentum_trader.active_positions) >= 5:
                    losing_positions = []
                    for symbol, pos in self.momentum_trader.active_positions.items():
                        price = await self.momentum_trader._get_price(symbol)
                        r = self.momentum_trader._calculate_r(pos, price)
                        if r < 0:
                            losing_positions.append((symbol, r))
                    if len(losing_positions) >= 4:
                        worst = min(losing_positions, key=lambda x: x[1])
                        logger.warning(f"🛡️ RISK: Cerrando peor posición {worst[0]}")
                        await self.close_position_immediately(worst[0])
            except Exception as e:
                logger.error(f"Risk manager error: {e}")


async def main():
    system = TradingSystem()
    try:
        await system.run()
    except KeyboardInterrupt:
        logger.info("🛑 Deteniendo sistema...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == '__main__':
    asyncio.run(main())
