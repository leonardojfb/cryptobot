import json
import sqlite3

def limpiar_anomalias():
    print("Escaneando memoria en busca del falso millonario...")
    
    try:
        with open("bot_memory.json", "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        print("No se encontró bot_memory.json")
        return

    # Conectamos a la base de datos de la IA
    conn = sqlite3.connect("ai_memory.db")
    cursor = conn.cursor()

    # Reiniciamos las sumatorias
    data["symbol_stats"] = {}
    data["mode_stats"] = {}
    
    reparados = 0

    for rec in data.get("trade_history", []):
        if rec.get("result") is None:
            continue # Trade abierto, no tocar
            
        # --- ESCUDO ANTI-NULOS ---
        close_raw = rec.get("close_price")
        pnl_raw = rec.get("pnl_usdt")
        
        if close_raw is None: close_raw = rec.get("entry_price", 1.0)
        if pnl_raw is None: pnl_raw = 0.0
            
        c_price = float(close_raw)
        pnl_usdt = float(pnl_raw)
        # -------------------------
            
        # 1. Detectar el bug del precio de cierre en 0
        if c_price <= 0.01:
            print(f"⚠️ Trade corrupto detectado en {rec.get('symbol')}: Precio de cierre fue 0.0")
            rec["close_price"] = rec.get("entry_price")
            rec["pnl_usdt"] = 0.0
            rec["result"] = "BREAKEVEN"
            reparados += 1
            
        # 2. Detectar PnLs astronómicos (Seguro de vida adicional)
        elif abs(pnl_usdt) > 10000:
            print(f"⚠️ PnL irreal detectado en {rec.get('symbol')}: {pnl_usdt}")
            rec["close_price"] = rec.get("entry_price")
            rec["pnl_usdt"] = 0.0
            rec["result"] = "BREAKEVEN"
            reparados += 1

        # --- CORRELACIÓN CON LA IA ---
        # Esto sobrescribe el error en la tabla que lee DeepSeek para aprender
        cursor.execute("""
            UPDATE trade_outcomes 
            SET pnl_usdt=?, result=?, close_price=? 
            WHERE trade_id=?
        """, (rec.get("pnl_usdt", 0.0), rec.get("result", "BREAKEVEN"), rec.get("close_price", 0.0), rec.get("trade_id")))

        # Reconstruimos las estadísticas locales
        sym = rec.get("symbol", "UNKNOWN")
        mode = rec.get("entry_mode", "STANDARD")
        pnl = float(rec.get("pnl_usdt", 0.0))
        res = rec.get("result", "BREAKEVEN")

        if sym not in data["symbol_stats"]:
            data["symbol_stats"][sym] = {"wins":0, "losses":0, "total_pnl":0.0, "trades":0, "best":0.0, "worst":0.0}
        s = data["symbol_stats"][sym]
        
        s["trades"] += 1
        s["total_pnl"] += pnl
        s["best"] = max(s["best"], pnl)
        s["worst"] = min(s["worst"], pnl)
        if res == "WIN": s["wins"] += 1
        elif res == "LOSS": s["losses"] += 1

        if mode not in data["mode_stats"]:
            data["mode_stats"][mode] = {"wins":0, "losses":0, "total_pnl":0.0, "trades":0}
        m = data["mode_stats"][mode]
        m["trades"] += 1
        m["total_pnl"] += pnl
        if res == "WIN": m["wins"] += 1
        elif res == "LOSS": m["losses"] += 1

    # Guardar el JSON purgado
    with open("bot_memory.json", "w") as f:
        json.dump(data, f, indent=2)

    conn.commit()
    conn.close()
    
    print(f"✅ Listo. Se neutralizaron {reparados} anomalías.")
    print("🤖 La base de datos de la IA (ai_memory.db) fue reparada para no contaminar el aprendizaje.")

if __name__ == "__main__":
    limpiar_anomalias()