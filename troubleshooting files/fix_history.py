import json
import sqlite3

def curar_historial():
    print("Iniciando reparación del historial...")
    
    # 1. Abrimos la memoria JSON
    try:
        with open("bot_memory.json", "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        print("No se encontró bot_memory.json. Asegúrate de estar en la carpeta correcta.")
        return

    # 2. Conectamos a la base de datos SQL de la IA
    conn = sqlite3.connect("ai_memory.db")
    cursor = conn.cursor()

    # 3. Reiniciamos las estadísticas globales para sumarlas de nuevo correctamente
    data["symbol_stats"] = {}
    data["mode_stats"] = {}
    
    reparados = 0

    # 4. Recalculamos cada trade uno por uno
    for rec in data.get("trade_history", []):
        if rec.get("result") is not None:  # Solo trades "cerrados"
            
            # --- ESCUDO ANTI-NULOS ---
            entry_raw = rec.get("entry_price")
            close_raw = rec.get("close_price")
            qty_raw = rec.get("qty")
            
            # Si algún dato clave está vacío, usamos valores por defecto
            if entry_raw is None: entry_raw = 0.0
            if close_raw is None: close_raw = entry_raw # Si no hay cierre, asumimos empate
            if qty_raw is None: qty_raw = 0.0
            
            entry = float(entry_raw)
            close = float(close_raw)
            qty = float(qty_raw)
            side = rec.get("side", "LONG")
            # -------------------------

            # LA MATEMÁTICA CORRECTA (Sin apalancamiento fantasma)
            if side == "LONG":
                pnl = (close - entry) * qty
            else:
                pnl = (entry - close) * qty

            # Corregir el Win Rate
            if pnl > 0:
                result = "WIN"
            elif pnl < 0:
                result = "LOSS"
            else:
                result = "BREAKEVEN"

            # Actualizamos el registro del JSON
            rec["pnl_usdt"] = round(pnl, 4)
            rec["result"] = result

            # Actualizamos la Base de Datos de la IA
            cursor.execute("""
                UPDATE trade_outcomes 
                SET pnl_usdt = ?, result = ? 
                WHERE trade_id = ?
            """, (round(pnl, 4), result, rec.get("trade_id", "")))

            # Reconstruimos las estadísticas del JSON
            sym = rec.get("symbol", "")
            mode = rec.get("entry_mode", "STANDARD")

            # -- Stats por Símbolo --
            if sym not in data["symbol_stats"]:
                data["symbol_stats"][sym] = {"wins":0, "losses":0, "total_pnl":0.0, "trades":0, "best":0.0, "worst":0.0}
            s = data["symbol_stats"][sym]
            s["trades"] += 1
            s["total_pnl"] += pnl
            s["best"] = max(s.get("best", 0.0), pnl)
            s["worst"] = min(s.get("worst", 0.0), pnl)
            if result == "WIN": s["wins"] += 1
            elif result == "LOSS": s["losses"] += 1

            # -- Stats por Modo --
            if mode not in data["mode_stats"]:
                data["mode_stats"][mode] = {"wins":0, "losses":0, "total_pnl":0.0, "trades":0}
            m = data["mode_stats"][mode]
            m["trades"] += 1
            m["total_pnl"] += pnl
            if result == "WIN": m["wins"] += 1
            elif result == "LOSS": m["losses"] += 1
            
            reparados += 1

    # 5. Guardamos todo
    with open("bot_memory.json", "w") as f:
        json.dump(data, f, indent=2)

    conn.commit()
    conn.close()
    
    print(f"✅ ¡Éxito! Se han recalculado {reparados} trades.")
    print("La matemática ahora es 100% real y la IA aprenderá correctamente.")

if __name__ == "__main__":
    curar_historial()