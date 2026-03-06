"""
notify_prefs.py — Preferencias de notificaciones de Telegram
Persiste en notify_prefs.json. El usuario las controla desde el bot.

Categorías:
  trades        — apertura/cierre de trades
  signals       — señales analizadas (FLAT incluido o no)
  analysis      — análisis técnico detallado por par
  news          — alertas de noticias importantes
  ai_decisions  — qué decidió DeepSeek y por qué
  positions     — actualizaciones periódicas de posiciones abiertas
  performance   — resúmenes diarios de rendimiento
  risk          — alertas del risk manager (circuit breaker, etc.)
  dev           — errores, fallos de API, trades que no se abrieron, logs
"""

import json, logging, os, threading
from typing import Dict

log = logging.getLogger("notify_prefs")
PREFS_FILE = os.getenv("NOTIFY_PREFS_FILE", "notify_prefs.json")

# Valores por defecto: solo lo esencial activo
DEFAULTS: Dict[str, bool] = {
    "trades":       True,   # apertura y cierre de trades
    "signals":      False,  # señales técnicas detectadas
    "analysis":     False,  # análisis técnico detallado (verboso)
    "news":         False,  # alertas de noticias
    "ai_decisions": True,   # rechazos/aprobaciones de DeepSeek
    "positions":    False,  # updates periódicos de posiciones abiertas
    "performance":  True,   # resumen diario
    "risk":         True,   # circuit breaker, stop-loss grupal
    "dev":          False,  # errores, fallos de API, debug logs
}

# Descripciones para el menú
DESCRIPTIONS: Dict[str, str] = {
    "trades":       "🔔 Apertura y cierre de trades",
    "signals":      "📡 Señales técnicas detectadas",
    "analysis":     "📊 Análisis detallado por par (verboso)",
    "news":         "📰 Alertas de noticias importantes",
    "ai_decisions": "🤖 Decisiones de DeepSeek (aprobados/rechazados)",
    "positions":    "📈 Updates periódicos de posiciones abiertas",
    "performance":  "🏆 Resumen diario de rendimiento",
    "risk":         "🛡️ Alertas del gestor de riesgo",
    "dev":          "🔧 Errores y logs de desarrollo",
}

_lock  = threading.Lock()
_prefs: Dict[str, bool] = {}


def _load() -> Dict[str, bool]:
    if os.path.exists(PREFS_FILE):
        try:
            with open(PREFS_FILE) as f:
                saved = json.load(f)
            # Merge con defaults (nuevas categorías futuras)
            return {**DEFAULTS, **saved}
        except Exception as e:
            log.warning(f"notify_prefs load error: {e}")
    return dict(DEFAULTS)


def _save():
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(_prefs, f, indent=2)
    except Exception as e:
        log.error(f"notify_prefs save error: {e}")


def init():
    global _prefs
    with _lock:
        _prefs = _load()
    log.info(f"notify_prefs cargadas: {sum(_prefs.values())}/{len(_prefs)} activas")


def is_enabled(category: str) -> bool:
    """Retorna True si la categoría está habilitada."""
    with _lock:
        return _prefs.get(category, DEFAULTS.get(category, False))


def set_pref(category: str, enabled: bool) -> bool:
    """Cambia una preferencia. Retorna False si la categoría no existe."""
    if category not in DEFAULTS:
        return False
    with _lock:
        _prefs[category] = enabled
        _save()
    return True


def toggle(category: str) -> bool:
    """Alterna una preferencia. Retorna el nuevo estado."""
    with _lock:
        current = _prefs.get(category, DEFAULTS.get(category, False))
        _prefs[category] = not current
        _save()
        return _prefs[category]


def get_all() -> Dict[str, bool]:
    with _lock:
        return dict(_prefs)


def reset_defaults():
    global _prefs
    with _lock:
        _prefs = dict(DEFAULTS)
        _save()


# Inicializar al importar
init()
