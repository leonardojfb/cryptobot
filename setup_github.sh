#!/bin/bash
# setup_github.sh - Guardar y ejecutar

echo "🚀 Configurando proyecto para GitHub..."

# Colores
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

# Verificar que estamos en la carpeta correcta
if [ ! -f "main.py" ] && [ ! -f "momentum_engine.py" ]; then
    echo -e "${RED}❌ Error: No se encontraron archivos del bot${NC}"
    echo "Ejecuta este script desde la carpeta de tu bot"
    exit 1
fi

# 1. Crear .gitignore
echo "📝 Creando .gitignore..."
cat > .gitignore << 'EOF'
# Secrets
.env
config.py
secrets.py
*.key
*.pem
credentials.json

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg
.pytest_cache/
.coverage
htmlcov/

# Logs
*.log
logs/

# Database
*.db
*.sqlite3
*.sqlite

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db
desktop.ini

# Trading data
data/
backtests/
results/
EOF

# 2. Crear estructura de ejemplos
mkdir -p examples

# 3. Crear config_example.py
cat > examples/config_example.py << 'EOF'
"""
config_example.py

Copiar a config.py en la raíz y completar con tus credenciales.
NO subir config.py a GitHub (está en .gitignore)
"""

# Bybit API Credentials
# Obtener en: https://www.bybit.com/app/user/api-management
BYBIT_API_KEY = "YOUR_API_KEY_HERE"
BYBIT_API_SECRET = "YOUR_API_SECRET_HERE"
BYBIT_TESTNET = True  # True para paper trading, False para real

# Telegram Bot
# Crear bot con @BotFather en Telegram
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"  # Obtener con @userinfobot

# Trading Configuration
TRADING_CONFIG = {
    'max_positions': 5,
    'risk_per_trade': 0.01,      # 1% del balance por trade
    'min_confidence': 20,         # Confianza mínima para entrar
    'min_score': 2.0,             # Score de momentum mínimo
    'symbols': [
        'BTCUSDT',
        'ETHUSDT', 
        'SOLUSDT',
        'BNBUSDT',
        'XRPUSDT'
    ],
    
    # Momentum Settings
    'scan_interval': 2,           # Segundos entre scans
    'momentum_lookback': 20,      # Periodos para calcular momentum
    
    # Profit Taking
    'partial_tp_levels': [1.5, 3.0, 5.0, 8.0],  # R:R ratios
    'partial_tp_sizes': [0.25, 0.25, 0.25, 0.25],
    'trailing_activation': 2.0,   # Activar trailing después de 2R
    'trailing_distance': 0.5,     # Distancia del trailing en R
    'be_activation': 1.0,           # Break-even después de 1R
}

# Risk Management
RISK_CONFIG = {
    'max_drawdown_pct': 5,        # Detener bot si drawdown > 5%
    'daily_loss_limit': 3,        # Máximo trades perdedores por día
    'position_size_pct': 0.02,    # 2% base del balance
}

# Logging
LOG_CONFIG = {
    'level': 'INFO',
    'file': 'logs/trading.log',
    'max_size': '10MB',
    'backup_count': 5
}
EOF

# 4. Crear README.md
cat > README.md << 'EOF'
# 🚀 Crypto Momentum Trading Bot

Bot de trading automatizado para Bybit enfocado en capturar movimientos de momentum en criptomonedas.

## ⚡ Características Principales

- **🔥 Momentum Trading**: Análisis en tiempo real de momentum en múltiples timeframes (1m, 5m, 15m)
- **📊 Gestión Activa**: Monitoreo dedicado cada 2 segundos por posición
- **💰 Profit Taking Inteligente**: TPs parciales adaptativos, trailing stops dinámicos, break-even automático
- **🔄 Rotación de Capital**: Cierra posiciones débiles por mejores oportunidades
- **🛡️ Risk Management**: Emergency exits, límites de drawdown, gestión de riesgo por posición
- **📱 Control vía Telegram**: Monitoreo y control desde cualquier lugar

## 🎯 Estrategia

El bot identifica "explosiones de momentum" usando:
- Rate of Change (ROC) en corto/medio plazo
- Detección de Squeeze de Bollinger Bands
- Aceleración de precio (segunda derivada)
- Volumen relativo

Entra en la dirección del momentum con stops ajustados por ATR y gestiona activamente para maximizar profit.

## 📁 Estructura del Proyecto