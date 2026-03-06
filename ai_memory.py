"""
ai_memory.py — Base de datos SQLite para el filtro de IA
Guarda cada decisión de DeepSeek, el razonamiento, y el resultado real del trade.
La IA consulta este historial para mejorar sus análisis futuros.

Tablas:
  ai_decisions  — cada consulta que se le hizo a DeepSeek
  trade_outcomes — resultado real de cada trade (PnL + / -)
"""

import sqlite3
import json
import logging
import os
import time
from typing import Dict, List, Optional

log = logging.getLogger("ai_memory")

DB_PATH = os.getenv("AI_MEMORY_DB", "ai_memory.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row          # acceso por nombre de columna
    conn.execute("PRAGMA journal_mode=WAL") # escrituras concurrentes sin bloqueo
    return conn


def init_db():
    """Crea las tablas si no existen. Llamar al arrancar el bot."""
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_decisions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id      TEXT,
            symbol        TEXT    NOT NULL,
            signal        TEXT    NOT NULL,          -- LONG | SHORT
            composite_score REAL,
            confidence    REAL,
            entry_mode    TEXT,
            -- Decisión de la IA
            approved      INTEGER NOT NULL,          -- 1 = aprobado, 0 = rechazado
            ai_confidence REAL,
            reasoning     TEXT,
            warnings      TEXT,                      -- JSON array string
            -- Contexto de noticias en el momento
            news_score    REAL,
            news_direction TEXT,
            fear_greed    INTEGER,
            fg_label      TEXT,
            recent_alerts INTEGER,
            recent_news   TEXT,                      -- JSON: últimas 3 noticias
            -- Contexto técnico adicional
            macro_bias    TEXT,
            mid_bias      TEXT,
            entry_bias    TEXT,
            aligned       INTEGER,
            squeeze       INTEGER,
            vol_spike     INTEGER,
            mark_price    REAL,
            atr           REAL,
            tp            REAL,
            sl            REAL,
            -- Historial del símbolo en el momento
            sym_trades    INTEGER,
            sym_wr        REAL,
            sym_pnl       REAL,
            -- Timestamp
            ts            INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trade_outcomes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id      TEXT    UNIQUE NOT NULL,
            symbol        TEXT    NOT NULL,
            side          TEXT    NOT NULL,          -- LONG | SHORT
            entry_price   REAL,
            close_price   REAL,
            pnl_usdt      REAL,                      -- positivo o negativo
            pnl_pct       REAL,
            result        TEXT,                      -- WIN | LOSS | BREAKEVEN
            close_reason  TEXT,                      -- TP | SL | MANUAL | etc.
            duration_s    INTEGER,
            leverage      INTEGER,
            ai_approved   INTEGER,                   -- 1 si la IA aprobó este trade
            ai_reasoning  TEXT,                      -- qué dijo la IA al aprobar
            ts_open       INTEGER,
            ts_close      INTEGER
        );

        -- Índices para queries rápidas
        CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON ai_decisions(symbol);
        CREATE INDEX IF NOT EXISTS idx_decisions_ts     ON ai_decisions(ts);
        CREATE INDEX IF NOT EXISTS idx_outcomes_symbol  ON trade_outcomes(symbol);
        CREATE INDEX IF NOT EXISTS idx_outcomes_result  ON trade_outcomes(result);
        CREATE INDEX IF NOT EXISTS idx_outcomes_ts      ON trade_outcomes(ts_close);
    """)
    conn.commit()
    conn.close()
    log.info(f"✅ ai_memory.db inicializada en {DB_PATH}")


# ── Escritura ──────────────────────────────────────────────────────────────────

def save_decision(trade_id: str, analysis: Dict, ai_result: Dict,
                  news_bias: Dict, symbol_stats: Dict,
                  recent_news: List[Dict]) -> int:
    """
    Guarda una decisión de la IA en la base de datos.
    Retorna el id insertado.
    """
    sym     = analysis.get("symbol", "")
    signal  = analysis.get("signal", "")
    tf      = analysis.get("tf_details", {})

    # Últimas 3 noticias en formato compacto
    news_compact = json.dumps([
        {"title": n.get("title", "")[:80],
         "source": n.get("source", ""),
         "direction": n.get("direction", ""),
         "score": round(n.get("sentiment_score", 0), 2)}
        for n in (recent_news or [])[:3]
    ])

    # Stats del símbolo
    st = symbol_stats or {}
    sym_trades = st.get("trades", 0)
    sym_wr     = round(st["wins"] / sym_trades, 3) if sym_trades > 0 else None
    sym_pnl    = st.get("total_pnl")

    conn = _connect()
    try:
        cur = conn.execute("""
            INSERT INTO ai_decisions (
                trade_id, symbol, signal, composite_score, confidence, entry_mode,
                approved, ai_confidence, reasoning, warnings,
                news_score, news_direction, fear_greed, fg_label, recent_alerts, recent_news,
                macro_bias, mid_bias, entry_bias, aligned, squeeze, vol_spike,
                mark_price, atr, tp, sl,
                sym_trades, sym_wr, sym_pnl,
                ts
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade_id, sym, signal,
            analysis.get("composite_score"), analysis.get("confidence"),
            analysis.get("entry_mode", "STANDARD"),
            1 if ai_result.get("approve") else 0,
            ai_result.get("confidence"), ai_result.get("reasoning"),
            json.dumps(ai_result.get("warnings", [])),
            news_bias.get("news_score"), news_bias.get("direction"),
            news_bias.get("fear_greed"), news_bias.get("fg_label"),
            news_bias.get("recent_alerts", 0), news_compact,
            analysis.get("macro_bias"), analysis.get("mid_bias"),
            analysis.get("entry_bias"),
            1 if analysis.get("aligned") else 0,
            1 if analysis.get("squeeze") else 0,
            1 if analysis.get("vol_spike") else 0,
            analysis.get("mark_price"), analysis.get("atr"),
            analysis.get("tp"), analysis.get("sl"),
            sym_trades, sym_wr, sym_pnl,
            int(time.time())
        ))
        conn.commit()
        row_id = cur.lastrowid
        return row_id
    finally:
        conn.close()


def save_outcome(trade_id: str, symbol: str, side: str,
                 entry_price: float, close_price: float,
                 pnl_usdt: float, pnl_pct: float, result: str,
                 close_reason: str, duration_s: int, leverage: int,
                 ts_open: int, ts_close: int):
    """
    Guarda el resultado real de un trade.
    Se llama cuando el trade se cierra (TP, SL, manual).
    También actualiza el campo ai_approved/ai_reasoning buscando la decisión previa.
    """
    conn = _connect()
    try:
        # Buscar si hubo decisión de IA para este trade
        row = conn.execute(
            "SELECT approved, reasoning FROM ai_decisions WHERE trade_id=? ORDER BY ts DESC LIMIT 1",
            (trade_id,)
        ).fetchone()
        ai_approved  = row["approved"]  if row else None
        ai_reasoning = row["reasoning"] if row else None

        conn.execute("""
            INSERT OR REPLACE INTO trade_outcomes (
                trade_id, symbol, side, entry_price, close_price,
                pnl_usdt, pnl_pct, result, close_reason,
                duration_s, leverage, ai_approved, ai_reasoning,
                ts_open, ts_close
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade_id, symbol, side, entry_price, close_price,
            pnl_usdt, pnl_pct, result, close_reason,
            duration_s, leverage, ai_approved, ai_reasoning,
            ts_open, ts_close
        ))
        conn.commit()
        log.info(f"📝 Outcome guardado: {symbol} {side} PnL={pnl_usdt:+.2f} [{result}]")
    finally:
        conn.close()


# ── Lectura para la IA ─────────────────────────────────────────────────────────

def get_symbol_history(symbol: str, limit: int = 15) -> List[Dict]:
    """
    Últimos N trades cerrados de un símbolo con la decisión de la IA.
    Se pasa al prompt de DeepSeek para que aprenda del pasado.
    """
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT o.side, o.pnl_usdt, o.result, o.close_reason,
                   o.ai_approved, o.ai_reasoning, o.duration_s,
                   d.composite_score, d.entry_mode, d.fear_greed,
                   d.news_direction, d.squeeze
            FROM trade_outcomes o
            LEFT JOIN ai_decisions d ON o.trade_id = d.trade_id
            WHERE o.symbol = ?
            ORDER BY o.ts_close DESC
            LIMIT ?
        """, (symbol, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_ai_accuracy(symbol: str = None) -> Dict:
    """
    Estadísticas de acierto de la IA: cuando aprobó/rechazó, ¿acertó?
    """
    conn = _connect()
    try:
        where = "WHERE o.symbol = ?" if symbol else ""
        params = (symbol,) if symbol else ()

        rows = conn.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN o.ai_approved=1 AND o.result='WIN'  THEN 1 ELSE 0 END) as approved_wins,
                SUM(CASE WHEN o.ai_approved=1 AND o.result='LOSS' THEN 1 ELSE 0 END) as approved_losses,
                SUM(CASE WHEN o.ai_approved=0 AND o.result='WIN'  THEN 1 ELSE 0 END) as rejected_wins,
                SUM(CASE WHEN o.ai_approved=0 AND o.result='LOSS' THEN 1 ELSE 0 END) as rejected_losses,
                SUM(CASE WHEN o.ai_approved=1 THEN o.pnl_usdt ELSE 0 END) as approved_pnl,
                SUM(CASE WHEN o.ai_approved=0 THEN o.pnl_usdt ELSE 0 END) as rejected_pnl
            FROM trade_outcomes o
            {where}
        """, params).fetchone()

        r = dict(rows)
        # Cuánto dinero se hubiera ganado/perdido en trades rechazados
        r["avoided_loss"] = -(r["rejected_pnl"] or 0)  # positivo = evitó pérdida
        return r
    finally:
        conn.close()


def get_recent_decisions(limit: int = 10) -> List[Dict]:
    """Últimas N decisiones de la IA (con o sin outcome aún)."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT d.symbol, d.signal, d.approved, d.ai_confidence,
                   d.reasoning, d.fear_greed, d.news_direction,
                   d.composite_score, d.entry_mode, d.ts,
                   o.result, o.pnl_usdt
            FROM ai_decisions d
            LEFT JOIN trade_outcomes o ON d.trade_id = o.trade_id
            ORDER BY d.ts DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pnl_summary(days: int = 7) -> Dict:
    """
    Resumen de PnL de los últimos N días.
    Siempre muestra signo + / - claramente.
    Valida tipos de datos: convierte strings a floats, maneja None.
    """
    since = int(time.time()) - days * 86400
    conn  = _connect()
    try:
        row = conn.execute("""
            SELECT
                COUNT(*)                                              as total_trades,
                SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END)       as wins,
                SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END)       as losses,
                SUM(pnl_usdt)                                         as total_pnl,
                SUM(CASE WHEN pnl_usdt > 0 THEN pnl_usdt ELSE 0 END) as gross_profit,
                SUM(CASE WHEN pnl_usdt < 0 THEN pnl_usdt ELSE 0 END) as gross_loss,
                MAX(pnl_usdt)                                         as best_trade,
                MIN(pnl_usdt)                                         as worst_trade,
                AVG(CASE WHEN result='WIN'  THEN pnl_usdt END)        as avg_win,
                AVG(CASE WHEN result='LOSS' THEN pnl_usdt END)        as avg_loss
            FROM trade_outcomes
            WHERE ts_close >= ?
        """, (since,)).fetchone()
        d = dict(row)
        
        # Convertir a tipos correctos y manejar None
        total_trades = int(d.get("total_trades") or 0)
        wins = int(d.get("wins") or 0)
        losses = int(d.get("losses") or 0)
        
        # Convertir PnL a floats
        total_pnl = float(d.get("total_pnl") or 0)
        gross_profit = float(d.get("gross_profit") or 0)
        gross_loss = abs(float(d.get("gross_loss") or 0))
        best_trade = float(d.get("best_trade") or 0)
        worst_trade = float(d.get("worst_trade") or 0)
        avg_win = float(d.get("avg_win") or 0)
        avg_loss = float(d.get("avg_loss") or 0)
        
        # Actualizar diccionario con valores validados
        d["total_trades"] = total_trades
        d["wins"] = wins
        d["losses"] = losses
        d["total_pnl"] = round(total_pnl, 2)
        d["gross_profit"] = round(gross_profit, 2)
        d["gross_loss"] = round(gross_loss * -1, 2)  # Devolver como negativo para claridad
        d["best_trade"] = round(best_trade, 2)
        d["worst_trade"] = round(worst_trade, 2)
        d["avg_win"] = round(avg_win, 2)
        d["avg_loss"] = round(avg_loss, 2)
        
        # Profit factor
        d["profit_factor"] = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
        # Win rate: evitar división por cero
        d["win_rate"] = round(wins / total_trades * 100, 1) if total_trades > 0 else 0
        return d
    finally:
        conn.close()


def get_daily_pnl(days: int = 14) -> List[Dict]:
    """PnL por día para los últimos N días."""
    since = int(time.time()) - days * 86400
    conn  = _connect()
    try:
        rows = conn.execute("""
            SELECT
                date(ts_close, 'unixepoch', 'localtime') as day,
                COUNT(*)                                  as trades,
                SUM(pnl_usdt)                             as pnl,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins
            FROM trade_outcomes
            WHERE ts_close >= ?
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
        """, (since, days)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
