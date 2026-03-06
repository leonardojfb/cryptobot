"""
main_bot.py v3 — Bot autónomo Bybit Paper Trading
"""

import logging, os, sys, threading, time
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")


def main():
    log.info("=" * 60)
    log.info("  BOT AUTÓNOMO — BYBIT PAPER TRADING  v3")
    log.info("=" * 60)

    api_key    = os.getenv("BYBIT_API_KEY",    "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
    paper      = os.getenv("PAPER_TRADING", "true").lower() in ("1","true","yes")

    if not api_key or not api_secret:
        log.error(
            "❌  BYBIT_API_KEY / BYBIT_API_SECRET vacíos en .env\n\n"
            "   Para Paper Trading:\n"
            "   1. bybit.com → activa 'Trading de Prueba' (banner naranja)\n"
            "   2. Mi Perfil → Gestión de API → Crear clave API\n"
            "   3. Pega esas claves en .env\n"
            "   ⚠️  Las claves de cuenta real NO sirven para el endpoint demo."
        )
        sys.exit(1)

    mode_str = "PAPER (api-demo.bybit.com)" if paper else "⚠️  REAL MONEY"
    log.info(f"Modo: {mode_str}")

    from bot_autonomous import AutonomousBot
    bot = AutonomousBot()

    # ── Verificar datos de mercado (siempre público, no necesita auth) ─────────
    price = bot.client.get_mark_price("BTCUSDT")
    if price > 0:
        log.info(f"✅ Datos de mercado OK — BTC: {price:,.2f} USDT")
    else:
        log.warning("⚠️  Sin datos de mercado")

    # ── Verificar autenticación ────────────────────────────────────────────────
    auth_ok = bot.client.test_connection()

    if auth_ok:
        balance = bot.client.get_usdt_balance()
        log.info(f"✅ Auth OK — Balance USDT disponible: {balance:,.2f}")
    else:
        log.error(
            "\n" + "═" * 60 + "\n"
            "  PROBLEMA DE AUTENTICACIÓN\n\n"
            "  Si estás en Paper Trading:\n"
            "  → Las claves API deben crearse desde el panel DEMO de Bybit\n"
            "    (con el banner 'Trading de Prueba' activo)\n"
            "  → Las claves de tu cuenta real NO funcionan en api-demo.bybit.com\n"
            + "═" * 60
        )
        log.warning("Continuando en modo lectura (análisis sin órdenes)...")
        auth_ok = False

    # ── Telegram ───────────────────────────────────────────────────────────────
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if tg_token:
        try:
            from tg_controller import run_telegram_bot
            threading.Thread(
                target=run_telegram_bot, args=(bot,),
                daemon=True, name="telegram"
            ).start()
            log.info("✅ Telegram controller iniciado")
        except Exception as e:
            log.warning(f"Telegram no disponible: {e}")
    else:
        log.info("ℹ️  Telegram no configurado")

    # ── Iniciar bot ────────────────────────────────────────────────────────────
    bot.start()

    watchlist = os.getenv("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
    log.info(f"🚀 Bot activo | Watchlist: {watchlist}")
    log.info(f"   TFs analizados: 1m 3m 5m 15m 30m 1h 2h 4h 6h 12h 1D 1W (12 TFs)")
    log.info(f"   Scan: {os.getenv('SCAN_INTERVAL_SEC','30')}s | "
             f"Monitor: {os.getenv('MONITOR_INTERVAL_SEC','10')}s")

    try:
        while True:
            s = bot.get_status()
            p = s.get("performance", {})
            bal = s.get("balance_usdt", 0)
            auth_icon = "✅" if auth_ok else "❌API"
            log.info(
                f"{auth_icon} | Bal: {bal:.2f} USDT | "
                f"Pos: {s['open_positions']} | "
                f"Trades: {p.get('total_trades',0)} | "
                f"WR: {p.get('win_rate',0):.1f}% | "
                f"PnL: {p.get('total_pnl',0):+.2f} USDT"
            )
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("⛔ Deteniendo...")
        bot.stop()
        log.info("Bot detenido.")

if __name__ == "__main__":
    main()
