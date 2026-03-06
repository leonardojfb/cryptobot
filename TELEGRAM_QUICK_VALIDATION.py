#!/usr/bin/env python3
"""
🎯 QUICK VALIDATION - Verifica que todo está funcionando
Ejecuta esto después de presionar botones en Telegram para confirmación rápida
"""

import os
import sys
from datetime import datetime, timedelta

def check_logs():
    """Verifica que hay logs recientes"""
    
    if not os.path.exists("logs/telegram_full.log"):
        print("❌ No se encontró logs/telegram_full.log")
        print("   El bot no ha creado logs. ¿Está corriendo?")
        return False
    
    # Verificar que el archivo tiene contenido reciente
    file_stat = os.stat("logs/telegram_full.log")
    file_time = datetime.fromtimestamp(file_stat.st_mtime)
    now = datetime.now()
    
    age_seconds = (now - file_time).total_seconds()
    
    if age_seconds < 60:
        print(f"✅ Logs recientes (hace {int(age_seconds)} segundos)")
        return True
    elif age_seconds < 3600:
        print(f"⚠️  Logs antiguos (hace {int(age_seconds/60)} minutos)")
        return True
    else:
        print(f"❌ Logs muy antiguos (hace {int(age_seconds/3600)} horas)")
        print("   Presiona botones en Telegram para generar logs recientes")
        return False


def check_for_critical_keywords():
    """Busca palabras clave de logs críticos"""
    
    keywords = {
        "[CALLBACK] ✅ ANSWER ENVIADO": "[CALLBACK] - q.answer() ejecutado",
        "[CALLBACK] ❌ CRÍTICO": "[CALLBACK] - Error en q.answer() (PROBLEMA)",
        "[CLOSE] Resultado de cierre": "[CLOSE] - Intento de cierre",
        "[REFRESH]": "[REFRESH] - Actualización de posición",
    }
    
    if not os.path.exists("logs/telegram_full.log"):
        return {}
    
    with open("logs/telegram_full.log", "r", encoding='utf-8') as f:
        content = f.read()
    
    found = {}
    for keyword, description in keywords.items():
        if keyword in content:
            # Contar ocurrencias
            count = content.count(keyword)
            found[keyword] = (count, description)
    
    return found


def validate_callback_safety():
    """Ejecuta validador de callbacks"""
    
    try:
        import telegram_callback_validator
        validator = telegram_callback_validator.CallbackValidator()
        
        # Redirect stdout para capturar output
        from io import StringIO
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        
        success = validator.validate_file("tg_controller.py")
        
        sys.stdout = old_stdout
        
        return success, len(validator.errors), len(validator.warnings)
    except Exception as e:
        return None, 0, 0


def run_quick_validation():
    """Ejecuta validación rápida"""
    
    print("\n" + "="*70)
    print("🔍 VALIDACIÓN RÁPIDA - ESTADO DE TELEGRAM LOGGING".center(70))
    print("="*70 + "\n")
    
    # Check 1: Logs existen
    print("1️⃣  Verificando logs...")
    logs_ok = check_logs()
    
    # Check 2: Contenido de logs
    print("\n2️⃣  Buscando eventos de botones...")
    logs = check_for_critical_keywords()
    
    if logs:
        for keyword, (count, desc) in logs.items():
            print(f"   ✅ {desc:40s} ({count} veces)")
    else:
        print("   ⚠️  Sin eventos de botones. ¿Has presionado botones en Telegram?")
    
    # Check 3: Validador callbacks
    print("\n3️⃣  Validando callback_data...")
    success, errors, warnings = validate_callback_safety()
    
    if success is None:
        print("   ⚠️  No se pudo ejecutar validador")
    else:
        if errors == 0:
            print(f"   ✅ Sin errores críticos en callbacks")
        else:
            print(f"   ❌ {errors} errores críticos en callbacks")
        
        if warnings > 0:
            print(f"   ℹ️  {warnings} advertencias (probablemente normales)")
    
    # Check 4: Archivo modificado
    print("\n4️⃣  Verificando cambios en tg_controller.py...")
    if os.path.exists("tg_controller.py"):
        with open("tg_controller.py", "r", encoding='utf-8') as f:
            content = f.read()
        
        checks = [
            ("await q.answer()", "Respuesta inmediata a callbacks"),
            ("[CALLBACK]", "Logs de callback"),
            ("[CLOSE]", "Logs de cierre"),
            ("[REFRESH]", "Logs de actualización"),
            ("CallbackQueryHandler(handle_callback)", "Handler registrado"),
        ]
        
        for check, desc in checks:
            if check in content:
                print(f"   ✅ {desc}")
            else:
                print(f"   ❌ FALTA: {desc}")
    
    # Resumen final
    print("\n" + "="*70)
    print("📊 RESUMEN".center(70))
    print("="*70 + "\n")
    
    if logs_ok and logs and errors == 0:
        print("🎉 ¡EXCELENTE! Todo está funcionando correctamente.")
        print("\n   ✅ Logs activos")
        print("   ✅ Botones disparando eventos")
        print("   ✅ Callbacks seguros")
        return True
    elif logs_ok:
        print("✅ Sistema parcialmente funcional.")
        print("\nPróximos pasos:")
        print("  1. Presiona más botones en Telegram")
        print("  2. Vuelve a ejecutar esta validación")
        print("  3. Si sigues sin ver eventos, revisa TELEGRAM_LOGS_GUIDE.md")
        return True
    else:
        print("⚠️  Algunos problemas detectados.")
        print("\nPróximos pasos:")
        print("  1. Verifica que el bot está corriendo: python main_bot.py")
        print("  2. Presiona botones en Telegram")
        print("  3. Espera 5 segundos y vuelve a ejecutar esta validación")
        return False


def print_usage():
    """Muestra cómo usar este script"""
    
    print("\n" + "📖 CÓMO USAR ESTE VALIDADOR".center(70))
    print("="*70 + "\n")
    print("1. Ejecuta tu bot normalmente:")
    print("   $ python main_bot.py")
    print("\n2. En otra terminal, abre Telegram y:")
    print("   • Envía /pos al bot")
    print("   • Presiona algunos botones (Cerrar, Actualizar, etc.)")
    print("   • Espera ~5 segundos")
    print("\n3. Vuelve a la terminal y ejecuta:")
    print("   $ .venv\\Scripts\\python.exe TELEGRAM_QUICK_VALIDATION.py")
    print("\n4. Revisa el resultado. Deberías ver:")
    print("   ✅ Logs recientes")
    print("   ✅ Eventos de botones capturados")
    print("   ✅ Sin errores en callbacks")
    print("\nSi hay problemas, revisa:")
    print("  • TELEGRAM_LOGS_GUIDE.md - Cómo debuggear")
    print("  • TELEGRAM_SPINNER_FIX_GUIDE.md - Guía técnica")
    print("  • logs/telegram_full.log - Log completo")
    print()


if __name__ == "__main__":
    
    # Mostrar instrucciones si se pasa --help
    if "--help" in sys.argv or "-h" in sys.argv:
        print_usage()
        sys.exit(0)
    
    # Ejecutar validación
    success = run_quick_validation()
    
    print_usage()
    
    sys.exit(0 if success else 1)
