#!/usr/bin/env python3
"""
QUICK START - Bot Autoaprendiente 🤖

Este script inicia el bot en modo demostración.
Verifica que todo está correctamente configurado.
"""

import sys
import os
import time

# Colores para terminal
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    END = '\033[0m'
    BOLD = '\033[1m'

def print_header():
    print(f"\n{Colors.BOLD}{Colors.BLUE}")
    print("=" * 70)
    print("🤖 BOT DE TRADING AUTOAPRENDIENTE")
    print("=" * 70)
    print(f"{Colors.END}\n")

def check_requirement(name, check_func, critical=True):
    """Verifica un requerimiento"""
    try:
        result = check_func()
        if result:
            print(f"{Colors.GREEN}✅ {name}{Colors.END}")
            return True
        else:
            status = f"{Colors.RED}❌ {name}{Colors.END}" if critical else f"{Colors.YELLOW}⚠️  {name}{Colors.END}"
            print(status)
            return result
    except Exception as e:
        status = f"{Colors.RED}❌ {name}: {e}{Colors.END}" if critical else f"{Colors.YELLOW}⚠️  {name}: {e}{Colors.END}"
        print(status)
        return False

def main():
    print_header()
    
    print(f"{Colors.BOLD}1. Verificando Dependencias...{Colors.END}")
    print("-" * 70)
    
    all_ok = True
    
    # Check 1: Python version
    all_ok &= check_requirement(
        "Python 3.10+",
        lambda: sys.version_info >= (3, 10),
        critical=True
    )
    
    # Check 2: Virtual environment
    all_ok &= check_requirement(
        "Virtual environment activo",
        lambda: hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix),
        critical=False
    )
    
    # Check 3: Required imports
    print(f"\n{Colors.BOLD}2. Checking Module Imports...{Colors.END}")
    print("-" * 70)
    
    modules_required = [
        ("ai_filter", "AI Filter (DeepSeek)"),
        ("ai_memory", "Memory Database"),
        ("bot_autonomous", "Bot Motor"),
        ("bybit_client", "Bybit Client"),
        ("learning_engine", "Learning Engine"),
        ("news_engine", "Noticias"),
        ("momentum_engine", "Análisis Técnico"),
        ("telegram_commands", "Telegram Interface"),
    ]
    
    for mod_name, mod_desc in modules_required:
        try:
            __import__(mod_name)
            print(f"{Colors.GREEN}✅ {mod_desc}{Colors.END}")
        except ImportError as e:
            print(f"{Colors.RED}❌ {mod_desc}: {e}{Colors.END}")
            all_ok = False
    
    # Check 3: Environment variables
    print(f"\n{Colors.BOLD}3. Verificando Configuración...{Colors.END}")
    print("-" * 70)
    
    required_env = {
        "DEEPSEEK_API_KEY": "DeepSeek API",
        "BYBIT_API_KEY": "Bybit API",
        "BYBIT_API_SECRET": "Bybit Secret",
        "TELEGRAM_BOT_TOKEN": "Telegram Bot Token",
        "TELEGRAM_CHAT_ID": "Telegram Chat ID"
    }
    
    for env_var, desc in required_env.items():
        value = os.getenv(env_var)
        if value:
            masked = value[:10] + "..." if len(str(value)) > 10 else value
            print(f"{Colors.GREEN}✅ {desc}: {masked}{Colors.END}")
        else:
            print(f"{Colors.RED}❌ {desc}: NO CONFIGURADO{Colors.END}")
            all_ok = False
    
    # Check 4: Database
    print(f"\n{Colors.BOLD}4. Verificando Base de Datos...{Colors.END}")
    print("-" * 70)
    
    try:
        import ai_memory
        # Try to connect to DB
        conn = ai_memory._connect()
        
        # Check tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        
        table_names = [t[0] for t in tables]
        
        if "ai_decisions" in table_names:
            print(f"{Colors.GREEN}✅ Tabla ai_decisions{Colors.END}")
        else:
            print(f"{Colors.YELLOW}⚠️  Tabla ai_decisions (se creará en primer trade){Colors.END}")
        
        if "trade_outcomes" in table_names:
            print(f"{Colors.GREEN}✅ Tabla trade_outcomes{Colors.END}")
        else:
            print(f"{Colors.YELLOW}⚠️  Tabla trade_outcomes (se creará en primer trade){Colors.END}")
        
        conn.close()
        
    except Exception as e:
        print(f"{Colors.YELLOW}⚠️  BD: {e}{Colors.END}")
    
    # Final status
    print(f"\n{Colors.BOLD}5. Resumen{Colors.END}")
    print("-" * 70)
    
    if all_ok:
        print(f"{Colors.GREEN}{Colors.BOLD}✅ TODO OK - BOT LISTO PARA EJECUTAR{Colors.END}\n")
        print(f"Próximos pasos:")
        print(f"  1. Inicia el bot:")
        print(f"     {Colors.BLUE}python main.py{Colors.END}")
        print(f"")
        print(f"  2. En Telegram, escribe: /start")
        print(f"")
        print(f"  3. El bot comenzará a scanear mercados")
        print(f"     Cuando abra un trade:")
        print(f"       •️ Verás \"🟢 TRADE ABIERTO\"")
        print(f"       •️ Cuando se cierre: \"✅ CERRADO\"")
        print(f"")
        print(f"  4. Ver learning stats:")
        print(f"     {Colors.BLUE}pytest")
        print(f"     /aprend  {Colors.END}→ Resumen de aprendizaje (7 días)")
        print(f"     /noticias → Impacto de noticias")
        print(f"     /accuracy → Precisión de la IA")
        print(f"")
        print(f"  5. Para más info:")
        print(f"     {Colors.BLUE}cat LEARNING_GUIDE.md{Colors.END}")
        print(f"     {Colors.BLUE}cat STATUS_FINAL.md{Colors.END}")
        return 0
    else:
        print(f"{Colors.RED}{Colors.BOLD}⚠️  ERRORES DETECTADOS{Colors.END}")
        print(f"Por favor revisa la configuración en .env")
        print(f"Referencia: https://github.com/[usuario]/trading-bot#setup")
        return 1

if __name__ == "__main__":
    sys.exit(main())
