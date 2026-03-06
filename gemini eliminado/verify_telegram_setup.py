#!/usr/bin/env python3
"""
verify_telegram_setup.py - Verificar que el logging de Telegram está configurado correctamente
"""

import os
import sys

def verify_setup():
    print("\n" + "="*70)
    print("VERIFICACIÓN DE CONFIGURACIÓN DE TELEGRAM LOGGING")
    print("="*70 + "\n")
    
    checks_passed = 0
    checks_total = 0
    
    # Check 1: telegram_logger.py existe
    checks_total += 1
    if os.path.exists("telegram_logger.py"):
        print("✅ Check 1: telegram_logger.py existe")
        checks_passed += 1
    else:
        print("❌ Check 1: telegram_logger.py NO EXISTE")
        print("   Solución: Debe estar en el mismo directorio que tg_controller.py")
    
    # Check 2: tg_controller.py fue actualizado
    checks_total += 1
    try:
        with open("tg_controller.py", 'r', encoding='utf-8') as f:
            content = f.read()
            if "[CALLBACK]" in content and "[INIT]" in content and "[CLOSE]" in content:
                print("✅ Check 2: tg_controller.py tiene logging agregado")
                checks_passed += 1
            else:
                print("❌ Check 2: tg_controller.py NO tiene logs agregados")
    except Exception as e:
        print(f"❌ Check 2: Error leyendo tg_controller.py: {e}")
    
    # Check 3: TELEGRAM_BOT_TOKEN configurado
    checks_total += 1
    try:
        from dotenv import load_dotenv
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if token and len(token) > 10:
            print("✅ Check 3: TELEGRAM_BOT_TOKEN configurado")
            print(f"   Token (primeros 20 caracteres): {token[:20]}...")
            checks_passed += 1
        else:
            print("⚠️  Check 3: TELEGRAM_BOT_TOKEN vacío o muy corto")
            print("   Solución: Configura TELEGRAM_BOT_TOKEN en .env")
    except Exception as e:
        print(f"❌ Check 3: Error: {e}")
    
    # Check 4: TELEGRAM_CHAT_ID configurado
    checks_total += 1
    try:
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if chat_id:
            print("✅ Check 4: TELEGRAM_CHAT_ID configurado")
            print(f"   Chat ID: {chat_id}")
            checks_passed += 1
        else:
            print("⚠️  Check 4: TELEGRAM_CHAT_ID vacío")
            print("   Solución: Configura TELEGRAM_CHAT_ID en .env")
    except Exception as e:
        print(f"❌ Check 4: Error: {e}")
    
    # Check 5: python-telegram-bot instalado
    checks_total += 1
    try:
        import telegram
        print("✅ Check 5: python-telegram-bot instalado")
        print(f"   Versión: {telegram.__version__}")
        checks_passed += 1
    except ImportError:
        print("❌ Check 5: python-telegram-bot NO instalado")
        print("   Solución: pip install python-telegram-bot")
    
    # Check 6: Crear carpeta logs/
    checks_total += 1
    try:
        os.makedirs("logs", exist_ok=True)
        print("✅ Check 6: Carpeta logs/ está disponible")
        checks_passed += 1
    except Exception as e:
        print(f"❌ Check 6: Error creando logs/: {e}")
    
    # Check 7: Importar telegram_logger
    checks_total += 1
    try:
        from telegram_logger import get_telegram_logger
        print("✅ Check 7: telegram_logger.py se importa correctamente")
        checks_passed += 1
    except ImportError as e:
        print(f"⚠️  Check 7: No se puede importar telegram_logger: {e}")
        print("   Esto puede deberse a faltas de dependencias, pero el bot intentará usar logging estándar")
    
    # Check 8: Probar logging
    checks_total += 1
    try:
        from telegram_logger import get_telegram_logger
        log = get_telegram_logger()
        log.info("[VERIFY] Test de logging exitoso")
        print("✅ Check 8: Sistema de logging funciona correctamente")
        print("   Los logs se guardarán en logs/telegram_full.log")
        checks_passed += 1
    except Exception as e:
        print(f"⚠️  Check 8: Error probando logging: {e}")
        print("   El bot usará logging estándar en consola")
    
    # Resumen
    print("\n" + "="*70)
    print(f"RESUMEN: {checks_passed}/{checks_total} checks completados")
    print("="*70 + "\n")
    
    if checks_passed == checks_total:
        print("🎉 ¡EXCELENTE! Todo está configurado correctamente.")
        print("\nPróximos pasos:")
        print("1. Ejecuta: python main_bot.py")
        print("2. Presiona botones en Telegram")
        print("3. Revisa los logs en logs/telegram_full.log")
        print("\nVe la guía en TELEGRAM_LOGS_GUIDE.md para más detalles.")
        return True
    elif checks_passed >= checks_total - 2:
        print("⚠️  Algunos checks no pasaron, pero el bot debería funcionar.")
        print("   Soluciona los problemas marcados con ❌")
        return True
    else:
        print("❌ Hay problemas de configuración.")
        print("   Por favor solucion los checks que fallaron.")
        return False

if __name__ == "__main__":
    success = verify_setup()
    sys.exit(0 if success else 1)
