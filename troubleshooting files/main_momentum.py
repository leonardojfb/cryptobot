import asyncio
import logging
import os

from bybit_client import BybitClient
from momentum_trader import MomentumTrader

logger = logging.getLogger("main_momentum")


async def main():
    # Inicializar cliente
    api_key = os.getenv('BYBIT_API_KEY', '')
    api_secret = os.getenv('BYBIT_API_SECRET', '')
    paper = os.getenv('PAPER_TRADING', 'true').lower() in ('1','true','yes')
    client = BybitClient(api_key, api_secret, paper=paper)

    trader = MomentumTrader(client)
    
    # Task 1: Monitoreo de posiciones activas (alta prioridad)
    # Esto ya corre en background por cada posición
    
    # Task 2: Scan constante de nuevas oportunidades
    async def scanner_loop():
        while True:
            try:
                await trader.scan_for_new_entries()
                await asyncio.sleep(3)  # Scan cada 3 segundos
            except Exception as e:
                logger.error(f"Scanner error: {e}")
                await asyncio.sleep(10)
    
    # Task 3: Sincronización de estado
    async def sync_loop():
        while True:
            try:
                # Verificar que todas las posiciones monitoreadas existan
                exchange_positions = await client.async_get_positions()
                exchange_symbols = {p['symbol'] for p in exchange_positions if float(p.get('size', 0)) > 0}
                
                # Limpiar posiciones muertas
                for symbol in list(trader.active_positions.keys()):
                    if symbol not in exchange_symbols:
                        logger.warning(f"👻 {symbol}: Posición fantasma detectada, limpiando")
                        await trader._close_position_cleanup(symbol, 'ghost')
                
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"Sync error: {e}")
                await asyncio.sleep(30)
    
    # Ejecutar todo en paralelo
    await asyncio.gather(
        scanner_loop(),
        sync_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())