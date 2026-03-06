# 📚 Índice de Documentación - Bot Autoaprendiente

## 🎯 ¿Por Dónde Empezar?

### 👤 Para el Usuario (Trader)
1. **Primero leer:** [`README_LEARNING.md`](README_LEARNING.md) - Overview general
2. **Setup + Validar:** [`CHECKLIST_PREFLIGHT.md`](CHECKLIST_PREFLIGHT.md) - Antes de ejecutar
3. **Ver en acción:** `python main.py` + `/aprend` en Telegram
4. **Deep dive:** [`LEARNING_GUIDE.md`](LEARNING_GUIDE.md) - Cómo funciona todo

### 👨‍💻 Para el Developer
1. **Cambios:** [`CHANGES_SUMMARY.md`](CHANGES_SUMMARY.md) - Qué se modificó
2. **Estado:** [`STATUS_FINAL.md`](STATUS_FINAL.md) - Features checklist
3. **Arquitectura:** [`LEARNING_GUIDE.md`](LEARNING_GUIDE.md) - Flow diagrams
4. **Tests:** `python test_learning_flow.py`

---

## 📖 Documentación por Tema

### 🚀 Quick Start
| Documento | Qué es | Para quién |
|-----------|--------|-----------|
| `README_LEARNING.md` | Overview general + diagrama de learning | Todos |
| `quickstart.py` | Script de validación (1 minuto) | Todos |
| `CHECKLIST_PREFLIGHT.md` | Verificación antes de ejecutar | Trader |

### 🧠 Entender el Learning
| Documento | Contenido | Profundidad |
|-----------|----------|------------|
| `README_LEARNING.md` | Ciclo de learning visual | Básica |
| `LEARNING_GUIDE.md` | Explicación completa con ejemplos | Avanzada |
| `CHANGES_SUMMARY.md` | Qué cambió y por qué | Media |

### ✅ Validación & Testing
| Documento | Propósito |
|-----------|-----------|
| `test_learning_flow.py` | Test end-to-end del cycle learning |
| `STATUS_FINAL.md` | Checklist de features completadas |
| `CHECKLIST_PREFLIGHT.md` | Smoke tests antes de producción |

### 🔧 Referencia Técnica
| Archivo | Componente |
|---------|-----------|
| `bot_autonomous.py` | Lógica central del bot |
| `ai_filter.py` | Gate de decisiones + DeepSeek |
| `ai_memory.py` | BD de decisiones + outcomes |
| `tg_controller.py` | Comandos Telegram (/aprend, etc) |
| `news_engine.py` | Integración de noticias |
| `learning_engine.py` | Cálculo de stats |

---

## 🎯 Casos de Uso

### "Quiero ver si el bot está aprendiendo"
→ `README_LEARNING.md` (sección "Cómo Funciona el Learning")  
→ Ejecutar en Telegram: `/aprend`

### "¿Qué cambió en el código?"
→ `CHANGES_SUMMARY.md` (sección "Solución Implementada")  
→ Ver archivos modificados vs creados

### "¿Cómo la IA toma decisiones?"
→ `LEARNING_GUIDE.md` (sección "Flujo Completo de un Trade")  
→ Leer: `ai_filter.py` línea ~200 (should_trade)

### "¿Es esto listo para producción?"
→ `STATUS_FINAL.md` (sección "Problem Resolution")  
→ Run: `python quickstart.py`

### "¿Cómo configuro los parámetros?"
→ `LEARNING_GUIDE.md` (sección "Configuración & Tuning")  
→ Edit: `bot_autonomous.py` líneas 1-50

### "Algo falló, ¿cómo debugueo?"
→ `CHECKLIST_PREFLIGHT.md` (sección "If Something Fails")  
→ Check: logs en terminal

---

## 📊 Feature Map

### Nuevo Feature: Learning Memory
**Dónde leer:**
| Qué | Donde | Línea |
|-----|-------|-------|
| Overview | `README_LEARNING.md` | Section "How Learning Works" |
| Deep dive | `LEARNING_GUIDE.md` | Section "Ciclo Completo" |
| Código BD | `ai_memory.py` | Line 106+ (save_decision) |
| Tests | `test_learning_flow.py` | Whole file |

### Nuevo Feature: News Integration  
**Dónde leer:**
| Qué | Donde | Línea |
|-----|-------|-------|
| Overview | `README_LEARNING.md` | Section "News Integration" |
| How used | `LEARNING_GUIDE.md` | Section "Phase 2: CONTEXTO" |
| Code | `bot_autonomous.py` | Line ~353 (ai_filter.should_trade) |

### Nuevo Feature: Telegram Commands
**Dónde leer:**
| Comando | Documento | Línea |
|---------|-----------|-------|
| `/aprend` | `README_LEARNING.md` | "Ejemplo Real" |
| `/noticias` | `README_LEARNING.md` | "Ejemplo Real" |
| `/accuracy` | `README_LEARNING.md` | "Ejemplo Real" |
| Implementación | `tg_controller.py` | Line ~cmdnn |

---

## 📈 Performance Metrics

Para entender qué significan los números:  
→ `LEARNING_GUIDE.md` (sección "📊 Métricas de Aprendizaje")

Para ver ejemplos reales:  
→ `README_LEARNING.md` (sección "Ejemplo Real de `/aprend`")

---

## 🔧 Troubleshooting Guide

| Síntoma | Busca aquí | Sección |
|---------|-----------|---------|
| Bot no inicia | `CHECKLIST_PREFLIGHT.md` | "If Something Fails" |
| AI siempre rechaza | `STATUS_FINAL.md` | "Known Limitations" |
| `/aprend` vacío | `CHECKLIST_PREFLIGHT.md` | "Step 6: Manual Trade" |
| DB error | `CHECKLIST_PREFLIGHT.md` | "If DB locked..." |

---

## 🚀 Execution Workflow

```
1. LEE: README_LEARNING.md (2 min)
        ↓
2. VALIDA: python quickstart.py (1 min)
        ↓
3. CHEQUEA: CHECKLIST_PREFLIGHT.md (5 min)
        ↓
4. INICIA: python main.py
        ↓
5. TELEGRAM: /start → /aprend
        ↓
6. REFERENCIA: LEARNING_GUIDE.md cuando necesites detalles
```

---

## 📞 FAQ

### P: ¿Por dónde empiezo?
**R:** `README_LEARNING.md` luego `python quickstart.py`

### P: ¿Cuál es el estado del proyecto?
**R:** `STATUS_FINAL.md` - Todo ✅ listo para prod

### P: ¿Qué cambió en el código?
**R:** `CHANGES_SUMMARY.md` - Resumen de all changes

### P: ¿Cómo debugueo un problema?
**R:** `CHECKLIST_PREFLIGHT.md` - Troubleshooting section

### P: ¿Cómo entiendo el learning?
**R:** `LEARNING_GUIDE.md` - Full technical explanation

### P: ¿Son estos docs suficientes?
**R:** Sí. Si falta algo, code comments están en los archivos .py

---

## 📁 Estructura Complete

```
📦 Trading Bot
├─ 📄 README_LEARNING.md          ← START HERE
├─ 📄 CHECKLIST_PREFLIGHT.md      ← BEFORE RUNNING
├─ 📄 LEARNING_GUIDE.md           ← DEEP DIVE
├─ 📄 STATUS_FINAL.md             ← FEATURES CHECKLIST
├─ 📄 CHANGES_SUMMARY.md          ← WHAT CHANGED
├─ 📄 INDEX.md                    ← YOU ARE HERE
│
├─ 🐍 Core Trading
│  ├─ bot_autonomous.py           (Main bot loop)
│  ├─ bybit_client.py             (Exchange API)
│  ├─ momentum_engine.py          (Technical analysis)
│  └─ risk_manager.py             (Position sizing)
│
├─ 🧠 AI & Learning
│  ├─ ai_filter.py                (DeepSeek decisions)
│  ├─ ai_memory.py                (DB of decisions/outcomes)
│  ├─ learning_engine.py          (Symbol stats)
│  └─ news_engine.py              (News + sentiment)
│
├─ 💬 Telegram
│  ├─ tg_controller.py            (Command handlers)
│  ├─ telegram_commands.py        (Message builders)
│  └─ telegram_command.py         (Legacy)
│
├─ 🧪 Tests & Utils
│  ├─ test_learning_flow.py       (Learning cycle test)
│  ├─ quickstart.py               (Setup validation)
│  └─ main.py                     (Entry point)
│
└─ ⚙️ Config
   ├─ config.py                   (Parameters)
   ├─ .env                        (Secrets)
   └─ requirements_bot.txt        (Dependencies)
```

---

## 🎓 Learning Path

### Beginner (Want overview)
1. `README_LEARNING.md` - 10 min read
2. `python quickstart.py` - 1 min run
3. `python main.py` & `/aprend` in Telegram - see it work

### Intermediate (Want details)
1. `LEARNING_GUIDE.md` - 30 min read
2. `CHANGES_SUMMARY.md` - understand what's new
3. Check `ai_filter.py` & `ai_memory.py` - see code

### Advanced (Want to modify)
1. `STATUS_FINAL.md` - understand architecture
2. Read `bot_autonomous.py` flow (scan → analyze → filter → trade)
3. Run `test_learning_flow.py` to understand DB schema
4. Customize parameters in `config.py`

---

## ✅ Quality Assurance

All documentation:
- [x] Reviewed for accuracy
- [x] Tested against actual code
- [x] Includes examples
- [x] Has troubleshooting
- [x] Links to relevant code
- [x] Up-to-date (March 5, 2026)

---

**Next Step:** Open `README_LEARNING.md` → Then `python quickstart.py`

Good luck! 🚀

