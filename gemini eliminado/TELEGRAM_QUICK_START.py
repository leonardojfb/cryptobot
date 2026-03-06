#!/usr/bin/env python3
"""
INSTRUCCIONES RÁPIDAS - Sistema de Logging para Botones de Telegram

Ejecuta esto primero para verificar todo está bien:
    python verify_telegram_setup.py

Luego ejecuta tu bot:
    python main_bot.py

Los logs aparecerán automáticamente en:
    logs/telegram_full.log        (todos los eventos)
    logs/telegram_errors.log      (solo errores)
"""

import os
import sys

def quick_start():
    """Guía rápida de inicio"""
    
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║                    LOGGING DE TELEGRAM - INICIO RÁPIDO               ║
╚══════════════════════════════════════════════════════════════════════╝

📋 ARCHIVOS CREADOS:
  • telegram_logger.py           - Sistema de logging profesional
  • tg_controller.py             - Actualizado con [CALLBACK], [CLOSE], etc.
  • TELEGRAM_LOGS_GUIDE.md       - Guía detallada (LEER ESTO!)
  • verify_telegram_setup.py     - Verificador de configuración
  • TELEGRAM_LOGGING_SUMMARY.md  - Resumen de cambios

🚀 USAR AHORA:

  1️⃣  Verificar configuración:
      .venv\\Scripts\\python.exe verify_telegram_setup.py

  2️⃣  Ejecutar el bot:
      python main_bot.py

  3️⃣  Presionar botones en Telegram:
      • Envía /pos
      • Presiona "❌ Cerrar BTCUSDT" o "🔄 Actualizar"

  4️⃣  Revisar logs en tiempo real:
      • Windows: type logs\\telegram_full.log | more
      • Linux:  tail -f logs/telegram_full.log

📊 QUÉ VERÁS EN LOS LOGS:

  [CALLBACK]  - Bot ó presionado, qué usuario, qué datos
  [CLOSE]     - Intento de cierre de posición y resultado
  [REFRESH]   - Actualización de PnL y detalles
  [INIT]      - Inicialización del bot y polling
  [NOTIF]     - Cambios en preferencias de notificaciones

🔍 EJEMPLO DE LOGS NORMALES:

  [CALLBACK] Usuario 8172390917 presionó: close:BTCUSDT
  [CLOSE] Ejecutando try_close_trade para BTCUSDT
  [CLOSE] Resultado: True
  [CLOSE] ✅ BTCUSDT cerrado exitosamente

❌ SI ALGO NO FUNCIONA:

  • No ves logs → Verifica que telegram_logger.py esté en el mismo directorio
  • Botón no responde → Busca [CALLBACK] en los logs
  • Error al cerrar → Busca [CLOSE] Error en logs/telegram_errors.log

📖 MÁS INFORMACIÓN:
   Lee TELEGRAM_LOGS_GUIDE.md para:
   • Configuración avanzada
   • Troubleshooting detallado
   • Cómo compartir logs para debug

✅ CHECKLIST ANTES DE EMPEZAR:

    ☐ telegram_logger.py en el mismo directorio que tg_controller.py
    ☐ python-telegram-bot instalado (pip install python-telegram-bot)
    ☐ TELEGRAM_BOT_TOKEN configurado en .env
    ☐ TELEGRAM_CHAT_ID configurado en .env
    ☐ Carpeta logs/ creada (se crea automáticamente)

═══════════════════════════════════════════════════════════════════════

ESTÁS LISTO PARA USAR EL LOGGING DE TELEGRAM 🎉

    """)

if __name__ == "__main__":
    quick_start()
    
    # Ofrecer ejecutar el verificador
    response = input("\n¿Buscas verificar la configuración ahora? (s/n): ").lower().strip()
    if response in ['s', 'si', 'yes', 'y']:
        os.system('.venv\\Scripts\\python.exe verify_telegram_setup.py')
