"""
analysis_engine_bybit.py  v4  — Motor SMC (Smart Money Concepts)
═══════════════════════════════════════════════════════════════════
Filosofía: Seguir a las instituciones, no al retail.

CONCEPTOS SMC IMPLEMENTADOS:
  1. ORDER BLOCKS          — Última vela bajista antes de impulso alcista (y viceversa)
  2. FAIR VALUE GAPS       — Imbalances entre high[i-2] y low[i] (áreas de reentry institucional)
  3. LIQUIDITY SWEEPS      — Barrido de máximos/mínimos anteriores antes de reversión
  4. VWAP RETEST           — Rebotes sobre/bajo VWAP (precio justo institucional)
  5. CHANGE OF CHARACTER   — ChoCH: primer HL en downtrend / LH en uptrend (señal de reversión)
  6. BREAK OF STRUCTURE    — BoS: continuación de tendencia confirmada
  7. PREMIUM / DISCOUNT    — OB's en discount (zona compra) o premium (zona venta)
  8. INDUCEMENT             — Barrido de liquidez antes del verdadero movimiento

EMAs INSTITUCIONALES: 7, 25, 99 (vs retail 7/21/50/200)

SCORING SMC (-10 a +10):
  - Order Block hit:        ±3.0
  - FVG fill:               ±2.5
  - Liquidity Sweep + reversal: ±3.5
  - VWAP retest:            ±2.0
  - BoS confirmado:         ±2.0
  - ChoCH detección:        ±1.5
  - EMA confluence (7/25/99): ±2.5
  - Orderbook imbalance:    ±1.5
"""

import time
import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

log = logging.getLogger("analysis_smc")

# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN DE TIMEFRAMES
# ══════════════════════════════════════════════════════════
TF_CONFIG: Dict[str, Dict] = {
    "1":   {"label": "1m",  "category": "entry", "weight": 0.04, "min_bars": 60},
    "3":   {"label": "3m",  "category": "entry", "weight": 0.05, "min_bars": 60},
    "5":   {"label": "5m",  "category": "entry", "weight": 0.07, "min_bars": 60},
    "15":  {"label": "15m", "category": "entry", "weight": 0.09, "min_bars": 60},
    "30":  {"label": "30m", "category": "mid",   "weight": 0.11, "min_bars": 80},
    "60":  {"label": "1h",  "category": "mid",   "weight": 0.14, "min_bars": 100},
    "120": {"label": "2h",  "category": "mid",   "weight": 0.13, "min_bars": 100},
    "240": {"label": "4h",  "category": "macro", "weight": 0.14, "min_bars": 100},
    "360": {"label": "6h",  "category": "macro", "weight": 0.09, "min_bars": 100},
    "720": {"label": "12h", "category": "macro", "weight": 0.07, "min_bars": 80},
    "D":   {"label": "1D",  "category": "macro", "weight": 0.05, "min_bars": 60},
    "W":   {"label": "1W",  "category": "macro", "weight": 0.02, "min_bars": 30},
}

ALL_TF   = list(TF_CONFIG.keys())
MACRO_TF = [tf for tf, c in TF_CONFIG.items() if c["category"] == "macro"]
MID_TF   = [tf for tf, c in TF_CONFIG.items() if c["category"] == "mid"]
ENTRY_TF = [tf for tf, c in TF_CONFIG.items() if c["category"] == "entry"]

MODE_AGGRESSIVE = "AGGRESSIVE"
MODE_MOMENTUM   = "MOMENTUM"
MODE_STANDARD   = "STANDARD"

# ══════════════════════════════════════════════════════════
#  UTILIDADES BÁSICAS
# ══════════════════════════════════════════════════════════

def to_df(klines: List[Dict]) -> pd.DataFrame:
    if not klines:
        return pd.DataFrame()
    df = pd.DataFrame(klines)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close"], inplace=True)
    df.sort_values("open_time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def _atr_series(df: pd.DataFrame, p: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - pc).abs(),
        (df["low"]  - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / p, adjust=False).mean()

def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def _macd(s: pd.Series, fast=12, slow=26, sig=9):
    ml = _ema(s, fast) - _ema(s, slow)
    ms = _ema(ml, sig)
    return ml, ms, ml - ms

def _bollinger(s: pd.Series, p=20, k=2.0):
    m  = s.rolling(p).mean()
    sd = s.rolling(p).std()
    return m + k * sd, m, m - k * sd

def _vwap(df: pd.DataFrame) -> float:
    pv = (df["close"] * df["volume"]).sum()
    v  = df["volume"].sum()
    return float(pv / v) if v > 0 else float(df["close"].iloc[-1])

def _obv(df: pd.DataFrame) -> pd.Series:
    return (np.sign(df["close"].diff().fillna(0)) * df["volume"]).cumsum()

def _supertrend(df: pd.DataFrame, p=10, mult=3.0) -> Tuple[pd.Series, pd.Series]:
    a   = _atr_series(df, p)
    hl2 = (df["high"] + df["low"]) / 2
    up  = hl2 + mult * a
    dn  = hl2 - mult * a
    st  = pd.Series(np.nan, index=df.index)
    dir_= pd.Series(1, index=df.index, dtype=int)
    for i in range(1, len(df)):
        ub = up.iloc[i] if (up.iloc[i] < up.iloc[i-1] or df["close"].iloc[i-1] > up.iloc[i-1]) else up.iloc[i-1]
        lb = dn.iloc[i] if (dn.iloc[i] > dn.iloc[i-1] or df["close"].iloc[i-1] < dn.iloc[i-1]) else dn.iloc[i-1]
        if   dir_.iloc[i-1] == -1 and df["close"].iloc[i] > ub: dir_.iat[i] = 1
        elif dir_.iloc[i-1] ==  1 and df["close"].iloc[i] < lb: dir_.iat[i] = -1
        else: dir_.iat[i] = dir_.iloc[i-1]
        st.iat[i] = lb if dir_.iat[i] == 1 else ub
    return st, dir_

def _market_structure(df: pd.DataFrame) -> str:
    if len(df) < 20:
        return "ranging"
    closes = df["close"].tail(20).values
    peaks   = [closes[i] for i in range(1, len(closes)-1)
               if closes[i] > closes[i-1] and closes[i] > closes[i+1]]
    troughs = [closes[i] for i in range(1, len(closes)-1)
               if closes[i] < closes[i-1] and closes[i] < closes[i+1]]
    if len(peaks) >= 2 and len(troughs) >= 2:
        if peaks[-1] > peaks[-2] and troughs[-1] > troughs[-2]: return "bullish"
        if peaks[-1] < peaks[-2] and troughs[-1] < troughs[-2]: return "bearish"
    return "ranging"

def _volume_spike(df: pd.DataFrame) -> Tuple[bool, float]:
    if len(df) < 20:
        return False, 1.0
    vol_ma  = float(df["volume"].rolling(20).mean().iloc[-1])
    vol_now = float(df["volume"].iloc[-1])
    ratio   = vol_now / vol_ma if vol_ma > 0 else 1.0
    return ratio >= 2.5, round(ratio, 2)

def _pivot_sr(df: pd.DataFrame) -> Dict:
    if len(df) < 3:
        return {}
    h, l, c = float(df["high"].iloc[-2]), float(df["low"].iloc[-2]), float(df["close"].iloc[-2])
    p = (h + l + c) / 3
    return {"P": p, "R1": 2*p-l, "R2": p+(h-l), "S1": 2*p-h, "S2": p-(h-l)}

# ══════════════════════════════════════════════════════════
#  SMC CORE — DETECTORES
# ══════════════════════════════════════════════════════════

def _detect_order_blocks(df: pd.DataFrame, lookback: int = 20) -> Dict:
    """
    Order Block (OB): última vela impulsiva de origen antes de un movimiento fuerte.

    Bullish OB: última vela BAJISTA antes de un impulso alcista significativo.
    → El cuerpo de esa vela (low–high) es zona de demanda institucional.

    Bearish OB: última vela ALCISTA antes de un impulso bajista significativo.
    → El cuerpo de esa vela (low–high) es zona de oferta institucional.

    Retorna:
      {"bullish_ob": {"high": float, "low": float, "age": int} | None,
       "bearish_ob": {"high": float, "low": float, "age": int} | None,
       "price_in_bullish_ob": bool,
       "price_in_bearish_ob": bool}
    """
    if len(df) < lookback + 5:
        return {"bullish_ob": None, "bearish_ob": None,
                "price_in_bullish_ob": False, "price_in_bearish_ob": False}

    price   = float(df["close"].iloc[-1])
    window  = df.tail(lookback + 5).reset_index(drop=True)
    n       = len(window)

    bullish_ob = None
    bearish_ob = None

    # Umbral de "impulso significativo": ATR * 1.5
    atr_val = float(_atr_series(df).iloc[-1])
    impulse_threshold = atr_val * 1.5

    for i in range(2, n - 2):
        # Impulso alcista fuerte (3 velas desde i+1)
        candle_move_up = float(window["close"].iloc[i+1]) - float(window["open"].iloc[i+1])
        if candle_move_up > impulse_threshold:
            # La vela en i es bajista → Bullish OB
            if float(window["close"].iloc[i]) < float(window["open"].iloc[i]):
                ob_low  = float(window["low"].iloc[i])
                ob_high = float(window["high"].iloc[i])
                age     = n - 1 - i
                if bullish_ob is None or age < bullish_ob["age"]:
                    bullish_ob = {"high": ob_high, "low": ob_low, "age": age,
                                  "mid": (ob_high + ob_low) / 2}

        # Impulso bajista fuerte
        candle_move_dn = float(window["open"].iloc[i+1]) - float(window["close"].iloc[i+1])
        if candle_move_dn > impulse_threshold:
            # La vela en i es alcista → Bearish OB
            if float(window["close"].iloc[i]) > float(window["open"].iloc[i]):
                ob_low  = float(window["low"].iloc[i])
                ob_high = float(window["high"].iloc[i])
                age     = n - 1 - i
                if bearish_ob is None or age < bearish_ob["age"]:
                    bearish_ob = {"high": ob_high, "low": ob_low, "age": age,
                                  "mid": (ob_high + ob_low) / 2}

    in_bull_ob = (bullish_ob is not None and
                  bullish_ob["low"] <= price <= bullish_ob["high"])
    in_bear_ob = (bearish_ob is not None and
                  bearish_ob["low"] <= price <= bearish_ob["high"])

    return {
        "bullish_ob": bullish_ob,
        "bearish_ob": bearish_ob,
        "price_in_bullish_ob": in_bull_ob,
        "price_in_bearish_ob": in_bear_ob,
    }


def _detect_fvg(df: pd.DataFrame, lookback: int = 30) -> Dict:
    """
    Fair Value Gap (FVG) / Imbalance:
    Bullish FVG: low[i] > high[i-2]  → gap alcista, precio puede reentrar
    Bearish FVG: high[i] < low[i-2]  → gap bajista

    Retorna el FVG más reciente no rellenado y si el precio está en zona de fill.
    """
    if len(df) < 5:
        return {"bullish_fvg": None, "bearish_fvg": None,
                "price_in_bull_fvg": False, "price_in_bear_fvg": False}

    price  = float(df["close"].iloc[-1])
    window = df.tail(lookback + 3).reset_index(drop=True)
    n      = len(window)

    bullish_fvg = None
    bearish_fvg = None

    for i in range(2, n):
        gap_high_bull = float(window["low"].iloc[i])
        gap_low_bull  = float(window["high"].iloc[i-2])
        if gap_high_bull > gap_low_bull:
            age = n - 1 - i
            if bullish_fvg is None or age < bullish_fvg["age"]:
                bullish_fvg = {
                    "high": gap_high_bull,
                    "low":  gap_low_bull,
                    "mid":  (gap_high_bull + gap_low_bull) / 2,
                    "age":  age,
                    "size": gap_high_bull - gap_low_bull,
                }

        gap_high_bear = float(window["low"].iloc[i-2])
        gap_low_bear  = float(window["high"].iloc[i])
        if gap_high_bear > gap_low_bear:
            age = n - 1 - i
            if bearish_fvg is None or age < bearish_fvg["age"]:
                bearish_fvg = {
                    "high": gap_high_bear,
                    "low":  gap_low_bear,
                    "mid":  (gap_high_bear + gap_low_bear) / 2,
                    "age":  age,
                    "size": gap_high_bear - gap_low_bear,
                }

    in_bull_fvg = (bullish_fvg is not None and
                   bullish_fvg["low"] <= price <= bullish_fvg["high"])
    in_bear_fvg = (bearish_fvg is not None and
                   bearish_fvg["low"] <= price <= bearish_fvg["high"])

    return {
        "bullish_fvg": bullish_fvg,
        "bearish_fvg": bearish_fvg,
        "price_in_bull_fvg": in_bull_fvg,
        "price_in_bear_fvg": in_bear_fvg,
    }


def _liquidity_sweep(df: pd.DataFrame, lookback: int = 30) -> Dict:
    """
    Liquidity Sweep (Inducement + Stop Hunt):
    Las instituciones empujan el precio más allá de máximos/mínimos evidentes
    para capturar órdenes de stop antes del movimiento real.

    Bullish sweep: precio baja por debajo de mínimos anteriores y CIERRA de vuelta arriba
    Bearish sweep: precio sube por encima de máximos anteriores y CIERRA de vuelta abajo

    También detecta si el sweep ocurrió en las últimas N velas (señal fresca).
    """
    if len(df) < lookback + 5:
        return {
            "bullish_sweep": False, "bearish_sweep": False,
            "sweep_freshness": 0, "sweep_level": None,
            "equal_highs": None, "equal_lows": None,
        }

    window = df.tail(lookback).reset_index(drop=True)
    n      = len(window)
    price  = float(window["close"].iloc[-1])
    atr_v  = float(_atr_series(df).iloc[-1])

    # Máximos y mínimos de referencia (excluye las últimas 3 velas = zona activa)
    ref_highs = window["high"].iloc[:-3]
    ref_lows  = window["low"].iloc[:-3]
    swing_high = float(ref_highs.max()) if not ref_highs.empty else price
    swing_low  = float(ref_lows.min())  if not ref_lows.empty  else price

    last_low   = float(window["low"].iloc[-1])
    last_high  = float(window["high"].iloc[-1])
    last_close = float(window["close"].iloc[-1])
    last_open  = float(window["open"].iloc[-1])

    # Sweep alcista: perforó mínimo previo pero cerró POR ENCIMA del mínimo de referencia
    bullish_sweep = (
        last_low < swing_low and        # wicked below
        last_close > swing_low and      # closed back above
        last_close > last_open          # vela alcista (absorción)
    )
    # Sweep bajista: perforó máximo previo pero cerró POR DEBAJO del máximo de referencia
    bearish_sweep = (
        last_high > swing_high and      # wicked above
        last_close < swing_high and     # closed back below
        last_close < last_open          # vela bajista (absorción)
    )

    # Equal Highs/Lows (zonas de liquidez apilada — target institucional)
    tolerance = atr_v * 0.3
    highs = window["high"].values
    lows  = window["low"].values
    equal_highs = None
    equal_lows  = None
    for i in range(len(highs)-4, max(0, len(highs)-lookback), -1):
        if abs(highs[i] - swing_high) < tolerance:
            equal_highs = float(highs[i])
            break
    for i in range(len(lows)-4, max(0, len(lows)-lookback), -1):
        if abs(lows[i] - swing_low) < tolerance:
            equal_lows = float(lows[i])
            break

    # Freshness: cómo de reciente fue el sweep (1.0 = última vela, 0.0 = viejo)
    sweep_freshness = 0.0
    sweep_level     = None
    if bullish_sweep:
        sweep_freshness = 1.0
        sweep_level     = swing_low
    elif bearish_sweep:
        sweep_freshness = 1.0
        sweep_level     = swing_high
    else:
        # Buscar sweep en las últimas 5 velas
        for lag in range(1, min(6, n)):
            lw = float(window["low"].iloc[-(lag+1)])
            lc = float(window["close"].iloc[-(lag+1)])
            lo = float(window["open"].iloc[-(lag+1)])
            if lw < swing_low and lc > swing_low and lc > lo:
                sweep_freshness = max(0.1, 1.0 - lag * 0.15)
                sweep_level     = swing_low
                bullish_sweep   = True
                break
            hw = float(window["high"].iloc[-(lag+1)])
            hc = float(window["close"].iloc[-(lag+1)])
            ho = float(window["open"].iloc[-(lag+1)])
            if hw > swing_high and hc < swing_high and hc < ho:
                sweep_freshness = max(0.1, 1.0 - lag * 0.15)
                sweep_level     = swing_high
                bearish_sweep   = True
                break

    return {
        "bullish_sweep":   bullish_sweep,
        "bearish_sweep":   bearish_sweep,
        "sweep_freshness": round(sweep_freshness, 2),
        "sweep_level":     sweep_level,
        "swing_high":      round(swing_high, 6),
        "swing_low":       round(swing_low, 6),
        "equal_highs":     equal_highs,
        "equal_lows":      equal_lows,
    }


def _vwap_retest(df: pd.DataFrame) -> Dict:
    """
    VWAP Retest: precio tocó el VWAP y rebotó (señal de entry de alta precisión).
    Instituciones usan el VWAP como precio justo de ejecución.

    Bullish retest: precio bajó a VWAP desde arriba y rebotó hacia arriba
    Bearish retest: precio subió a VWAP desde abajo y rebotó hacia abajo
    """
    if len(df) < 10:
        return {"bullish_retest": False, "bearish_retest": False,
                "vwap": None, "distance_pct": 0.0}

    vwap_v = _vwap(df)
    price  = float(df["close"].iloc[-1])

    # Distancia actual al VWAP
    dist_pct = (price - vwap_v) / vwap_v * 100 if vwap_v > 0 else 0.0

    # Últimas 3 velas para detectar retest
    recent = df.tail(5)
    lows   = recent["low"].values
    highs  = recent["high"].values
    closes = recent["close"].values
    opens  = recent["open"].values

    atr_v    = float(_atr_series(df).iloc[-1])
    tolerance= atr_v * 0.5   # Holgura para considerar "toque" del VWAP

    bullish_retest = False
    bearish_retest = False

    for i in range(len(closes) - 1):
        # Toque bullish: vela bajó hasta cerca del VWAP (low dentro del rango)
        if abs(lows[i] - vwap_v) <= tolerance and closes[i] > vwap_v:
            # La siguiente vela confirma el rebote
            if closes[i+1] > closes[i] and closes[i+1] > vwap_v:
                bullish_retest = True

        # Toque bearish: vela subió hasta cerca del VWAP (high dentro del rango)
        if abs(highs[i] - vwap_v) <= tolerance and closes[i] < vwap_v:
            if closes[i+1] < closes[i] and closes[i+1] < vwap_v:
                bearish_retest = True

    return {
        "bullish_retest": bullish_retest,
        "bearish_retest": bearish_retest,
        "vwap":           round(vwap_v, 6),
        "distance_pct":   round(dist_pct, 3),
        "above_vwap":     price > vwap_v,
    }


def _detect_choch_bos(df: pd.DataFrame, lookback: int = 30) -> Dict:
    """
    Change of Character (ChoCH) y Break of Structure (BoS).

    BoS alcista:  nuevo HH rompe el último swing high (continuación)
    BoS bajista:  nuevo LL rompe el último swing low  (continuación)
    ChoCH alcista: primer HL después de un downtrend  (possible reversión)
    ChoCH bajista: primer LH después de un uptrend    (possible reversión)
    """
    if len(df) < lookback:
        return {"bos_bullish": False, "bos_bearish": False,
                "choch_bullish": False, "choch_bearish": False,
                "last_swing_high": None, "last_swing_low": None}

    window = df.tail(lookback).reset_index(drop=True)
    n      = len(window)
    closes = window["close"].values
    highs  = window["high"].values
    lows   = window["low"].values

    # Extraer swing points
    swing_highs = [(i, highs[i]) for i in range(1, n-1)
                   if highs[i] > highs[i-1] and highs[i] > highs[i+1]]
    swing_lows  = [(i, lows[i])  for i in range(1, n-1)
                   if lows[i]  < lows[i-1]  and lows[i]  < lows[i+1]]

    bos_bullish = bos_bearish = choch_bullish = choch_bearish = False
    last_sh = swing_highs[-1][1] if swing_highs else None
    last_sl = swing_lows[-1][1]  if swing_lows  else None

    current_high = float(window["high"].iloc[-1])
    current_low  = float(window["low"].iloc[-1])

    if len(swing_highs) >= 2:
        prev_sh, last_sh_val = swing_highs[-2][1], swing_highs[-1][1]
        if current_high > last_sh_val:              # BoS alcista
            bos_bullish = True
        elif last_sh_val < prev_sh and current_high > last_sh_val:  # ChoCH
            choch_bullish = True

    if len(swing_lows) >= 2:
        prev_sl, last_sl_val = swing_lows[-2][1], swing_lows[-1][1]
        if current_low < last_sl_val:               # BoS bajista
            bos_bearish = True
        elif last_sl_val > prev_sl and current_low < last_sl_val:   # ChoCH
            choch_bearish = True

    # ChoCH más sofisticado: primera ruptura contraria de estructura
    if len(swing_lows) >= 3:
        # Downtrend: últimos 3 LL's — ahora precio por encima del último pivot high
        if all(swing_lows[i+1][1] < swing_lows[i][1] for i in range(-3, -1)):
            if len(swing_highs) >= 1 and current_high > swing_highs[-1][1]:
                choch_bullish = True

    if len(swing_highs) >= 3:
        # Uptrend: últimos 3 HH's — ahora precio por debajo del último pivot low
        if all(swing_highs[i+1][1] > swing_highs[i][1] for i in range(-3, -1)):
            if len(swing_lows) >= 1 and current_low < swing_lows[-1][1]:
                choch_bearish = True

    return {
        "bos_bullish":    bos_bullish,
        "bos_bearish":    bos_bearish,
        "choch_bullish":  choch_bullish,
        "choch_bearish":  choch_bearish,
        "last_swing_high": round(last_sh, 6) if last_sh else None,
        "last_swing_low":  round(last_sl, 6) if last_sl  else None,
    }


def _premium_discount(df: pd.DataFrame, lookback: int = 50) -> Dict:
    """
    Premium / Discount zones:
    Precio en Discount (< 50% del rango) → zona de compra institucional
    Precio en Premium  (> 50% del rango) → zona de venta institucional

    Se calcula en base al rango de los últimos N cierres.
    """
    if len(df) < lookback:
        return {"zone": "equilibrium", "pct_in_range": 0.5}

    window   = df.tail(lookback)
    rng_high = float(window["high"].max())
    rng_low  = float(window["low"].min())
    price    = float(df["close"].iloc[-1])

    if rng_high == rng_low:
        return {"zone": "equilibrium", "pct_in_range": 0.5,
                "range_high": rng_high, "range_low": rng_low}

    pct = (price - rng_low) / (rng_high - rng_low)
    zone = "discount" if pct < 0.35 else ("premium" if pct > 0.65 else "equilibrium")

    return {
        "zone":        zone,
        "pct_in_range": round(pct, 3),
        "range_high":  round(rng_high, 6),
        "range_low":   round(rng_low, 6),
    }


# ══════════════════════════════════════════════════════════
#  SCORING SMC POR TIMEFRAME
# ══════════════════════════════════════════════════════════

def score_tf(df: pd.DataFrame, tf_label: str = "") -> Tuple[float, Dict]:
    """
    Score SMC de -10 a +10 para un timeframe.
    Retorna (score, detailed_dict) con todos los datos crudos SMC.
    """
    cfg_min = TF_CONFIG.get(tf_label, {}).get("min_bars", 60)
    if len(df) < cfg_min:
        return 0.0, {"skip": "not enough bars"}

    c      = df["close"]
    price  = float(c.iloc[-1])
    score  = 0.0
    det    = {}

    atr_v = float(_atr_series(df).iloc[-1])
    det["atr"]   = round(atr_v, 6)
    det["price"] = round(price, 6)

    # ── 1. EMAs Institucionales (7, 25, 99) ──────────────────────────────────
    e7  = float(_ema(c, 7).iloc[-1])
    e25 = float(_ema(c, 25).iloc[-1])
    e99 = float(_ema(c, 99).iloc[-1]) if len(df) >= 99 else None

    ema_s = 0.0
    # Alineación perfecta: precio > 7 > 25 > 99 = confluencia institucional bullish
    if e99 is not None:
        if price > e7 > e25 > e99:  ema_s = 2.5
        elif price < e7 < e25 < e99: ema_s = -2.5
        elif price > e25 > e99:     ema_s = 1.5
        elif price < e25 < e99:     ema_s = -1.5
        elif price > e7 > e25:      ema_s = 0.8
        elif price < e7 < e25:      ema_s = -0.8
        else:                        ema_s = 0.2 if price > e99 else -0.2
    else:
        if price > e7 > e25: ema_s = 1.5
        elif price < e7 < e25: ema_s = -1.5
        elif price > e7:       ema_s = 0.5
        else:                   ema_s = -0.5

    # Golden/Death cross en EMAs institucionales (7 cruza 25)
    e7_prev = float(_ema(c, 7).iloc[-2])
    e25_prev= float(_ema(c, 25).iloc[-2])
    if e7_prev <= e25_prev and e7 > e25: ema_s += 1.5   # golden cross 7/25
    elif e7_prev >= e25_prev and e7 < e25: ema_s -= 1.5 # death cross 7/25

    score += ema_s
    det["ema_s"] = round(ema_s, 2)
    det["ema7"]  = round(e7, 6)
    det["ema25"] = round(e25, 6)
    det["ema99"] = round(e99, 6) if e99 else None

    # ── 2. SuperTrend (10, 3) ─────────────────────────────────────────────────
    _, st_dir = _supertrend(df)
    st_s = 1.5 if int(st_dir.iloc[-1]) == 1 else -1.5
    if int(st_dir.iloc[-1]) != int(st_dir.iloc[-2]):
        st_s *= 2.0   # flip = señal fuerte
    score += st_s
    det["st_s"]  = round(st_s, 2)
    det["st_dir"] = int(st_dir.iloc[-1])

    # ── 3. RSI (solo como contexto, no como señal principal) ─────────────────
    rsi_v = float(_rsi(c, 14).iloc[-1])
    # Solo señala zonas extremas como confirmación
    rsi_confirm = 0.0
    if rsi_v < 25:   rsi_confirm = 1.0    # extremo OS = confirma bullish SMC
    elif rsi_v > 75: rsi_confirm = -1.0   # extremo OB = confirma bearish SMC
    score += rsi_confirm
    det["rsi"]          = round(rsi_v, 1)
    det["rsi_confirm"]  = round(rsi_confirm, 2)

    # ── 4. MACD momentum ─────────────────────────────────────────────────────
    ml, ms, mh = _macd(c)
    mhv, mhp   = float(mh.iloc[-1]), float(mh.iloc[-2])
    if   mhv > 0 and mhp <= 0: macd_s = 2.0   # cross alcista
    elif mhv > 0 and mhv > mhp: macd_s = 0.8
    elif mhv > 0:               macd_s = 0.3
    elif mhv < 0 and mhp >= 0: macd_s = -2.0  # cross bajista
    elif mhv < 0 and mhv < mhp: macd_s = -0.8
    else:                        macd_s = -0.3
    score += macd_s
    det["macd_s"] = round(macd_s, 2)

    # ── 5. Order Blocks ───────────────────────────────────────────────────────
    ob_data = _detect_order_blocks(df)
    ob_s    = 0.0
    if ob_data["price_in_bullish_ob"]:
        ob = ob_data["bullish_ob"]
        freshness = max(0.3, 1.0 - ob["age"] / 20) if ob else 0.3
        ob_s = 3.0 * freshness   # +3.0 máximo si es reciente
    elif ob_data["price_in_bearish_ob"]:
        ob = ob_data["bearish_ob"]
        freshness = max(0.3, 1.0 - ob["age"] / 20) if ob else 0.3
        ob_s = -3.0 * freshness
    score += ob_s
    det["ob_s"]              = round(ob_s, 2)
    det["in_bullish_ob"]     = ob_data["price_in_bullish_ob"]
    det["in_bearish_ob"]     = ob_data["price_in_bearish_ob"]
    det["bullish_ob"]        = ob_data["bullish_ob"]
    det["bearish_ob"]        = ob_data["bearish_ob"]

    # ── 6. Fair Value Gaps ───────────────────────────────────────────────────
    fvg_data = _detect_fvg(df)
    fvg_s    = 0.0
    if fvg_data["price_in_bull_fvg"]:
        fvg = fvg_data["bullish_fvg"]
        freshness = max(0.3, 1.0 - fvg["age"] / 15) if fvg else 0.3
        fvg_s = 2.5 * freshness
    elif fvg_data["price_in_bear_fvg"]:
        fvg = fvg_data["bearish_fvg"]
        freshness = max(0.3, 1.0 - fvg["age"] / 15) if fvg else 0.3
        fvg_s = -2.5 * freshness
    score += fvg_s
    det["fvg_s"]         = round(fvg_s, 2)
    det["in_bull_fvg"]   = fvg_data["price_in_bull_fvg"]
    det["in_bear_fvg"]   = fvg_data["price_in_bear_fvg"]
    det["bullish_fvg"]   = fvg_data["bullish_fvg"]
    det["bearish_fvg"]   = fvg_data["bearish_fvg"]

    # ── 7. Liquidity Sweep ───────────────────────────────────────────────────
    sweep_data = _liquidity_sweep(df)
    sweep_s    = 0.0
    freshness  = sweep_data["sweep_freshness"]
    if sweep_data["bullish_sweep"]:
        sweep_s = 3.5 * freshness
    elif sweep_data["bearish_sweep"]:
        sweep_s = -3.5 * freshness
    score += sweep_s
    det["sweep_s"]        = round(sweep_s, 2)
    det["bullish_sweep"]  = sweep_data["bullish_sweep"]
    det["bearish_sweep"]  = sweep_data["bearish_sweep"]
    det["sweep_freshness"]= freshness
    det["sweep_level"]    = sweep_data["sweep_level"]
    det["equal_highs"]    = sweep_data["equal_highs"]
    det["equal_lows"]     = sweep_data["equal_lows"]

    # ── 8. VWAP Retest ───────────────────────────────────────────────────────
    vwap_data = _vwap_retest(df)
    vwap_s    = 0.0
    if vwap_data["bullish_retest"]:
        vwap_s = 2.0
    elif vwap_data["bearish_retest"]:
        vwap_s = -2.0
    elif vwap_data["above_vwap"]:
        vwap_s = 0.5    # precio sobre VWAP = ligero bullish
    else:
        vwap_s = -0.5
    score += vwap_s
    det["vwap_s"]          = round(vwap_s, 2)
    det["vwap"]            = vwap_data["vwap"]
    det["vwap_retest_bull"]= vwap_data["bullish_retest"]
    det["vwap_retest_bear"]= vwap_data["bearish_retest"]
    det["vwap_dist_pct"]   = vwap_data["distance_pct"]

    # ── 9. ChoCH / BoS ────────────────────────────────────────────────────────
    struct_data = _detect_choch_bos(df)
    struct_s    = 0.0
    if struct_data["bos_bullish"]:    struct_s += 2.0
    if struct_data["bos_bearish"]:    struct_s -= 2.0
    if struct_data["choch_bullish"]:  struct_s += 1.5
    if struct_data["choch_bearish"]:  struct_s -= 1.5
    score += struct_s
    det["struct_s"]      = round(struct_s, 2)
    det["bos_bullish"]   = struct_data["bos_bullish"]
    det["bos_bearish"]   = struct_data["bos_bearish"]
    det["choch_bullish"] = struct_data["choch_bullish"]
    det["choch_bearish"] = struct_data["choch_bearish"]

    # ── 10. Premium / Discount ────────────────────────────────────────────────
    pd_data = _premium_discount(df)
    pd_s    = 0.0
    if pd_data["zone"] == "discount":  pd_s = 0.8    # zona de compra institucional
    elif pd_data["zone"] == "premium": pd_s = -0.8   # zona de venta institucional
    score += pd_s
    det["pd_zone"] = pd_data["zone"]
    det["pd_pct"]  = pd_data["pct_in_range"]
    det["pd_s"]    = round(pd_s, 2)

    # ── 11. Volume spike ─────────────────────────────────────────────────────
    is_spike, vol_ratio = _volume_spike(df)
    candle_bull = float(c.iloc[-1]) > float(df["open"].iloc[-1])
    vol_s = 0.0
    if is_spike:
        vol_s = 1.5 if candle_bull else -1.5
    elif vol_ratio > 1.5:
        vol_s = 0.6 if candle_bull else -0.6
    score += vol_s
    det["vol_ratio"] = round(vol_ratio, 2)
    det["vol_s"]     = round(vol_s, 2)
    det["vol_spike"] = is_spike

    # ── 12. OBV trend ─────────────────────────────────────────────────────────
    obv_s_series = _obv(df)
    obv_ema      = _ema(obv_s_series, 20)
    obv_trending = float(obv_s_series.iloc[-1]) > float(obv_ema.iloc[-1])
    obs_s        = 0.6 if obv_trending else -0.6
    score += obs_s
    det["obv_s"] = round(obs_s, 2)

    # ── Estructura de mercado general ─────────────────────────────────────────
    struct_market = _market_structure(df)
    det["structure"] = struct_market

    # ── Squeeze Bollinger/Keltner ─────────────────────────────────────────────
    bu, bm, bl = _bollinger(c)
    ku, km, kl = _keltner_local(df)
    buv, blv = float(bu.iloc[-1]), float(bl.iloc[-1])
    kuv, klv = float(ku.iloc[-1]), float(kl.iloc[-1])
    squeeze = buv < kuv and blv > klv
    det["squeeze"] = squeeze

    score = max(-10.0, min(10.0, score))
    det["score"] = round(score, 3)
    return score, det


def _keltner_local(df: pd.DataFrame, p=20, mult=1.5):
    mid = _ema(df["close"], p)
    a   = _atr_series(df, p)
    return mid + mult * a, mid, mid - mult * a


# _keltner_local is defined above and called directly within score_tf


# ══════════════════════════════════════════════════════════
#  ANÁLISIS MULTI-TIMEFRAME — MOTOR PRINCIPAL
# ══════════════════════════════════════════════════════════

def analyze_symbol(client, symbol: str,
                   timeframes: List[str] = None,
                   fast_mode: bool = False) -> Dict[str, Any]:
    """
    Motor de análisis SMC Multi-Timeframe.
    Detecta automáticamente el setup óptimo:
      - AGGRESSIVE: sweep + OB frescos → entrada inmediata
      - MOMENTUM:   BoS confirmado + volumen → trend riding
      - STANDARD:   confluencia SMC multi-TF
    """
    if fast_mode:
        timeframes = ["15", "60", "240"]
    elif timeframes is None:
        timeframes = ALL_TF

    mark_price = client.get_mark_price(symbol)
    ob_raw     = client.get_orderbook(symbol, limit=25)

    tf_details:       Dict[str, Dict]   = {}
    weighted_scores:  Dict[str, float]  = {}
    primary_atr:      Optional[float]   = None
    any_squeeze       = False
    any_vol_spike     = False
    any_sweep         = False
    any_ob_hit        = False
    any_fvg_fill      = False
    any_vwap_retest   = False
    entry_mode        = MODE_STANDARD

    for tf in timeframes:
        cfg   = TF_CONFIG.get(tf, {"label": tf, "category": "mid", "weight": 0.1, "min_bars": 60})
        label = cfg["label"]
        limit = max(350, cfg["min_bars"] + 80)
        klines = client.get_klines(symbol, tf, limit=limit)
        df     = to_df(klines)
        if df.empty or len(df) < cfg["min_bars"]:
            continue

        sc, det = score_tf(df, tf)
        weighted_scores[tf] = sc * cfg["weight"]
        tf_details[label]   = {"tf": tf, "category": cfg["category"], **det}

        if det.get("squeeze"):          any_squeeze     = True
        if det.get("vol_spike"):        any_vol_spike   = True
        if det.get("bullish_sweep") or det.get("bearish_sweep"): any_sweep = True
        if det.get("in_bullish_ob") or det.get("in_bearish_ob"): any_ob_hit = True
        if det.get("in_bull_fvg") or det.get("in_bear_fvg"):     any_fvg_fill = True
        if det.get("vwap_retest_bull") or det.get("vwap_retest_bear"): any_vwap_retest = True

        if tf in ("60", "240") and det.get("atr"):
            primary_atr = det["atr"]

        time.sleep(0.04)

    if not weighted_scores:
        return _empty_result(symbol, mark_price)

    total_w   = sum(TF_CONFIG[tf]["weight"] for tf in weighted_scores)
    composite = sum(weighted_scores.values()) / total_w if total_w > 0 else 0.0

    # ── Bias por categoría ──────────────────────────────────
    def _bias(tfs):
        vals = [weighted_scores[tf] / TF_CONFIG[tf]["weight"]
                for tf in tfs if tf in weighted_scores]
        if not vals:
            return "NEUTRAL", 0.0
        avg = sum(vals) / len(vals)
        if avg >= 2.0:    return "LONG",  avg
        elif avg <= -2.0: return "SHORT", avg
        return "NEUTRAL", avg

    macro_bias, macro_avg = _bias(MACRO_TF)
    mid_bias,   mid_avg   = _bias(MID_TF)
    entry_bias, entry_avg = _bias(ENTRY_TF)

    # ── Orderbook imbalance ─────────────────────────────────
    ob_score = _ob_imbalance(ob_raw)
    composite = composite * 0.90 + ob_score * 0.10
    composite = max(-10.0, min(10.0, composite))

    # ── DETERMINAR MODO DE ENTRADA ─────────────────────────
    # SMC AGGRESSIVE: sweep + ob_hit (setup institucional clásico)
    if any_sweep and any_ob_hit and abs(composite) >= 2.5:
        entry_mode = MODE_AGGRESSIVE
        threshold  = 2.5
    # MOMENTUM: fvg_fill o vwap_retest + trend macro
    elif (any_fvg_fill or any_vwap_retest) and macro_bias != "NEUTRAL" and abs(composite) >= 3.0:
        entry_mode = MODE_MOMENTUM
        threshold  = 3.0
    else:
        entry_mode = MODE_STANDARD
        threshold  = 4.0

    # ── SEÑAL ─────────────────────────────────────────────
    signal = "FLAT"
    if composite >= threshold:
        if entry_mode == MODE_STANDARD:
            if macro_bias != "SHORT" and mid_bias != "SHORT":
                signal = "LONG"
        else:
            if macro_bias != "SHORT":
                signal = "LONG"
    elif composite <= -threshold:
        if entry_mode == MODE_STANDARD:
            if macro_bias != "LONG" and mid_bias != "LONG":
                signal = "SHORT"
        else:
            if macro_bias != "LONG":
                signal = "SHORT"

    aligned    = (macro_bias == mid_bias == entry_bias and macro_bias != "NEUTRAL")
    confidence = min(1.0, abs(composite) / 10.0)
    if aligned:             confidence = min(1.0, confidence * 1.30)
    if any_sweep:           confidence = min(1.0, confidence * 1.20)
    if any_ob_hit:          confidence = min(1.0, confidence * 1.15)
    if any_vwap_retest:     confidence = min(1.0, confidence * 1.10)
    if any_fvg_fill:        confidence = min(1.0, confidence * 1.08)

    # TP/SL dinámicos
    tp_mult = 1.8 if entry_mode == MODE_AGGRESSIVE else (2.2 if entry_mode == MODE_MOMENTUM else 2.5)
    sl_mult = 1.0 if entry_mode == MODE_AGGRESSIVE else (1.1 if entry_mode == MODE_MOMENTUM else 1.2)
    tp, sl  = _calc_tp_sl(signal, mark_price, primary_atr, tp_mult, sl_mult)

    # ── Datos crudos SMC para la IA ────────────────────────
    smc_summary = _build_smc_summary(tf_details, composite, any_sweep, any_ob_hit,
                                      any_fvg_fill, any_vwap_retest)

    return {
        "symbol":          symbol,
        "composite_score": round(composite, 3),
        "signal":          signal,
        "confidence":      round(confidence, 3),
        "aligned":         aligned,
        "entry_mode":      entry_mode,
        "threshold":       threshold,
        "macro_bias":      macro_bias,
        "mid_bias":        mid_bias,
        "entry_bias":      entry_bias,
        "macro_avg":       round(macro_avg, 2),
        "mid_avg":         round(mid_avg, 2),
        "entry_avg":       round(entry_avg, 2),
        "mark_price":      mark_price,
        "atr":             primary_atr,
        "tp":              tp,
        "sl":              sl,
        "ob_score":        round(ob_score, 3),
        "squeeze":         any_squeeze,
        "vol_spike":       any_vol_spike,
        # ── SMC raw data ─────────────────────────────────────────────────────
        "smc_sweep":       any_sweep,
        "smc_ob_hit":      any_ob_hit,
        "smc_fvg_fill":    any_fvg_fill,
        "smc_vwap_retest": any_vwap_retest,
        "smc_summary":     smc_summary,
        # ─────────────────────────────────────────────────────────────────────
        "tf_details":      tf_details,
        "ts":              int(time.time()),
    }


def _build_smc_summary(tf_details: Dict, composite: float,
                        any_sweep: bool, any_ob_hit: bool,
                        any_fvg_fill: bool, any_vwap_retest: bool) -> Dict:
    """Construye resumen SMC compacto para pasar a la IA."""
    summary = {
        "composite_score":  round(composite, 2),
        "sweep_detected":   any_sweep,
        "order_block_hit":  any_ob_hit,
        "fvg_fill":         any_fvg_fill,
        "vwap_retest":      any_vwap_retest,
        "setups_count":     sum([any_sweep, any_ob_hit, any_fvg_fill, any_vwap_retest]),
        "tf_evidence":      {},
    }
    # Recopilar evidencia por TF relevante
    for label, det in tf_details.items():
        tf_ev = {}
        if det.get("bullish_sweep") or det.get("bearish_sweep"):
            tf_ev["sweep"] = "BULL" if det.get("bullish_sweep") else "BEAR"
            tf_ev["sweep_freshness"] = det.get("sweep_freshness", 0)
        if det.get("in_bullish_ob"):
            ob = det.get("bullish_ob") or {}
            tf_ev["ob"] = f"BULL @ {ob.get('mid', '?'):.4f}" if isinstance(ob.get('mid'), float) else "BULL OB"
        elif det.get("in_bearish_ob"):
            ob = det.get("bearish_ob") or {}
            tf_ev["ob"] = f"BEAR @ {ob.get('mid', '?'):.4f}" if isinstance(ob.get('mid'), float) else "BEAR OB"
        if det.get("in_bull_fvg"):
            fvg = det.get("bullish_fvg") or {}
            tf_ev["fvg"] = f"BULL gap {fvg.get('low', '?'):.4f}-{fvg.get('high', '?'):.4f}" if isinstance(fvg.get('low'), float) else "BULL FVG"
        elif det.get("in_bear_fvg"):
            tf_ev["fvg"] = "BEAR FVG"
        if det.get("vwap_retest_bull"):
            tf_ev["vwap"] = f"BULL retest @ {det.get('vwap', '?')}"
        elif det.get("vwap_retest_bear"):
            tf_ev["vwap"] = f"BEAR retest @ {det.get('vwap', '?')}"
        if det.get("choch_bullish"):
            tf_ev["structure"] = "ChoCH BULL"
        elif det.get("choch_bearish"):
            tf_ev["structure"] = "ChoCH BEAR"
        elif det.get("bos_bullish"):
            tf_ev["structure"] = "BoS BULL"
        elif det.get("bos_bearish"):
            tf_ev["structure"] = "BoS BEAR"
        if tf_ev:
            summary["tf_evidence"][label] = tf_ev
    return summary


# ══════════════════════════════════════════════════════════
#  SCANNER DE MERCADO
# ══════════════════════════════════════════════════════════

def scan_best_opportunities(client, top_n: int = 5,
                             min_volume_usdt: float = 5_000_000) -> List[Dict]:
    STABLES = {"USDC","BUSD","TUSD","DAI","FDUSD","USDD","USDP"}
    tickers = client.get_tickers(category="linear")
    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym.replace("USDT","") in STABLES:
            continue
        try:
            vol24 = float(t.get("turnover24h", 0))
        except Exception:
            vol24 = 0
        if vol24 < min_volume_usdt:
            continue
        candidates.append((sym, vol24))

    candidates.sort(key=lambda x: x[1], reverse=True)
    results = []
    for sym, _ in candidates[:60]:
        try:
            a = analyze_symbol(client, sym, fast_mode=True)
            if a["signal"] != "FLAT" and a["confidence"] > 0.30:
                results.append(a)
        except Exception as e:
            log.debug(f"scan {sym}: {e}")
        time.sleep(0.06)

    results.sort(key=lambda x: abs(x["composite_score"]), reverse=True)
    return results[:top_n]


# ══════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════

def _ob_imbalance(ob: Dict) -> float:
    bids = ob.get("b", []) or []
    asks = ob.get("a", []) or []
    if not bids or not asks:
        return 0.0
    bv = sum(float(b[1]) for b in bids[:15])
    av = sum(float(a[1]) for a in asks[:15])
    tv = bv + av
    return round(((bv - av) / tv) * 3, 3) if tv > 0 else 0.0

def _calc_tp_sl(signal: str, price: float, atr_v: Optional[float],
                tp_mult: float = 2.5, sl_mult: float = 1.2):
    if not atr_v or not price:
        return None, None
    max_atr = price * 0.30
    atr_v   = min(atr_v, max_atr)
    if signal == "LONG":
        tp = round(price + atr_v * tp_mult, 4)
        sl = round(price - atr_v * sl_mult, 4)
        if tp <= price or sl >= price or sl <= 0:
            return None, None
        return tp, sl
    if signal == "SHORT":
        tp = round(price - atr_v * tp_mult, 4)
        sl = round(price + atr_v * sl_mult, 4)
        if tp >= price or tp <= 0 or sl <= price:
            return None, None
        return tp, sl
    return None, None

def _empty_result(symbol: str, price: float) -> Dict:
    return {
        "symbol": symbol, "composite_score": 0.0, "signal": "FLAT",
        "confidence": 0.0, "aligned": False, "entry_mode": MODE_STANDARD,
        "threshold": 4.0,
        "macro_bias": "NEUTRAL", "mid_bias": "NEUTRAL", "entry_bias": "NEUTRAL",
        "macro_avg": 0.0, "mid_avg": 0.0, "entry_avg": 0.0,
        "mark_price": price, "atr": None, "tp": None, "sl": None,
        "ob_score": 0.0, "squeeze": False, "vol_spike": False,
        "smc_sweep": False, "smc_ob_hit": False, "smc_fvg_fill": False,
        "smc_vwap_retest": False, "smc_summary": {},
        "tf_details": {}, "ts": int(time.time()),
    }


# ══════════════════════════════════════════════════════════
#  FORMATO TELEGRAM
# ══════════════════════════════════════════════════════════

def format_analysis_for_tg(a: Dict) -> str:
    sym    = a["symbol"]
    sig    = a["signal"]
    score  = a["composite_score"]
    conf   = a["confidence"]
    mode   = a.get("entry_mode", MODE_STANDARD)
    thresh = a.get("threshold", 4.0)

    mode_emoji = {"AGGRESSIVE": "⚡", "MOMENTUM": "🚀", "STANDARD": "📊"}.get(mode, "📊")
    sig_emoji  = "🟢 LONG" if sig == "LONG" else ("🔴 SHORT" if sig == "SHORT" else "⚪ FLAT")

    def _bar(s):
        n = int((s + 10) / 20 * 10)
        return f"[{'█'*n}{'░'*(10-n)}] {s:+.1f}"

    def _be(b):
        return "🟢" if b == "LONG" else ("🔴" if b == "SHORT" else "⚪")

    tfd = a.get("tf_details", {})
    tf_lines = []
    for label in ["1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1D","1W"]:
        d = tfd.get(label)
        if not d: continue
        s  = d.get("score", 0)
        st = "▲" if d.get("st_dir", 0) == 1 else "▼"
        sq = "⚡" if d.get("squeeze") else ""
        vs = "💥" if d.get("vol_spike") else ""
        sw = "🌊" if (d.get("bullish_sweep") or d.get("bearish_sweep")) else ""
        ob = "🧱" if (d.get("in_bullish_ob") or d.get("in_bearish_ob")) else ""
        fv = "🪟" if (d.get("in_bull_fvg") or d.get("in_bear_fvg")) else ""
        vr = "🎯" if (d.get("vwap_retest_bull") or d.get("vwap_retest_bear")) else ""
        tf_lines.append(
            f"  <code>{label:>3}</code> {_bar(s)} {st}{sq}{vs}{sw}{ob}{fv}{vr}  RSI:{d.get('rsi','-')}"
        )

    smc_badges = []
    if a.get("smc_sweep"):       smc_badges.append("🌊 SWEEP")
    if a.get("smc_ob_hit"):      smc_badges.append("🧱 ORDER BLOCK")
    if a.get("smc_fvg_fill"):    smc_badges.append("🪟 FVG FILL")
    if a.get("smc_vwap_retest"): smc_badges.append("🎯 VWAP RETEST")

    lines = [
        f"<b>📊 {sym}  {sig_emoji}</b>  {mode_emoji}<i>{mode}</i>",
        f"Score: {_bar(score)}  (umbral: {thresh})",
        f"Confianza: {conf:.0%}  {'✅ ALINEADO' if a.get('aligned') else '⚠️ PARCIAL'}",
        "",
        f"Macro {_be(a['macro_bias'])} <b>{a['macro_bias']}</b>  "
        f"Mid {_be(a['mid_bias'])} <b>{a['mid_bias']}</b>  "
        f"Entry {_be(a['entry_bias'])} <b>{a['entry_bias']}</b>",
        "",
        "<b>SMC Setups:</b> " + ("  ".join(smc_badges) if smc_badges else "ninguno detectado"),
        "",
        "<b>Timeframes:</b>",
    ] + tf_lines

    if a.get("tp"):
        lines.append(f"\nTP: <code>{a['tp']}</code>  SL: <code>{a['sl']}</code>")

    return "\n".join(lines)
