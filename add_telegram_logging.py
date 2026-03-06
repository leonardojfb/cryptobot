#!/usr/bin/env python3
"""
Script para agregar logging detallado a tg_controller.py
para debuggear problemas con botones de Telegram.
"""

import re

# Leer el archivo
with open('tg_controller.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Agregar logging en la sección de refresh
refresh_pattern = r'(\s+)# ── Actualizar posición ───────────────────────────────────────────────\n(\s+)elif data\.startswith\("refresh:"\):\n(\s+)sym = data\[8:\]\n'

refresh_replacement = r'''\1# ── Actualizar posición ───────────────────────────────────────────────
\2elif data.startswith("refresh:"):
\3sym = data[8:]
\3log.info(f"[REFRESH] Usuario {user_id} solicita actualizar {sym}")
'''

content = re.sub(refresh_pattern, refresh_replacement, content)

# 2. Agregar logging en notificaciones
notif_pattern = r'(\s+)elif data\.startswith\("notif:"\):\n(\s+)cat = data\[6:\]\n'

notif_replacement = r'''\1elif data.startswith("notif:"):
\2cat = data[6:]
\2log.info(f"[NOTIF] Usuario {user_id} toggle notif: {cat}")
'''

content = re.sub(notif_pattern, notif_replacement, content)

# 3. Agregar logging en run_telegram_bot
run_pattern = r'(def run_telegram_bot\(bot_instance\):.*?\n)(.*?TG_AVAILABLE)'

def run_replacement(match):
    func_start = match.group(1)
    rest = match.group(2)
    return func_start + '    log.info("[INIT] Iniciando Telegram bot controller")\n    ' + rest

content = re.sub(run_pattern, run_replacement, content, flags=re.DOTALL)

# 4. Agregar logging en CommandHandler
cmd_pattern = r'(for cmd, _ in COMMAND_LIST:)'

cmd_replacement = r'''log.info("[INIT] Registrando handlers de comandos")
    \1'''

content = re.sub(cmd_pattern, cmd_replacement, content)

# 5. Agregar logging en app.run_polling()
polling_pattern = r'(app\.run_polling\(\))'

polling_replacement = r'''log.info("[INIT] Telegram polling iniciado - esperando interacciones")
    \1'''

content = re.sub(polling_pattern, polling_replacement, content)

# Escribir el archivo actualizado
with open('tg_controller.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ Logging agregado a tg_controller.py")
print("\nCambios realizados:")
print("  - [REFRESH] logs en actualización de posiciones")
print("  - [NOTIF] logs en cambios de preferencias")
print("  - [INIT] logs en inicialización del bot")
print("  - [CALLBACK] logs existentes mejorados")
print("  - [CLOSE] logs existentes mejorados")
