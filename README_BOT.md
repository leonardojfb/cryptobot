# 🤖 Bot Autónomo de Trading — Bybit Paper Trading

## Arquitectura del sistema

```
main_bot.py               ← Punto de entrada principal
├── bot_autonomous.py     ← Orquestador: abre/cierra trades
├── bybit_client.py       ← Cliente Bybit API (Paper + Real)
├── analysis_engine_bybit.py ← Motor de análisis técnico
│   ├── EMA 7/21/50/200
│   ├── RSI + Stoch RSI
│   ├── MACD
│   ├── Bollinger Bands
│   ├── SuperTrend
│   ├── Ichimoku Cloud
│   ├── Williams %R
│   ├── ATR (trailing stop)
│   ├── VWAP
│   ├── OBV
│   └── Orderbook Imbalance
├── learning_engine.py    ← IA: aprende de cada trade
│   ├── Ajusta umbrales de señal
│   ├── Ajusta TP/SL multipliers
│   ├── Ajusta pesos por timeframe
│   └── Ajusta risk por winrate
├── risk_manager.py       ← Circuit breaker, drawdown máx.
└── tg_controller.py      ← Control remoto via Telegram
```

## Instalación

```bash
pip install -r requirements_bot.txt
```

## Configuración

1. Copia `.env.example` → `.env`
2. Obtén API keys de Bybit Demo Trading:
   - Ve a bybit.com y activa "Demo Trading" desde el menú de tu cuenta
   - En el panel de Demo → API Management → Crea una clave
   - **IMPORTANTE**: Las claves de demo son diferentes a las de real
3. Edita `.env` con tus datos

## Uso

```bash
python main_bot.py
```

## Control via Telegram

Configura `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`.

| Comando | Descripción |
|---------|-------------|
| `/start` | Iniciar bot |
| `/stop` | Detener bot |
| `/status` | Estado general y rendimiento |
| `/pos` | Posiciones abiertas |
| `/close BTCUSDT` | Cerrar posición específica |
| `/closeall` | Cerrar todo |
| `/perf` | Rendimiento histórico detallado |
| `/params` | Ver parámetros del learning engine |
| `/set min_score_long 4.5` | Cambiar parámetro manualmente |
| `/watchlist` | Ver pares monitoreados |
| `/add DOGEUSDT` | Añadir par |
| `/remove DOGEUSDT` | Quitar par |
| `/risk` | Estado del gestor de riesgo |

## Cómo funciona el aprendizaje

El `learning_engine.py` registra **cada trade** con un snapshot completo:
- Score compuesto de cada timeframe
- Confianza de la señal
- ATR, indicadores, etc.

Al cerrar un trade, analiza el resultado y **ajusta automáticamente**:

| Win Rate | Acción |
|----------|--------|
| > 65% | Baja umbral de señal (más trades) |
| < 40% | Sube umbral de señal (más selectivo) |
| R:R < 1.5 | Amplía TP |
| R:R > 3.0 | Reduce TP (cierra más rápido) |

También aprende qué **timeframes** son más predictivos y les da más peso.

## Flujo de una señal

```
Scan (cada 30s)
  → analyze_symbol() multi-TF
  → composite_score: -10 a +10
  → score > 3.5 → LONG | score < -3.5 → SHORT
  → risk_manager.can_open()
  → learning_engine.calculate_position_size()
  → bybit_client.place_order() con TP/SL
  → learning_engine.record_open()

Monitor (cada 10s)
  → Actualiza trailing stop
  → Detecta si TP/SL fue hit
  → learning_engine.record_close()
  → Dispara ajuste de parámetros (cada 10 trades)
```

## Pasar a dinero real

1. Obtén API keys de tu cuenta **real** de Bybit
2. En `.env`: `PAPER_TRADING=false`
3. Empieza con `RISK_PCT_PER_TRADE=0.5` y `DEFAULT_LEVERAGE=5`
4. El bot ya habrá aprendido con paper trading y tendrá mejores parámetros

## Notas importantes

- El bot usa la **cuenta Demo de Bybit** (no testnet) → gráficas reales, datos reales
- El endpoint de Demo es `api-demo.bybit.com` — completamente diferente a testnet
- Las posiciones en Demo se ven en el panel de Bybit con "Demo Trading" activado
- El learning engine guarda su memoria en `bot_memory.json` — no borres este archivo
