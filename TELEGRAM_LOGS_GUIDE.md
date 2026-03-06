# 🔧 Guía: Debuggear Botones de Telegram

## Problema: Los botones de Telegram no responden

He configurado **logging detallado** en tu bot de Telegram para que puedas ver exactamente qué está pasando cuando presionas un botón.

---

## 📊 Archivos de Log

Después de correr tu bot, se crearán automáticamente en la carpeta `logs/`:

### 1. `telegram_full.log` 
- **Todos** los eventos (DEBUG, INFO, WARNING, ERROR)
- Muy detallado - útil para diagnóstico completo
- Tamaño máximo: 10 MB (después rota automáticamente)

### 2. `telegram_errors.log`
- **Solo** WARNING y ERROR
- Enfócate aquí si hay problemas
- Tamaño máximo: 5 MB

### 3. Consola
- INFO+ se imprime en tiempo real mientras el bot corre

---

## 🔍 Puntos de Logging Agregados

### Para CIERRE de posiciones
```
[CLOSE] Solicitud de cierre del símbolo: BTCUSDT
[CLOSE] Ejecutando try_close_trade para BTCUSDT
[CLOSE] Resultado de cierre: True
[CLOSE] ✅ BTCUSDT cerrado exitosamente desde Telegram
```

### Para ACTUALIZACIÓN de posiciones (refresh)
```
[REFRESH] Usuario 8172390917 solicita actualizar ETHUSDT
[REFRESH] Formateando detalles de posición para ETHUSDT
[REFRESH] Editando mensaje para ETHUSDT
[REFRESH] OK: ETHUSDT actualizado
```

### Para BOTONES (Telegram)
```
[CALLBACK] Botón presionado - usuario: 8172390917, data: 'close:BTCUSDT'
[CALLBACK] q.answer() ejecutado correctamente para 'close:BTCUSDT'
```

### Para INICIALIZACIÓN
```
[INIT] Iniciando Telegram bot controller...
[INIT] BOT_INSTANCE asignado correctamente
[INIT] Registrando 30 comandos...
[INIT] Registrando CallbackQueryHandler para botones inline
[INIT] ⏳ Polling iniciado - esperando mensajes y interacciones de usuarios...
```

---

## 🚀 Cómo Usar los Logs para Debuggear

### Paso 1: Inicia el bot normalmente
```bash
python main_bot.py
```

### Paso 2: Prueba un botón en Telegram
1. Abre Telegram
2. Envía `/pos` al bot
3. Presiona uno de los botones (ej: "❌ Cerrar BTCUSDT")

### Paso 3: Revisa los logs
```bash
# Ver en tiempo real (solo INFO+)
tail -f logs/telegram_full.log

# O en Windows PowerShell:
Get-Content logs/telegram_full.log -Tail 20 -Wait
```

---

## 🐛 Problemas Comunes y Soluciones

### Problema: Botón presionado pero no pasa nada
**Qué buscar en los logs:**
```
[CALLBACK] Botón presionado - usuario: ..., data: 'close:BTCUSDT'
```

Si **NO** ves esto → El callback no se registró correctamente:
- Verifica que `CallbackQueryHandler` está presente en `tg_controller.py` línea ~1135
- Reinicia el bot

### Problema: "Bot no disponible"
**Log:**
```
[CLOSE] BOT_INSTANCE no disponible
```

**Soluciones:**
- El bot se está reiniciando. Espera a que vuelva online
- Verifica que `set_bot(bot_instance)` se llama correctamente

### Problema: Posición no se cierra
**Log:**
```
[CLOSE] Resultado de cierre: False
```

**Qué significa:**
- La posición no existe (ya fue cerrada)
- Hay un error en `BOT_INSTANCE.try_close_trade()`
- El símbolo está mal formateado

**Qué hacer:**
- Verifica en `/pos` que la posición existe
- Revisa que el símbolo tiene el formato correcto (BTCUSDT, no BTC)

### Problema: Logs no se crean
**Soluciones:**
1. Verifica que `telegram_logger.py` está en el mismo directorio que `tg_controller.py`
2. Si lo importa fallida, usará logging estándar (verás logs en consola)
3. Crea la carpeta `logs/` manualmente:
   ```bash
   mkdir logs
   ```

---

## 📝 Ejemplo: Seguir un Cierre de Posición

Cuando presiones el botón "❌ Cerrar BTCUSDT", los logs deberían mostrar esto en orden:

```
[CALLBACK] Botón presionado - usuario: 8172390917, data: 'close:BTCUSDT'
[CALLBACK] q.answer() ejecutado correctamente para 'close:BTCUSDT'
[CLOSE] Solicitud de cierre del símbolo: BTCUSDT
[CLOSE] Ejecutando try_close_trade para BTCUSDT
[CLOSE] Resultado de cierre: True
[CLOSE] ✅ BTCUSDT cerrado exitosamente desde Telegram
```

Si faltas alguno → ahí está el problema.

---

## 🔧 Configurar Nivel de Log

**En `telegram_logger.py`**, línea ~25, puedes cambiar:

```python
logger.setLevel(logging.DEBUG)  # Cambia esto
```

- `logging.DEBUG` → Altamente detallado (por defecto)
- `logging.INFO` → Solo info importante
- `logging.WARNING` → Solo problemas
- `logging.ERROR` → Solo errores graves

---

## 📤 Cómo Compartir Logs Conmigo

Si necesitas ayuda para debuggear:

1. **Reproduce el problema:**
   ```bash
   python main_bot.py
   # Presiona los botones
   # Espera 30 segundos
   ```

2. **Captura los logs:**
   ```bash
   # Copiar archivos log completos
   cp logs/telegram_full.log telegram_logs_debug.log
   ```

3. **Comparte conmigo:**
   - El archivo `telegram_logs_debug.log`
   - Una descripción de qué botón presionaste y qué pasó/no pasó

---

## ✅ Verificación Rápida

**Comando para ver si los logs funcionan:**
```bash
# Crear un pequeño test
python -c "from telegram_logger import get_telegram_logger; log = get_telegram_logger(); log.info('[TEST] Logging funciona!'); log.warning('[TEST] Warning test'); log.error('[TEST] Error test')"
```

Deberías ver archivos creados en `logs/` con el contenido de test.

---

## 🎯 Resumen de Cambios Realizados

✅ Agregado logging en `handle_callback()`:
- Usuario ID
- Datos del botón
- Confirmation de q.answer()

✅ Agregado logging en funciones de cierre:
- Solicitud de cierre
- Resultado del try_close_trade
- Éxito o fallo

✅ Agregado logging en actualización:
- Solicitud de refresh
- Formateo de datos
- Edición de mensaje

✅ Agregado logging de inicialización:
- Seteo de BOT_INSTANCE
- Registro de handlers
- Inicio de polling

✅ Creado `telegram_logger.py`:
- Logging configurado profesional
- Rotación automática de archivos
- Dos niveles de detalle

---

## 📞 Siguientes Pasos

1. **Ejecuta el bot con el nuevo logging**
2. **Presiona los botones en Telegram** (intenta cerrar una posición)
3. **Revisa los logs** en `logs/telegram_full.log`
4. **Comparte conmigo** si ves errores o comportamientos raros

¡Con estos logs, podremos identificar exactamente dónde está el problema! 🔍
