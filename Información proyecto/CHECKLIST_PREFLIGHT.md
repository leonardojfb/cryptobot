# ✅ PRE-FLIGHT CHECKLIST - Bot Trading Autoaprendiente

## 🔥 ANTES DE EJECUTAR EL BOT

### Part 1: Environment Setup
- [ ] Python 3.10+ instalado (`python --version`)
- [ ] Virtual environment activo (`.venv`)
- [ ] Packages instalados (`pip list | grep -i requirements`)
- [ ] `.env` file with all API keys present
- [ ] Git cliente installed (para updates)

### Part 2: API Credentials
- [ ] DEEPSEEK_API_KEY válida (testing con `curl`)
- [ ] BYBIT_API_KEY válida (testing con GET /v5/account)
- [ ] BYBIT_API_SECRET presente
- [ ] TELEGRAM_BOT_TOKEN válida (testing con /getMe)
- [ ] TELEGRAM_CHAT_ID es un número válido (no @username)

### Part 3: Code Quality
- [ ] `python -m py_compile *.py` - No syntax errors
- [ ] `python quickstart.py` - All checks pass
- [ ] `python test_learning_flow.py` - Learning cycle test passes
- [ ] No hay imports rotos (`python -c "import bot_autonomous"`)

### Part 4: Database
- [ ] `ai_memory.db` exists y es accesible
- [ ] Tablas `ai_decisions` y `trade_outcomes` creadas
- [ ] DB es writable (not on read-only filesystem)

### Part 5: Telegram Setup
- [ ] Bot iniciado en BotFather (@BotFather /start)
- [ ] Chat/group agregado al bot
- [ ] `/start` retorna menú
- [ ] `/help` muestra comandos

### Part 6: Bybit Setup
- [ ] Trading enabled en account
- [ ] At least 10 USDT de balance
- [ ] Leverage settings configured (max 20x recomendado)
- [ ] Symbols en watchlist tienen sufficient liquidity

### Part 7: News Engine
- [ ] News feeds accessible (RSS feeds not blocked)
- [ ] Fear & Greed index accessible (cryptofear.index.im)
- [ ] No rate limits hits en primeros 10 requests

---

## 🧪 SMOKE TEST SEQUENCE

### Step 1: Start Bot
```bash
python main.py
```
**Expected:** 
- Log: "🚀 Bot autónomo en marcha"
- News engine starts
- Scanner starts
- Monitor starts

### Step 2: Telegram /start
```
User: /start
Bot: Menú con opciones (🤖 AI, 💬 Noticias, etc)
```

### Step 3: Check Scanner
```
Expected logs:
📊 BTCUSDT: LONG  score=+45.2  conf=62%
📊 ETHUSDT: FLAT  score=-12.0  conf=38%
```
**Should see:** At least 10 symbols scanned in first 10 seconds

### Step 4: Check Learning Stats
```telegram
User: /aprend
Bot: "Resumen de Aprendizaje (últimos 7 días):
     Total PnL: +0 USDT (sin trades aún)
     Win Rate: N/A
     ..."
```

### Step 5: Check News Integration
```telegram
User: /noticias
Bot: "Influencia de Noticias:
     Fear & Greed: 55 - Neutral
     ..."
```

### Step 6: Manual Trade (Paper Mode)
```
1. Set PAPER_TRADING=true in main.py
2. Open 1 fake trade via /ai command
3. When it "closes" after 10 seconds
4. Verify in /aprend: should show 1 trade (WIN or LOSS)
```

### Step 7: Database Check
```bash
sqlite3 ai_memory.db
sqlite> SELECT COUNT(*) FROM ai_decisions;
# Should return: 1+
sqlite> SELECT * FROM ai_decisions ORDER BY ts DESC LIMIT 1;
# Should see your test trade
```

---

## 🎯 READY TO GO?

### If All Checks Pass
```bash
# Real trading
export PAPER_TRADING=false
python main.py
```

### If Something Fails

#### Problem: "DeepSeek 401 Unauthorized"
```
Solution:
1. Check DEEPSEEK_API_KEY in .env
2. Verify key format (should start with "sk-")
3. Test: curl -H "Authorization: Bearer $KEY" https://api.deepseek.com/...
```

#### Problem: "Bybit connection refused"
```
Solution:
1. Check internet connection
2. Verify BYBIT_API_KEY / SECRET correct
3. Paste URL in browser: https://api.bybit.com/v5/account
   (should return JSON, not error)
```

#### Problem: "Telegram bot not responding"
```
Solution:
1. Verify TELEGRAM_BOT_TOKEN correct
2. Verify TELEGRAM_CHAT_ID is a number (not @username)
3. Restart bot: stop and python main.py again
```

#### Problem: "DB locked / can't create trades"
```
Solution:
1. Check file permissions: chmod 644 ai_memory.db
2. Ensure not on network drive (bad performance)
3. Restart Python (db connection pool issue)
```

---

## 📋 FIRST TRADE CHECKLIST

When bot opens first trade:

- [ ] Telegram: "🟢 TRADE ABIERTO" message received
- [ ] Message shows: Par, Leverage, Entry, TP, SL
- [ ] Order visible on Bybit UI
- [ ] Bot logs show: "Orden colocada exitosamente"

When trade closes (TP/SL hit):

- [ ] Telegram: "✅ CERRADO" message received
- [ ] Message shows: PnL (with +/- sign), Duration
- [ ] Bot logs show: "[WIN/LOSS] trade closed [TP/SL]"
- [ ] `/aprend` now shows: 1 trade, PnL value, win_rate

## 🎓 MONITORING DASHBOARD

For next 100 trades, monitor these metrics:

```
Daily:
  - /aprend → Total PnL (should trend up over time)
  - Trade count (should increase)
  - Win rate (should stabilize around 60%)

Weekly:
  - /aprend → Profit Factor (should be > 1.5x)
  - /noticias → Fear & Greed impact
  - /accuracy → AI vs Technical comparison

Monthly:
  - Sharpe Ratio (risk-adjusted returns)
  - Drawdown recovery
  - Best/Worst pairs identified
```

---

## 🔄 OPERATIONAL PROCEDURES

### Daily
```bash
# Morning
python main.py

# Evening
# Check logs, verify no errors
# Screenshot /aprend stats for record

# Night
# Keep running (bot trades 24/7 if market open)
```

### Weekly
```
Monday:
  1. Review /aprend stats
  2. Identify worst performing pair
  3. Consider reducing leverage for that pair
  
Friday:
  1. Export learning data for analysis
  2. Backup ai_memory.db
  3. Plan any parameter adjustments
```

### Monthly
```
1. Full performance analysis
2. Compare actual vs. expected metrics
3. Adjust setup if needed
4. Document changes in CHANGES_SUMMARY.md
```

---

## 🚨 EMERGENCY PROCEDURES

### If Bot Crashes
```bash
1. Stop bot: Ctrl+C
2. Check logs: tail -50 bot.log
3. Fix issue
4. Restart: python main.py
```

### If Market Crashes
```bash
1. Bot automatically pauses (built-in circuit breaker)
2. Check /aprend to see position status
3. Manual close if needed: /close SYMBOL
4. Resume: /resume
```

### If API Down
```bash
1. DeepSeek down: AI disabled gracefully (continues with tech)
2. Bybit down: Bot waits for reconnect (up to 5 min)
3. Telegram down: Logs continue, no notifications
```

### If You Need to Stop Trading Immediately
```telegram
/stop
# Bot halts all trading, closes web sockets
```

---

## 📞 SUPPORT

If stuck, check these files:
1. `LEARNING_GUIDE.md` - How learning works
2. `STATUS_FINAL.md` - Features checklist
3. `CHANGES_SUMMARY.md` - What changed
4. Bot logs - `bot.log` (if configured)

For API issues:
- Bybit docs: https://bybit-exchange.github.io/docs/
- DeepSeek docs: https://api-docs.deepseek.com/
- cryptofear index: https://cryptofear.index.im/

---

## ✅ FINAL SIGN-OFF

- [ ] I have read LEARNING_GUIDE.md
- [ ] I have run all smoke tests successfully
- [ ] I understand that bot uses real money (if not paper mode)
- [ ] I have set up stop-loss safeguards
- [ ] I am ready to monitor daily for first week
- [ ] I have backed up ai_memory.db

**Status: READY FOR LAUNCH** 🚀

---

Date: ________
Signed: ________________________
Comments: _____________________________________________________

---

Remember: The bot learns from every trade. 
Give it at least 50 trades before judging performance.
That's when patterns start to emerge. 🎯

