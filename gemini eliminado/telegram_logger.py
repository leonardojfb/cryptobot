"""
telegram_logger.py - Configuración de logging para Telegram bot

Este módulo configura logs detallados para:
- Todas las interacciones con botones
- Estados de la aplicación
- Errores y excepciones
- Métricas de rendimiento

Agregarlo a tu bot para obtener logs de debug profundos.
"""

import logging
import logging.handlers
import os
from datetime import datetime

def setup_telegram_logging(log_dir: str = "logs") -> logging.Logger:
    """
    Configura logging detallado para el bot de Telegram.
    
    Crea dos archivos de log:
    - telegram_full.log: Todos los eventos (DEBUG+)
    - telegram_errors.log: Solo errores y warnings
    
    Args:
        log_dir: Directorio donde guardar los logs
    
    Returns:
        Logger configurado para usar en tg_controller.py
    """
    
    # Crear directorio si no existe
    os.makedirs(log_dir, exist_ok=True)
    
    logger = logging.getLogger("tg_controller")
    logger.setLevel(logging.DEBUG)
    
    # Evitar handlers duplicados
    if logger.handlers:
        return logger
    
    # Formato detallado
    detailed_format = logging.Formatter(
        '[%(asctime)s] %(name)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. Handler para archivo completo (rotación por tamaño)
    full_log_file = os.path.join(log_dir, "telegram_full.log")
    full_handler = logging.handlers.RotatingFileHandler(
        full_log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding='utf-8'
    )
    full_handler.setLevel(logging.DEBUG)
    full_handler.setFormatter(detailed_format)
    logger.addHandler(full_handler)
    
    # 2. Handler para errores solamente
    error_log_file = os.path.join(log_dir, "telegram_errors.log")
    error_handler = logging.handlers.RotatingFileHandler(
        error_log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(detailed_format)
    logger.addHandler(error_handler)
    
    # 3. Handler para consola (INFO+)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '[%(levelname)s] %(name)s - %(message)s'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # Log inicial
    logger.info("="*70)
    logger.info("TELEGRAM BOT LOGGING INICIALIZADO")
    logger.info(f"Fecha/Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Logs completos: {full_log_file}")
    logger.info(f"Solo errores: {error_log_file}")
    logger.info("="*70)
    
    return logger


def get_telegram_logger() -> logging.Logger:
    """
    Obtiene el logger configurado para Telegram.
    Si no está configurado, lo configura automáticamente.
    """
    logger = logging.getLogger("tg_controller")
    if not logger.handlers:
        setup_telegram_logging()
    return logger


# Para uso en tg_controller.py, simplemente importa:
# from telegram_logger import get_telegram_logger
# log = get_telegram_logger()

if __name__ == "__main__":
    # Test: crear logs de ejemplo
    log = setup_telegram_logging()
    
    log.debug("[TEST] Este es un mensaje de DEBUG")
    log.info("[TEST] Este es un mensaje de INFO")
    log.warning("[TEST] Este es un mensaje de WARNING")
    log.error("[TEST] Este es un mensaje de ERROR")
    
    print("\n✅ Archivos de log creados en directorio 'logs/'")
    print("   - telegram_full.log (todos los eventos)")
    print("   - telegram_errors.log (solo errores)")
