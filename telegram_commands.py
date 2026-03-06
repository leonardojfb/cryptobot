import asyncio
import time
import logging
from typing import Optional

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, Application

logger = logging.getLogger("telegram_commands")


class TelegramCommands:
    def __init__(self, momentum_trader, client):
        self.trader = momentum_trader
        self.client = client

    async def register_commands(self, app: Application):
        """Register command handlers on a python-telegram-bot `Application` instance."""
        app.add_handler(CommandHandler(["pos", "posiciones"], self.cmd_positions))
        app.add_handler(CommandHandler(["profit", "pnl"], self.cmd_profit))
        app.add_handler(CommandHandler(["status"], self.cmd_status))

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler for /pos and /posiciones - lists open positions."""
        message = update.message
        if not self.trader:
            if message:
                await message.reply_text("Momentum trader no inicializado.")
            return

        lines = ["📊 POSICIONES ABIERTAS", "═════════════════════════"]
        unrealized_total = 0.0

        for pos in self.trader.active_positions.values():
            symbol = pos.symbol
            try:
                ticker = await self.client.async_get_ticker(symbol)
                price = float(ticker.get('lastPrice', pos.entry_price))
            except Exception:
                price = pos.entry_price

            r = self.trader._calculate_r(pos, price)
            pnl = self.trader._estimate_pnl(pos, pos.size, price)
            unrealized_total += pnl

            lines.append(f"*{symbol}* {'🟢 LONG' if pos.side == 'long' else '🔴 SHORT'}")
            lines.append(f"Entry: `{pos.entry_price:.4f}` | Now: `{price:.4f}`")
            lines.append(f"Size: `{pos.size:.4f}` | R: `{r:.2f}`")
            lines.append(f"PnL: `{pnl:+.2f}` USDT")
            lines.append(f"TPs hit: {len(pos.partial_tp_done)}/{len(self.trader.partial_tp_levels)}")
            duration = int(time.time() - pos.entry_time)
            mins = duration // 60
            secs = duration % 60
            lines.append(f"⏱️ Duration: {mins}m {secs}s")
            lines.append("────────────────────")

        lines.append(f"\n💵 Unrealized Total: `{unrealized_total:+.2f}` USDT")

        text = "\n".join(lines) if len(lines) > 2 else "No hay posiciones abiertas."
        if message:
            await message.reply_markdown_v2(text)

    async def cmd_profit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler for /profit and /pnl - shows realized and unrealized PnL and stats."""
        message = update.message
        if not self.trader:
            if message:
                await message.reply_text("Momentum trader no inicializado.")
            return

        # Unrealized
        unrealized_lines = []
        unrealized_total = 0.0
        for pos in self.trader.active_positions.values():
            try:
                ticker = await self.client.async_get_ticker(pos.symbol)
                price = float(ticker.get('lastPrice', pos.entry_price))
            except Exception:
                price = pos.entry_price
            pnl = self.trader._estimate_pnl(pos, pos.size, price)
            unrealized_total += pnl
            unrealized_lines.append((pos.symbol, pnl))

        # Realized
        total_realized = float(getattr(self.trader, 'total_realized_pnl', 0.0))
        closed_trades = getattr(self.trader, 'closed_trades', [])

        # Realized today (since midnight local)
        now = time.time()
        local_time = time.localtime(now)
        midnight = time.mktime((local_time.tm_year, local_time.tm_mon, local_time.tm_mday,
                                0, 0, 0, local_time.tm_wday, local_time.tm_yday, local_time.tm_isdst))
        realized_today = sum(t['pnl'] for t in closed_trades if t.get('time', 0) >= midnight)

        lines = ["💰 REPORTE DE PROFIT", "═══════════════════════════", "\n📊 NO REALIZADO (Open)"]
        for sym, pnl in unrealized_lines:
            lines.append(f"{sym}: `{pnl:+.2f}` USDT")
        lines.append(f"\n💵 Subtotal: `{unrealized_total:+.2f}` USDT")
        lines.append("\n✅ REALIZADO HOY")
        lines.append(f"`{realized_today:+.2f}` USDT")
        lines.append("\n🏆 REALIZADO TOTAL (Histórico)")
        lines.append(f"`{total_realized:+.2f}` USDT")

        # Stats
        total_trades = getattr(self.trader, 'total_trades', 0)
        wins = getattr(self.trader, 'winning_trades', 0)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
        best = getattr(self.trader, 'best_trade_pnl', 0.0)
        worst = getattr(self.trader, 'worst_trade_pnl', 0.0)
        balance = getattr(self.trader, 'get_balance', lambda: 0)()

        lines.append("\n═══════════════════════════")
        lines.append(f"💎 PROFIT TOTAL: `{(total_realized + unrealized_total):+.2f}` USDT")
        lines.append(f"📈 Retorno: `N/A`")
        lines.append("\n📉 ESTADÍSTICAS")
        lines.append(f"Trades: `{total_trades}` | Wins: `{wins}`")
        lines.append(f"Win Rate: `{win_rate:.1f}%`")
        lines.append(f"Mejor: `{best:+.2f}` | Peor: `{worst:+.2f}`")
        lines.append(f"\n💳 Balance: `{balance:+.2f}` USDT")

        if message:
            await message.reply_markdown_v2("\n".join(lines))

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Simple status of the bot and trader"""
        message = update.message
        status = []
        status.append("🤖 Bot status: running")
        active = len(getattr(self.trader, 'active_positions', {})) if self.trader else 0
        status.append(f"Active positions: {active}")
        if message:
            await message.reply_text("\n".join(status))
