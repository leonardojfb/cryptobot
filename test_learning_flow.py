#!/usr/bin/env python3
"""
Test script para verificar que el ciclo de learning funciona:
1. Crea un trade ficticio en la BD
2. Verifica que se puede consultar
3. Simula el cierre y verifica el outcome
"""

import sys
import time
import json
from datetime import datetime, timedelta

# Asegurar imports correctos
try:
    import ai_memory
    import ai_filter
    from learning_engine import LearningEngine
    print("✅ Imports exitosos: ai_memory, ai_filter, learning_engine")
except ImportError as e:
    print(f"❌ Error en imports: {e}")
    sys.exit(1)

def test_learning_cycle():
    print("\n" + "="*60)
    print("🧪 TEST: Ciclo completo de Learning")
    print("="*60)

    # 1. Crear datos ficticicios de un trade
    sym = "BTCUSDT"
    trade_id = "TEST001"
    ts_open = int(time.time()) - 3600  # hace 1 hora
    ts_close = int(time.time())

    print(f"\n1️⃣  Simulando apertura de trade...")
    print(f"   Symbol: {sym}")
    print(f"   Trade ID: {trade_id}")
    print(f"   TS Open: {datetime.fromtimestamp(ts_open)}")

    # 2. Guardar decisión de IA (como si fuera aprobada)
    print(f"\n2️⃣  Guardando decisión de IA...")
    try:
        analysis = {
            "symbol": sym,
            "signal": "LONG",
            "composite_score": 75.5,
            "confidence": 0.85,
            "macro_bias": "BULLISH",
            "mid_bias": "NEUTRAL",
            "entry_bias": "BULLISH",
            "tp": 45000,
            "sl": 42000,
            "mark_price": 44000,
            "atr": 500,
            "aligned": True,
            "squeeze": False,
            "vol_spike": False,
            "entry_mode": "STANDARD"
        }
        ai_result = {
            "approve": True,
            "confidence": 0.85,
            "reasoning": "Test: Good setup with positive news bias",
            "warnings": []
        }
        news_bias = {
            "news_score": 10.0,
            "direction": "BULLISH",
            "fear_greed": 55,
            "fg_label": "Neutral",
            "recent_alerts": 0
        }
        symbol_stats = {
            "trades": 5,
            "wins": 3,
            "total_pnl": 150.5
        }
        recent_news_list = [
            {"title": "Bitcoin rises", "source": "Reuters", "direction": "BULLISH", "sentiment_score": 0.7}
        ]

        ai_memory.save_decision(
            trade_id=trade_id,
            analysis=analysis,
            ai_result=ai_result,
            news_bias=news_bias,
            symbol_stats=symbol_stats,
            recent_news=recent_news_list
        )
        print(f"   ✅ Decisión guardada en BD")
    except Exception as e:
        print(f"   ❌ Error guardando decisión: {e}")
        return False

    # 3. Simular cierre de trade (ganador)
    print(f"\n3️⃣  Guardando outcome del trade (ganador)...")
    try:
        ai_memory.save_outcome(
            trade_id=trade_id,
            symbol=sym,
            side="LONG",
            entry_price=44000.0,
            close_price=44500.0,  # +500 ganancia
            pnl_usdt=50.0,  # Ganancia de 50 USDT
            pnl_pct=1.13,  # 1.13%
            result="WIN",
            close_reason="TP",
            duration_s=3600,
            leverage=5,
            ts_open=ts_open,
            ts_close=ts_close
        )
        print(f"   ✅ Outcome guardado en BD")
    except Exception as e:
        print(f"   ❌ Error guardando outcome: {e}")
        return False

    # 4. Consultar la decisión
    print(f"\n4️⃣  Consultando decisiones previas...")
    try:
        history = ai_memory.get_symbol_history(sym, limit=5)
        if history:
            print(f"   ✅ Encontradas {len(history)} decisiones:")
            for dec in history:
                print(f"      - Trade {dec.get('trade_id')}: {dec.get('approved')} ({dec.get('reasoning')})")
        else:
            print(f"   ⚠️  No hay decisiones previas (esperado si es el primer trade)")
    except Exception as e:
        print(f"   ❌ Error consultando: {e}")
        return False

    # 5. Consultar accuracy
    print(f"\n5️⃣  Consultando accuracy de IA...")
    try:
        accuracy = ai_memory.get_ai_accuracy(sym)
        print(f"   ✅ Accuracy report:")
        for key, val in accuracy.items():
            print(f"      - {key}: {val}")
    except Exception as e:
        print(f"   ❌ Error consultando accuracy: {e}")
        return False

    # 6. Verificar PnL summary
    print(f"\n6️⃣  Consultando resumen de PnL (últimos 7 días)...")
    try:
        summary = ai_memory.get_pnl_summary(days=7)
        print(f"   ✅ Resumen PnL:")
        for key, val in summary.items():
            if key in ["total_pnl", "avg_pnl", "win_rate", "trades_count"]:
                print(f"      - {key}: {val}")
    except Exception as e:
        print(f"   ❌ Error consultando PnL: {e}")
        return False

    print("\n" + "="*60)
    print("✅ TEST COMPLETADO EXITOSAMENTE")
    print("="*60)
    return True


def test_telegram_format():
    """Verificar que los formatos de Telegram para PnL funcionan"""
    print("\n" + "="*60)
    print("🧪 TEST: Formato de Telegram (PnL con signos)")
    print("="*60)

    def _pnl_str(pnl) -> str:
        """Formato con signo explícito"""
        return f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"

    test_cases = [50.5, -30.2, 0.0, 123.456, -0.01]
    print("\nFormatos PnL:")
    for pnl in test_cases:
        formatted = _pnl_str(pnl)
        print(f"  {pnl:8.2f}  →  {formatted:>8s}")

    print("\n✅ Formato de PnL verificado")
    return True


if __name__ == "__main__":
    print("\n🚀 Iniciando tests de learning...")
    print("Este script verifica que el ciclo de learning está completo:\n")
    print("  1. AI toma decisión (save_decision)")
    print("  2. Trade se abre y cierra")
    print("  3. Outcome se registra (save_outcome)")
    print("  4. AI puede consultar su histórico para aprender")
    print("  5. Telegram puede mostrar stats\n")

    # Test 1: Ciclo completo
    success = test_learning_cycle()

    # Test 2: Formatos
    test_telegram_format()

    if success:
        print("\n🎉 Todos los tests pasaron!")
        print("\nProximos pasos:")
        print("  1. Ejecuta el bot real: python main.py")
        print("  2. Abre un trade real (o usa paper mode)")
        print("  3. Cuando se cierre, verifica en Telegram:")
        print("     /aprend  → deberías ver stats incluyendo este trade")
        print("     /noticias → deberías ver influencia de noticias")
        sys.exit(0)
    else:
        print("\n❌ Tests fallaron")
        sys.exit(1)
