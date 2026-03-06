# 🔧 GUÍA TÉCNICA: Spinner Infinito y Flujo de Callbacks en Telegram

## El Problema: ¿Por qué el botón queda con spinner infinito?

### Flujo Correcto (Lo que debe pasar):

```
1. USUARIO presiona botón en Telegram
   ↓
2. BOT RECIBE callback_query (NO es un message)
   ↓
3. BOT RESPONDE INMEDIATAMENTE: await query.answer()  ←← CRÍTICO
   └─→ Telegram: "OK, spinner quitado"
   ↓
4. BOT procesa lógica (cierre, actualización, etc.)
   ↓
5. BOT edita el mensaje: query.edit_message_text()
   ↓
6. USUARIO ve resultado actualizado
```

### Flujo Incorrecto (Spinner infinito):

```
1. USUARIO presiona botón
   ↓
2. BOT RECIBE callback_query
   ↓
3. BOT OLVIDA responder (sin await query.answer())
   ↓
4. Telegram espera respuesta... (SPINNER GIRANDO)
   ↓
5. 30 segundos después → TIMEOUT DEL USUARIO
   ↓
6. Usuario ve "❌ Telegram: No se conectó"
```

---

## ✅ Lo Que Hemos Arreglado

### 1. **`await q.answer()` AHORA ES CRÍTICO**

Antes:
```python
try:
    await q.answer()
    log.debug(...)  # Log poco importante
except Exception as e:
    log.error(...)  # Error mínimo
```

Ahora:
```python
try:
    await q.answer()  # ← INMEDIATO
    log.info(f"[CALLBACK] ✅ ANSWER ENVIADO - spinner bajo control")
except Exception as e:
    log.error(f"[CALLBACK] ❌ CRÍTICO: Error en q.answer() - El botón puede quedar en spinner: {e}")
    # ↑↑↑ Si esto falla, ES UN PROBLEMA GRAVE
```

### 2. **Logs Detallados en Refresh**

Antes:
```python
elif data.startswith("refresh:"):
    sym = data[8:]
    if not BOT_INSTANCE:
        await q.edit_message_text("Bot no disponible")
        return
    # ... sin logs internos ...
    try:
        await q.edit_message_text(...)
    except Exception:
        pass  # Silencioso!
```

Ahora:
```python
elif data.startswith("refresh:"):
    sym = data[8:]
    log.info(f"[REFRESH] Solicitud - usuario: {user_id}, símbolo: {sym}")
    
    if not BOT_INSTANCE:
        log.error(f"[REFRESH] ERROR: BOT_INSTANCE no disponible")  # ←← Visible!
        # ...
    
    log.debug(f"[REFRESH] Obteniendo posición {sym} del lock")
    # ...
    log.debug(f"[REFRESH] Editando mensaje")
    await q.edit_message_text(...)
    log.info(f"[REFRESH] ✅ Actualización completada")
    # ↑↑↑ Cada paso es visible en logs
```

### 3. **Mejor Manejo de Excepciones**

Excepciones "silenciosas" ahora se logguean:

```python
except Exception as e:
    log.debug(f"[REFRESH] Excepción al editar (normal si no hay cambios): {type(e).__name__}: {e}")
    # ↑↑↑ Sabes EXACTAMENTE qué causó el problema
```

---

## 🔍 Cómo Debuggear con los Logs

### Escenario 1: Botón presionado pero spinner infinito

**Qué ver en los logs:**

```
❌ MALO - No ves esto:
[CALLBACK] ✅ ANSWER ENVIADO - spinner bajo control

✅ BUENO - Ves:
[CALLBACK] ✅ ANSWER ENVIADO - spinner bajo control
[CLOSE] Solicitud de cierre del símbolo: BTCUSDT
[CLOSE] ✅ BTCUSDT cerrado exitosamente
```

**Si NO ves `ANSWER ENVIADO`:**
- El callback no se registró correctamente
- Verifica que `CallbackQueryHandler(handle_callback)` está en `tg_controller.py` línea ~1145
- Reinicia el bot

### Escenario 2: Botón responde (spinner desaparece) pero nada ocurre

**Qué buscar:**

```
[CALLBACK] Botón presionado - usuario: 8172390917, data: 'close:BTCUSDT'
[CALLBACK] ✅ ANSWER ENVIADO
[CLOSE] Solicitud de cierre del símbolo: BTCUSDT
[CLOSE] Ejecutando try_close_trade para BTCUSDT
[CLOSE] Resultado de cierre: False  ← ¡ACA ESTÁ!
[CLOSE] ❌ No hay posición abierta
```

Significado: La posición NO existe o ya fue cerrada.

### Escenario 3: Spam de excepciones en refresh

**Qué buscar:**

```
[REFRESH] ✅ Actualización completada para BTCUSDT
[REFRESH] Excepción (normal si no hay cambios): BadRequest: message is not modified
```

**Esto es NORMAL**. Significa que el precio no cambió, así que Telegram rechaza el edit. Es seguro ignorarlo.

---

## 📊 Flujo Completo de UN Botón

### Usuario presiona: "❌ Cerrar BTCUSDT"

```
TELEGRAM                              TU BOT
   │                                    │
   ├─ Usuario presiona botón ──────────→ handle_callback()
   │                                    │
   │                                    ├─ Recibe: callback_query
   │                                    │  └─ data="close:BTCUSDT"
   │                                    │  └─ Log: "[CALLBACK] Botón presionado..."
   │                                    │
   │                                    ├─ await q.answer()  ← CRÍTICO
   │←────── q.answer() response ────────┤
   │ (Spinner desaparece)               │
   │                                    ├─ Parsea: sym="BTCUSDT"
   │                                    │  └─ Log: "[CLOSE] Solicitud de cierre"
   │                                    │
   │                                    ├─ Llama: try_close_trade()
   │                                    │  └─ Log: "[CLOSE] Resultado: True"
   │                                    │
   │                                    ├─ Prepara nuevo texto
   │                                    │
   │                                    ├─ q.edit_message_text()
   │←──── Mensaje actualizado ──────────┤
   │ Usuario ve: "✅ BTCUSDT cerrado"   │
   │
```

---

## 🎯 Validación de callback_data

Todos los botones deben tener `callback_data` válido:

```python
# ✅ VÁLIDO (< 64 bytes)
InlineKeyboardButton("Cerrar", callback_data="close:BTCUSDT")

# ❌ INVÁLIDO (> 64 bytes)
InlineKeyboardButton("Cerrar", callback_data="close:" + "VERYLONGSTRINGHERE" * 10)
```

**El máximo es 64 bytes**. Si excedes, Telegram rechaza silenciosamente el botón.

---

## 🚨 Errores Críticos a Buscar en Logs

### ERROR 1: `[CALLBACK] ❌ CRÍTICO: Error en q.answer()`
- **Causa**: Problema con credenciales de Telegram
- **Solución**: Verifica `TELEGRAM_BOT_TOKEN` en `.env`

### ERROR 2: `[CLOSE] ERROR: BOT_INSTANCE no disponible`
- **Causa**: El bot principal está reiniciando
- **Solución**: Espera a que se inicie completamente

### ERROR 3: `[CALLBACK] ❌ ERROR NO CAPTURADO EN LÓGICA`
- **Causa**: Excepción en algún punto del callback
- **Solución**: Busca el trace completo en `logs/telegram_errors.log`

### ERROR 4: No ves `[CALLBACK]` cuando presionas botón
- **Causa**: `CallbackQueryHandler` no está registrado
- **Solución**: Reinicia el bot, verifica que `app.add_handler(CallbackQueryHandler(handle_callback))` existe

---

## 📋 Checklist de Debug

Cuando algo no funciona, revisa esto en orden:

- [ ] ¿Ves `[CALLBACK] Botón presionado` en los logs?
  - ❌ NO → El callback no se registró. Reinicia bot.
  - ✅ SÍ → Continúa.

- [ ] ¿Ves `[CALLBACK] ✅ ANSWER ENVIADO`?
  - ❌ NO → Error crítico. Verifica token.
  - ✅ SÍ → El spinner debería haber desaparecido.

- [ ] ¿Ves la lógica del botón (ej: `[CLOSE]`)?
  - ❌ NO → Hay error antes de llegar ahí.
  - ✅ SÍ → Continúa.

- [ ] ¿Ves `✅` al final (ej: `[CLOSE] ✅ Cerrado`)?
  - ✅ SÍ → ¡Todo funcionó!
  - ❌ NO → Hubo error en la lógica. Busca el error específico.

---

## 🔧 Cómo Ver Logs en Tiempo Real

### Windows PowerShell:
```powershell
# Ver últimas 50 líneas
Get-Content logs\telegram_full.log -Tail 50

# Ver en tiempo real (como tail -f)
Get-Content logs\telegram_full.log -Tail 20 -Wait
```

### Linux/Mac:
```bash
tail -f logs/telegram_full.log
```

### VS Code (Recomendado):
1. Abre carpeta: `logs/`
2. Haz clic en `telegram_full.log`
3. Scroll al final (arriba aparece contador de líneas)
4. Observa nuevas líneas mientras interactúas

---

## 📊 Niveles de Log

En `telegram_logger.py` puedes cambiar el nivel:

```python
logger.setLevel(logging.DEBUG)  # Actual - muy detallado
```

Opciones:
- `logging.DEBUG` → Cada función, variable, decisión (actual)
- `logging.INFO` → Solo eventos importantes
- `logging.WARNING` → Solo problemas
- `logging.ERROR` → Solo errores graves

Para máximas detalles (recomendado mientras debuggeas): _mantén `DEBUG`_

---

## 🎓 Resumen: Cómo Funciona

1. **Presionas botón en Telegram**
2. **Telegram envía `callback_query` a tu bot**
3. **Tu bot DEBE hacer `await q.answer()` inmediatamente**
   - Si no lo hace → spinner infinito
   - Si lo hace → spinner desaparece (usuario ve que funcionó)
4. **Luego tu bot procesa la lógica**
5. **Edita el mensaje con resultados**

**Lo más importante**: `await q.answer()` PRIMERO, todo lo demás DESPUÉS.

---

## ✅ Verificación Final

Presiona cualquier botón en Telegram y deberías ver:

```
[CALLBACK] Botón presionado - usuario: XXXX, data: 'XXX'
[CALLBACK] ✅ ANSWER ENVIADO - spinner bajo control
... lógica específica ...
```

Si ves estas dos primeras líneas, **el spinner está bajo control**. Lo demás es detalles de la lógica.

---

**Creado**: 2026-03-06  
**Versión**: 1.0  
**Estado**: ✅ Aplicado a tg_controller.py
