# momentum_trader.py  v2 — Integrado con analysis_engine_bybit
# Corrige: klines dict vs list, get_balance() real, TP negativo,
# PostOnly en market orders, integración con 13 indicadores

import asyncio
import numpy as np
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum
import time

logger = logging.getLogger("momentum_trader")


class OrderStatus(Enum):
    PENDING  = "pending"
    PARTIAL  = "partial"
    FILLED   = "filled"
    TP_HIT   = "tp_hit"
    SL_HIT   = "sl_hit"
    TRAILING = "trailing"


@dataclass
class ActivePosition:
    symbol:         str
    entry_price:    float
    size:           float
    side:           str        # 'long' | 'short'
    entry_time:     float
    confidence:     float
    momentum_score: float
    initial_sl:     float = 0
    current_sl:     float = 0
    current_tp:     float = 0
    highest_price:  float = 0
    lowest_price:   float = float('inf')
    partial_tp_done: List[int] = field(default_factory=list)

    def __post_init__(self):
        self.highest_price = self.entry_price if self.side == 'long' else 0
        self.lowest_price  = self.entry_price if self.side == 'short' else float('inf')


class MomentumTrader:
    def __init__(self, client):
        self.client = client

        self.active_positions: Dict[str, ActivePosition] = {}
        self.monitoring_tasks: Dict[str, asyncio.Task]   = {}

        # Configuración
        self.scan_interval      = 2
        self.momentum_lookback  = 20
        self.partial_tp_levels  = [1.5, 3.0, 5.0, 8.0]
        self.partial_tp_sizes   = [0.25, 0.25, 0.25, 0.25]
        self.trailing_activation = 2.0
        self.trailing_distance   = 0.5
        self.be_activation       = 1.0

        # Estadísticas
        self.total_realized_pnl: float = 0.0
        self.total_trades:       int   = 0
        self.winning_trades:     int   = 0
        self.best_trade_pnl:     float = float('-inf')
        self.worst_trade_pnl:    float = float('inf')
        self.start_time:         float = time.time()
        self.closed_trades:      List[Dict] = []

    # ── helpers klines (soporta dict Y lista) ─────────────────────────────────

    @staticmethod
    def _closes(klines: List) -> List[float]:
        """Extrae closes de klines independientemente del formato."""
        if not klines:
            return []
        if isinstance(klines[0], dict):
            return [float(x.get('close', 0)) for x in klines]
        return [float(x[4]) for x in klines]   # formato lista Bybit raw

    @staticmethod
    def _volumes(klines: List) -> List[float]:
        if not klines:
            return []
        if isinstance(klines[0], dict):
            return [float(x.get('volume', 0)) for x in klines]
        return [float(x[5]) for x in klines]

    @staticmethod
    def _highs_lows(klines: List):
        if not klines:
            return [], []
        if isinstance(klines[0], dict):
            h = [float(x.get('high',  0)) for x in klines]
            l = [float(x.get('low',   0)) for x in klines]
        else:
            h = [float(x[2]) for x in klines]
            l = [float(x[3]) for x in klines]
        return h, l

    # ── balance real ──────────────────────────────────────────────────────────

    async def get_balance_async(self) -> float:
        """Balance real de la cuenta."""
        try:
            return await self.client.async_get_balance()
        except Exception as e:
            logger.warning(f"get_balance_async: {e}")
            return 0.0

    def get_balance(self) -> float:
        """Síncrono — sólo para compatibilidad interna."""
        try:
            return self.client.get_usdt_balance()
        except Exception:
            return 0.0

    # ── Señal de momentum (integrada con analysis_engine) ─────────────────────

    async def _generate_momentum_signal(self, symbol: str) -> Optional[Dict]:
        """
        Genera señal basada en momentum puro (1m, 5m, 15m).
        Usa el mismo formato de resultado que analysis_engine_bybit.
        """
        try:
            klines_1m  = await self.client.async_get_klines(symbol, '1',  limit=30)
            klines_5m  = await self.client.async_get_klines(symbol, '5',  limit=20)
            klines_15m = await self.client.async_get_klines(symbol, '15', limit=15)

            closes_1m  = self._closes(klines_1m)
            closes_5m  = self._closes(klines_5m)
            closes_15m = self._closes(klines_15m)

            if len(closes_1m) < 10 or len(closes_5m) < 5:
                return None

            mom_1m  = self._momentum_score(closes_1m,  5)
            mom_5m  = self._momentum_score(closes_5m,  3)
            mom_15m = self._momentum_score(closes_15m, 3) if len(closes_15m) >= 5 else 0

            squeeze = self._detect_squeeze(closes_1m)

            # ── Determinar dirección ────────────────────────────────────────
            if mom_1m > 0.6 and mom_5m > 0.3 and mom_15m >= 0:
                direction = 'long'
                raw_score = (mom_1m * 0.5 + mom_5m * 0.3 + mom_15m * 0.2) * 5
            elif mom_1m < -0.6 and mom_5m < -0.3 and mom_15m <= 0:
                direction = 'short'
                raw_score = abs(mom_1m * 0.5 + mom_5m * 0.3 + mom_15m * 0.2) * 5
            elif squeeze and abs(mom_1m) > 0.5:
                direction = 'long' if mom_1m > 0 else 'short'
                raw_score = 3.0
            else:
                return None

            volume_spike = self._check_volume_spike(klines_1m)
            confidence   = min(95.0, 20 + volume_spike * 30 + abs(mom_1m) * 40)

            return {
                'direction':    direction,
                'signal':       'LONG' if direction == 'long' else 'SHORT',
                'score':        float(raw_score),
                'confidence':   float(confidence),
                'momentum_1m':  float(mom_1m),
                'momentum_5m':  float(mom_5m),
                'squeeze':      squeeze,
            }

        except Exception as e:
            logger.debug(f"_generate_momentum_signal {symbol}: {e}")
            return None

    def _momentum_score(self, prices: List[float], period: int) -> float:
        if len(prices) < period + 5 or prices[-period] == 0:
            return 0.0
        roc      = (prices[-1] - prices[-period]) / prices[-period]
        velocity = np.diff(prices[-period:])
        avg_vel  = float(np.mean(velocity)) if len(velocity) else 0
        accel    = np.diff(velocity)
        avg_acc  = float(np.mean(accel)) if len(accel) else 0
        return float(np.tanh(roc * 10 + avg_vel * 100 + avg_acc * 1000))

    def _detect_squeeze(self, closes: List[float], length: int = 20) -> bool:
        if len(closes) < length:
            return False
        sma  = np.mean(closes[-length:])
        std  = np.std(closes[-length:])
        if sma == 0:
            return False
        bw = (4 * std) / sma
        return bool(bw < 0.05)

    def _check_volume_spike(self, klines: List) -> float:
        vols = self._volumes(klines)
        if len(vols) < 10:
            return 0.0
        avg = np.mean(vols[:-1])
        return float(min(1.0, (vols[-1] / avg - 1) / 2)) if avg > 0 else 0.0

    # ── Precio en tiempo real ─────────────────────────────────────────────────

    async def _get_price(self, symbol: str) -> float:
        try:
            ticker = await self.client.async_get_ticker(symbol)
            return float(ticker.get('markPrice') or ticker.get('lastPrice') or 0)
        except Exception:
            return 0.0

    # ── Cálculos de posición ──────────────────────────────────────────────────

    def _calculate_r(self, pos: ActivePosition, current_price: float) -> float:
        risk = abs(pos.entry_price - pos.initial_sl)
        if risk == 0:
            return 0.0
        profit = (current_price - pos.entry_price) if pos.side == 'long' \
                 else (pos.entry_price - current_price)
        return profit / risk

    def _estimate_pnl(self, pos: ActivePosition, size: float, exit_price: float) -> float:
        mult = 1 if pos.side == 'long' else -1
        return (exit_price - pos.entry_price) * size * mult

    def _calculate_trend_strength(self, pos: ActivePosition, price: float) -> float:
        if pos.side == 'long':
            from_high = (pos.highest_price - price) / pos.highest_price \
                        if pos.highest_price > 0 else 0
            return max(-1.0, min(1.0, 1 - from_high * 2))
        else:
            from_low = (price - pos.lowest_price) / pos.lowest_price \
                       if pos.lowest_price > 0 else 0
            return max(-1.0, min(1.0, 1 - from_low * 2))

    def _calculate_tp_price(self, pos: ActivePosition, current_r: float) -> float:
        if pos.side == 'long':
            return pos.entry_price * (1 + current_r * 0.98)
        return pos.entry_price * (1 - current_r * 0.98)

    # ── Estrategia de salida ──────────────────────────────────────────────────

    def _evaluate_exit_strategy(self, pos, current_r, momentum,
                                 volatility, trend_strength, price) -> Optional[Dict]:
        # Emergency exit: momentum muy contrario
        if pos.side == 'long' and momentum < -0.7 and current_r > 0.5:
            return {'action': 'emergency_exit', 'reason': f'Momentum bearish ({momentum:.2f})', 'size_pct': 1.0}
        if pos.side == 'short' and momentum > 0.7 and current_r > 0.5:
            return {'action': 'emergency_exit', 'reason': f'Momentum bullish ({momentum:.2f})', 'size_pct': 1.0}

        # Partial TPs
        for i, tp_r in enumerate(self.partial_tp_levels):
            if current_r >= tp_r and i not in pos.partial_tp_done:
                size_pct = self.partial_tp_sizes[i] * (0.5 if momentum > 0.3 and trend_strength > 0.5 else 1.0)
                reason   = f'TP{i+1} {"parcial" if size_pct < self.partial_tp_sizes[i] else "completo"}'
                return {
                    'action':     'partial_tp',
                    'reason':     reason,
                    'size_pct':   size_pct,
                    'tp_level':   i,
                    'order_type': 'Market',   # FIX: era Limit/PostOnly → rechazado
                    'price':      self._calculate_tp_price(pos, current_r),
                }

        # Trailing stop
        if current_r >= self.trailing_activation and pos.current_sl != pos.entry_price:
            risk = abs(pos.entry_price - pos.initial_sl)
            if pos.side == 'long':
                new_sl = pos.highest_price - self.trailing_distance * risk
                if new_sl > pos.current_sl:
                    return {'action': 'update_sl', 'new_sl': new_sl, 'reason': f'Trailing {current_r:.1f}R'}
            else:
                new_sl = pos.lowest_price + self.trailing_distance * risk
                if new_sl < pos.current_sl or pos.current_sl == 0:
                    return {'action': 'update_sl', 'new_sl': new_sl, 'reason': f'Trailing {current_r:.1f}R'}

        # Break-even
        if current_r >= self.be_activation and pos.current_sl == pos.initial_sl:
            return {'action': 'move_be', 'new_sl': pos.entry_price, 'reason': f'BE {current_r:.1f}R'}

        # Expansión de target
        if current_r > 3 and momentum > 0.8 and trend_strength > 0.8 and volatility < 0.02:
            return {'action': 'expand_target', 'reason': 'Momentum explosivo', 'new_tp': current_r * 1.5}

        return None

    # ── Monitor intensivo ─────────────────────────────────────────────────────

    async def start_monitoring(self, symbol: str, position: ActivePosition):
        self.active_positions[symbol] = position
        if symbol in self.monitoring_tasks:
            self.monitoring_tasks[symbol].cancel()
        task = asyncio.create_task(self._intensive_monitor(symbol), name=f"monitor_{symbol}")
        self.monitoring_tasks[symbol] = task
        logger.info(f"🔥 {symbol}: Monitoreo iniciado | Entry: {position.entry_price}")

    async def _intensive_monitor(self, symbol: str):
        pos = self.active_positions.get(symbol)
        if not pos:
            return
        try:
            while symbol in self.active_positions:
                t0 = time.time()

                ticker    = await self.client.async_get_ticker(symbol)
                orderbook = await self.client.async_get_orderbook(symbol, limit=5)

                lp  = float(ticker.get('lastPrice', 0))
                bids = orderbook.get('b', []) or orderbook.get('bids', [])
                asks = orderbook.get('a', []) or orderbook.get('asks', [])
                bid = float(bids[0][0]) if bids else lp
                ask = float(asks[0][0]) if asks else lp
                market_price = (bid + ask) / 2 if bid and ask else lp

                if pos.side == 'long':
                    pos.highest_price = max(pos.highest_price, market_price)
                else:
                    pos.lowest_price  = min(pos.lowest_price, market_price)

                momentum      = await self._calculate_live_momentum(symbol)
                volatility    = await self._calculate_volatility(symbol)
                trend_str     = self._calculate_trend_strength(pos, market_price)
                current_r     = self._calculate_r(pos, market_price)

                action = self._evaluate_exit_strategy(
                    pos, current_r, momentum, volatility, trend_str, market_price
                )
                if action:
                    await self._execute_action(symbol, action, pos, market_price)

                await self._update_dynamic_stops(pos, current_r, market_price)

                # Detectar cierre externo
                real = await self.client.async_get_positions(symbol)
                real_size = sum(float(p.get('size', 0)) for p in (real or []))
                if real_size == 0 and symbol in self.active_positions:
                    logger.info(f"✅ {symbol}: Cerrado externamente (TP/SL hit)")
                    await self._close_position_cleanup(symbol, 'external')
                    break

                elapsed    = time.time() - t0
                await asyncio.sleep(max(0.2, self.scan_interval - elapsed))

        except asyncio.CancelledError:
            logger.info(f"🛑 {symbol}: Monitor cancelado")
        except Exception as e:
            logger.error(f"❌ {symbol}: Error monitor: {e}")
            await asyncio.sleep(5)
            asyncio.create_task(self._intensive_monitor(symbol))

    async def _calculate_live_momentum(self, symbol: str) -> float:
        try:
            k1 = await self.client.async_get_klines(symbol, '1',  limit=30)
            k5 = await self.client.async_get_klines(symbol, '5',  limit=20)
            m1 = self._momentum_score(self._closes(k1), 10)
            m5 = self._momentum_score(self._closes(k5),  5)
            return m1 * 0.7 + m5 * 0.3
        except Exception:
            return 0.0

    async def _calculate_volatility(self, symbol: str) -> float:
        try:
            kl = await self.client.async_get_klines(symbol, '1', limit=20)
            closes = self._closes(kl)
            highs, lows = self._highs_lows(kl)
            tr_list = [
                max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                for i in range(1, len(closes))
            ]
            atr = float(np.mean(tr_list)) if tr_list else 0.0
            return float(atr / closes[-1]) if closes and closes[-1] > 0 else 0.01
        except Exception:
            return 0.01

    async def _update_dynamic_stops(self, pos: ActivePosition, current_r: float, price: float):
        pass  # delegado a _evaluate_exit_strategy / _execute_action

    # ── Ejecutar acciones ─────────────────────────────────────────────────────

    async def _execute_action(self, symbol: str, action: Dict,
                               pos: ActivePosition, price: float):
        a = action['action']

        if a == 'partial_tp':
            size_close = pos.size * action['size_pct']
            level      = int(action['tp_level'])
            try:
                await self.client.async_create_order(
                    symbol=symbol,
                    side='Sell' if pos.side == 'long' else 'Buy',
                    qty=size_close,
                    order_type='Market',   # siempre Market para garantizar fill
                    reduce_only=True,
                )
                pos.partial_tp_done.append(level)
                pos.size -= size_close
                pnl = self._estimate_pnl(pos, size_close, price)
                logger.info(f"💰 {symbol}: TP{level+1} | size={size_close:.4f} | PnL≈{pnl:.2f}")
                await self.notify_telegram(
                    f"💰 <b>{symbol}</b> TP{level+1}\nR: {self._calculate_r(pos, price):.1f}\n≈{pnl:.2f} USDT"
                )
            except Exception as e:
                logger.error(f"partial_tp {symbol}: {e}")
                await self._market_close_partial(symbol, size_close, pos)

        elif a == 'emergency_exit':
            try:
                await self.client.async_create_order(
                    symbol=symbol,
                    side='Sell' if pos.side == 'long' else 'Buy',
                    qty=pos.size, order_type='Market', reduce_only=True
                )
                pnl = self._estimate_pnl(pos, pos.size, price)
                logger.warning(f"🚨 {symbol}: EMERGENCY EXIT | PnL≈{pnl:.2f} | {action.get('reason','')}")
                await self._close_position_cleanup(symbol, 'emergency', pnl)
            except Exception as e:
                logger.error(f"emergency_exit {symbol}: {e}")

        elif a in ('update_sl', 'move_be'):
            new_sl = action['new_sl']
            try:
                await self.client.async_set_stop_loss(symbol, new_sl)
                pos.current_sl = new_sl
                logger.info(f"🛡️ {symbol}: SL→{new_sl:.4f} | {action.get('reason','')}")
            except Exception as e:
                logger.error(f"update_sl {symbol}: {e}")

    async def _market_close_partial(self, symbol: str, size: float, pos: ActivePosition):
        try:
            await self.client.async_create_order(
                symbol=symbol,
                side='Sell' if pos.side == 'long' else 'Buy',
                qty=size, order_type='Market', reduce_only=True
            )
        except Exception as e:
            logger.error(f"_market_close_partial {symbol}: {e}")

    async def _close_position(self, symbol: str):
        pos = self.active_positions.get(symbol)
        if not pos:
            return
        try:
            price = await self._get_price(symbol)
            await self.client.async_create_order(
                symbol=symbol,
                side='Sell' if pos.side == 'long' else 'Buy',
                qty=pos.size, order_type='Market', reduce_only=True
            )
            pnl = self._estimate_pnl(pos, pos.size, price)
            await self._close_position_cleanup(symbol, 'manual', pnl)
        except Exception as e:
            logger.error(f"_close_position {symbol}: {e}")

    async def _close_position_cleanup(self, symbol: str, reason: str, realized_pnl: float = 0.0):
        pos = self.active_positions.get(symbol)
        if pos and realized_pnl != 0:
            self.total_realized_pnl += float(realized_pnl)
            self.total_trades += 1
            if realized_pnl > 0:
                self.winning_trades += 1
            self.best_trade_pnl  = max(self.best_trade_pnl,  float(realized_pnl))
            self.worst_trade_pnl = min(self.worst_trade_pnl, float(realized_pnl))
            self.closed_trades.append({
                'symbol':   symbol, 'pnl': float(realized_pnl),
                'side':     pos.side if pos else None,
                'duration': time.time() - (pos.entry_time if pos else time.time()),
                'reason':   reason,  'time': time.time(),
            })
        # Cancel and remove monitoring task if exists
        if symbol in self.monitoring_tasks:
            task = self.monitoring_tasks.pop(symbol, None)
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass

        # Remove active position if exists
        if symbol in self.active_positions:
            try:
                del self.active_positions[symbol]
            except Exception:
                pass
        logger.info(f"🧹 {symbol}: cleanup | {reason} | PnL: {realized_pnl:+.2f}")

    # ── Scanner de nuevas entradas ─────────────────────────────────────────────

    async def scan_for_new_entries(self, symbols: Optional[List[str]] = None):
        symbols = symbols or ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT']
        for symbol in symbols:
            if symbol in self.active_positions:
                continue
            try:
                signal = await self._generate_momentum_signal(symbol)
                if not signal or signal['score'] < 2.0 or signal['confidence'] < 25:
                    continue
                if len(self.active_positions) >= 5:
                    # Rotación: cerrar peor posición si señal es mucho mejor
                    worst, worst_r = None, float('inf')
                    for p in self.active_positions.values():
                        price_p = await self._get_price(p.symbol)
                        r_p = self._calculate_r(p, price_p)
                        if worst is None or r_p < worst_r:
                            worst, worst_r = p, r_p
                    if worst and signal['score'] > worst_r * 1.5:
                        logger.info(f"🔄 Rotando: {worst.symbol} → {symbol}")
                        await self._close_position(worst.symbol)
                    else:
                        continue
                await self._open_momentum_position(symbol, signal)
            except Exception as e:
                logger.error(f"scan_for_new_entries {symbol}: {e}")

    async def _open_momentum_position(self, symbol: str, signal: Dict):
        """Abre posición con SL/TP calculados correctamente (sin TPs negativos)."""
        try:
            balance = await self.get_balance_async()
            if balance <= 0:
                logger.warning(f"Balance inválido ({balance}), no se puede abrir {symbol}")
                return

            price = await self._get_price(symbol)
            if price <= 0:
                return

            # Tamaño basado en confianza y balance real
            confidence = float(signal.get('confidence', 50))
            size_factor = 0.5 + confidence / 200   # 0.5x a 1.0x
            risk_usdt   = balance * 0.01 * size_factor

            atr = await self._calculate_volatility(symbol) * price
            sl_mult = 1.5 if signal.get('squeeze') else 2.0
            sl_dist = max(atr * sl_mult, price * 0.005)   # mínimo 0.5% del precio

            if signal['direction'] == 'long':
                sl = price - sl_dist
                tp = price + sl_dist * 3    # FIX: siempre positivo para LONG
                side_str = 'Buy'
            else:
                sl = price + sl_dist
                tp = price - sl_dist * 3    # FIX: asegurar que es menor que entry
                side_str = 'Sell'
                # Validar: tp debe ser > 0 y < price
                if tp <= 0:
                    logger.warning(f"TP inválido calculado para {symbol} SHORT: {tp:.6f}")
                    return

            # Qty en contratos
            qty = risk_usdt / sl_dist if sl_dist > 0 else 0
            qty = min(qty, (balance * 0.08) / price)   # cap 8% del balance
            qty = round(qty, 4)
            if qty <= 0:
                return

            resp = await self.client.async_create_order(
                symbol=symbol, side=side_str, qty=qty,
                order_type='Market', reduce_only=False
            )
            if not resp or (isinstance(resp, dict) and resp.get('retCode', -1) not in (0, None)):
                logger.error(f"Order failed {symbol}: {resp}")
                return

            await self.client.async_set_stop_loss(symbol, sl)

            pos = ActivePosition(
                symbol=symbol, entry_price=price, size=qty,
                side=signal['direction'], entry_time=time.time(),
                confidence=confidence, momentum_score=signal['score'],
                initial_sl=sl, current_sl=sl, current_tp=tp,
            )
            await self.start_monitoring(symbol, pos)

            logger.info(
                f"🚀 {symbol}: {signal['direction'].upper()} | "
                f"price={price:.4f} qty={qty:.4f} SL={sl:.4f} TP={tp:.4f} "
                f"score={signal['score']:.2f}"
            )
            await self.notify_telegram(
                f"🚀 <b>{symbol}</b> {signal['direction'].upper()}\n"
                f"Score: {signal['score']:.2f} | Conf: {confidence:.0f}%\n"
                f"Entry: {price:.4f} | SL: {sl:.4f} | TP: {tp:.4f}"
            )

        except Exception as e:
            logger.error(f"_open_momentum_position {symbol}: {e}")

    # ── Compatibilidad con telegram_commands ──────────────────────────────────

    async def notify_telegram(self, message: str):
        pass   # sobrescrito externamente si se conecta un notifier
