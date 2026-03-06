# telegram_commands.py - Agregar a tu bot existente

from telegram import Update
from telegram.ext import ContextTypes
import asyncio
import time
import logging

logger = logging.getLogger("telegram_command")

class TelegramCommands:
    def __init__(self, momentum_trader, bybit_client):
        self.trader = momentum_trader
        self.client = bybit_client
    
    async def register_commands(self, application):
        """Registra los comandos en el bot"""
        from telegram.ext import CommandHandler
        
        application.add_handler(CommandHandler("pos", self.cmd_positions))
        application.add_handler(CommandHandler("posiciones", self.cmd_positions))
        application.add_handler(CommandHandler("profit", self.cmd_profit))
        application.add_handler(CommandHandler("pnl", self.cmd_profit))
        application.add_handler(CommandHandler("status", self.cmd_status))
    
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /pos - Lista posiciones abiertas con métricas de momentum
        """
        try:
            # Obtener posiciones del momentum trader
            active_positions = self.trader.active_positions
            
            # También consultar exchange por si hay diferencias
            exchange_positions = await self.client.get_positions()
            
            if not active_positions and not exchange_positions:
                msg = update.message
                if msg:
                    await msg.reply_text(
                        "📭 *No hay posiciones abiertas*\n"
                        "Capital disponible para trading",
                        parse_mode='Markdown'
                    )
                return
            
            message = "📊 *POSICIONES ABIERTAS*\n"
            message += "═" * 25 + "\n\n"
            
            total_unrealized = 0
            
            for symbol, pos in active_positions.items():
                # Obtener precio actual
                current_price = await self.trader._get_price(symbol)
                
                # Calcular métricas
                entry = pos.entry_price
                size = pos.size
                side = pos.side
                side_emoji = "🟢 LONG" if side == 'long' else "🔴 SHORT"
                
                # PnL no realizado
                if side == 'long':
                    pnl_pct = ((current_price - entry) / entry) * 100
                    pnl_usdt = (current_price - entry) * size
                else:
                    pnl_pct = ((entry - current_price) / entry) * 100
                    pnl_usdt = (entry - current_price) * size
                
                total_unrealized += pnl_usdt
                
                # Calcular R actual
                r = self.trader._calculate_r(pos, current_price)
                
                # Momentum actual
                momentum = await self.trader._calculate_live_momentum(symbol)
                mom_emoji = "🚀" if abs(momentum) > 0.5 else "⚡" if abs(momentum) > 0.3 else "📉"
                
                # Estado del trailing
                sl_status = "🔒 BE" if pos.current_sl == entry else f"🛡️ {pos.current_sl:.4f}"
                if r >= self.trader.trailing_activation:
                    sl_status = "🏃 TRAILING"
                
                message += (
                    f"*{symbol}* {side_emoji}\n"
                    f"📍 Entry: `{entry:.4f}` | Now: `{current_price:.4f}`\n"
                    f"📦 Size: `{size:.4f}` | R: `{r:.2f}`\n"
                    f"💰 PnL: `{pnl_usdt:+.2f}` USDT (`{pnl_pct:+.2f}%`)\n"
                    f"{mom_emoji} Momentum: `{momentum:.2f}`\n"
                    f"🛡️ SL: {sl_status}\n"
                    f"TPs hit: {len(pos.partial_tp_done)}/4\n"
                    f"⏱️ Duration: {self._format_duration(time.time() - pos.entry_time)}\n"
                    f"─" * 20 + "\n\n"
                )
            
            # Resumen al final
            message += f"\n💵 *Unrealized Total:* `{total_unrealized:+.2f}` USDT"
            
            msg = update.message
            if msg:
                await msg.reply_text(message, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error en cmd_positions: {e}")
            msg = update.message
            if msg:
                await msg.reply_text(f"❌ Error: {str(e)}")
    
    async def cmd_profit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /profit - Muestra breakdown completo de PnL
        """
        try:
            # 1. PnL NO REALIZADO (posiciones abiertas)
            unrealized_pnl = 0
            unrealized_by_symbol = {}
            
            for symbol, pos in self.trader.active_positions.items():
                current_price = await self.trader._get_price(symbol)
                
                if pos.side == 'long':
                    pnl = (current_price - pos.entry_price) * pos.size
                else:
                    pnl = (pos.entry_price - current_price) * pos.size
                
                unrealized_pnl += pnl
                unrealized_by_symbol[symbol] = pnl
            
            # 2. PnL REALIZADO (trades cerrados hoy)
            realized_today = await self._get_realized_pnl_today()
            
            # 3. PnL REALIZADO (histórico total - desde inicio del bot)
            total_realized = getattr(self.trader, 'total_realized_pnl', 0)
            
            # 4. Balance y métricas
            balance = await self.client.get_balance()
            
            # Calcular estadísticas
            total_pnl = total_realized + unrealized_pnl
            total_trades = getattr(self.trader, 'total_trades', 0)
            winning_trades = getattr(self.trader, 'winning_trades', 0)
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
            
            # Mejor/peor trade
            best_trade = getattr(self.trader, 'best_trade_pnl', 0)
            worst_trade = getattr(self.trader, 'worst_trade_pnl', 0)
            
            message = "💰 *REPORTE DE PROFIT*\n"
            message += "═" * 25 + "\n\n"
            
            # Sección NO REALIZADO
            message += "📊 *NO REALIZADO (Open)*\n"
            if unrealized_by_symbol:
                for sym, pnl in sorted(unrealized_by_symbol.items(), key=lambda x: abs(x[1]), reverse=True):
                    emoji = "🟢" if pnl > 0 else "🔴"
                    message += f"{emoji} `{sym}`: `{pnl:+.2f}` USDT\n"
                message += f"\n💵 *Subtotal:* `{unrealized_pnl:+.2f}` USDT\n\n"
            else:
                message += "Sin posiciones abiertas\n\n"
            
            # Sección REALIZADO HOY
            message += "✅ *REALIZADO HOY*\n"
            message += f"`{realized_today:+.2f}` USDT\n\n"
            
            # Sección REALIZADO TOTAL
            message += "🏆 *REALIZADO TOTAL (Histórico)*\n"
            message += f"`{total_realized:+.2f}` USDT\n\n"
            
            # Sección TOTAL
            message += "═" * 25 + "\n"
            message += f"💎 *PROFIT TOTAL:* `{total_pnl:+.2f}` USDT\n"
            message += f"📈 Retorno: `{(total_pnl/balance*100):+.2f}%`\n\n"
            
            # Estadísticas
            message += "📉 *ESTADÍSTICAS*\n"
            message += f"Trades: `{total_trades}` | Wins: `{winning_trades}`\n"
            message += f"Win Rate: `{win_rate:.1f}%`\n"
            message += f"Mejor: `{best_trade:+.2f}` | Peor: `{worst_trade:+.2f}`\n\n"
            
            # Balance actual
            message += f"💳 Balance: `{balance:.2f}` USDT"
            
            msg = update.message
            if msg:
                await msg.reply_text(message, parse_mode='Markdown')
            
            # Gráfico simple de barras (opcional - ASCII art)
            if total_trades > 0:
                chart = self._generate_pnl_chart(total_realized, unrealized_pnl)
                msg = update.message
                if msg:
                    await msg.reply_text(f"```{chart}```", parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error en cmd_profit: {e}")
            msg = update.message
            if msg:
                await msg.reply_text(f"❌ Error: {str(e)}")
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /status - Estado general del sistema
        """
        try:
            # Info del sistema
            active_count = len(self.trader.active_positions)
            monitoring_tasks = len([t for t in self.trader.monitoring_tasks.values() if not t.done()])
            
            # Uptime
            uptime = getattr(self.trader, 'start_time', time.time())
            uptime_str = self._format_duration(time.time() - uptime)
            
            # API status
            try:
                await self.client.get_balance()
                api_status = "🟢 Online"
            except:
                api_status = "🔴 Error"
            
            message = (
                "🤖 *ESTADO DEL BOT*\n"
                "═" * 20 + "\n\n"
                f"🟢 Sistema: `Operativo`\n"
                f"⏱️ Uptime: `{uptime_str}`\n"
                f"📡 API Bybit: {api_status}\n\n"
                f"📊 Posiciones activas: `{active_count}/5`\n"
                f"👁️ Tasks monitoreo: `{monitoring_tasks}`\n\n"
                f"🔄 Scan interval: `{self.trader.scan_interval}s`\n"
                f"🎯 Min score: `{self.trader.min_score}`\n"
                f"💪 Min confidence: `{self.trader.min_confidence}%`"
            )
            
            msg = update.message
            if msg:
                await msg.reply_text(message, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error en cmd_status: {e}")
            msg = update.message
            if msg:
                await msg.reply_text(f"❌ Error: {str(e)}")
    
    async def _get_realized_pnl_today(self) -> float:
        """Obtiene PnL realizado de hoy desde Bybit"""
        try:
            # Bybit API v5 - closed PnL
            response = await self.client.session.get(
                f"{self.client.base_url}/v5/position/closed-pnl",
                params={
                    'category': 'linear',
                    'settleCoin': 'USDT',
                    'limit': 50
                }
            )
            data = await response.json()
            
            from datetime import datetime
            today = datetime.now().date()
            total = 0
            
            for trade in data.get('result', {}).get('list', []):
                # Filtrar solo trades de hoy
                trade_time = datetime.fromtimestamp(int(trade['updatedTime']) / 1000).date()
                if trade_time == today:
                    total += float(trade.get('closedPnl', 0))
            
            return total
            
        except Exception as e:
            logger.error(f"Error getting realized PnL: {e}")
            return 0
    
    def _format_duration(self, seconds: float) -> str:
        """Formatea duración en formato legible"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"
    
    def _generate_pnl_chart(self, realized: float, unrealized: float) -> str:
        """Genera gráfico ASCII simple de PnL"""
        total = abs(realized) + abs(unrealized)
        if total == 0:
            return "Sin datos"
        
        width = 20
        r_width = int(abs(realized) / total * width)
        u_width = int(abs(unrealized) / total * width)
        
        r_bar = "█" * r_width
        u_bar = "▓" * u_width
        
        r_sign = "+" if realized >= 0 else "-"
        u_sign = "+" if unrealized >= 0 else "-"
        
        chart = (
            f"Realized  {r_sign}{r_bar} {realized:+.2f}\n"
            f"Unrealized{u_sign}{u_bar} {unrealized:+.2f}"
        )
        return chart