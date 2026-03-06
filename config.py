# config.py - Configuración para el motor de Momentum

MOMENTUM_CONFIG = {
    'scan_interval': 2,
    'min_score': 2.0,
    'min_confidence': 20,
    'max_positions': 5,
    'partial_tp_levels': [1.5, 3.0, 5.0, 8.0],
    'partial_tp_sizes': [0.25, 0.25, 0.25, 0.25],
    'trailing_activation': 2.0,
    'trailing_distance': 0.5,
    'be_activation': 1.0,
    'risk_per_trade': 0.01,
    'position_size_pct': 0.02,
    'momentum_threshold': 0.7,
}

TRADING_SYMBOLS = [
    'BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT'
]

TIMEFRAMES = {
    'fast': '1',
    'medium': '5',
    'slow': '15',
}
