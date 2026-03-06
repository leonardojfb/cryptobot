"""
analysis_engine_bybit.py  v3  — Motor SNIPER + AGRESIVO
═══════════════════════════════════════════════════════
OBJETIVO: Máximo profit en mínimo tiempo.

ESTRATEGIAS IMPLEMENTADAS:
  1. MOMENTUM SCALPING  — entra en breakouts con volumen explosivo (TF bajos)
  2. TREND RIDING       — sigue tendencia confirmada multi-TF (mayor duración)
  3. SQUEEZE BREAKOUT   — detecta compresión BB/Keltner y entra en la explosión
  4. DIVERGENCE PLAY    — RSI/MACD divergencia con precio (reversals precisos)
  5. ORDERBOOK SWEEP    — detecta desequilibrio extremo bid/ask en tiempo real

TIMEFRAMES:
  Macro (dirección): 4h, 6h, 12h, 1D, 1W
  Mid (momentum):    30m, 1h, 2h
  Entry (timing):    1m, 3m, 5m, 15m

INDICADORES (13):
  Tendencia   : EMA 7/21/50/200, Golden/Death cross, SuperTrend(10,3), Ichimoku
  Momentum    : RSI(14)+divergencia, StochRSI(14,14,3,3), MACD(12,26,9)
  Volatilidad : Bollinger(20,2), Keltner(20,2) → Squeeze detector
  Precio      : Williams%R(14), Pivot S/R, Estructura HH/HL
  Volumen     : OBV, MFI(14), Volume spike detector, VWAP
  Order flow  : Bid/Ask imbalance top-25

SISTEMA DE SEÑAL MULTINIVEL:
  - score_tf()      → -10 a +10 por timeframe
  - analyze_symbol() → composite score ponderado
  - TRES modos de entrada según condición de mercado:
      AGGRESSIVE: squeeze detectado → umbral reducido a 2.5
      MOMENTUM:   volumen 3x + tendencia clara → umbral 3.0
      STANDARD:   análisis normal → umbral 4.0
  - Señal se genera si macro NO contradice (no requiere alineación perfecta)
"""

import time
import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

log = logging.getLogger("analysis")

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

# Modos de entrada
MODE_AGGRESSIVE = "AGGRESSIVE"   # squeeze/breakout  → umbral 2.5
MODE_MOMENTUM   = "MOMENTUM"     # vol spike+trend   → umbral 3.0
MODE_STANDARD   = "STANDARD"     # análisis normal   → umbral 4.0

# ══════════════════════════════════════════════════════════
#  INDICADORES
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

def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/p, adjust=False).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def _macd(s: pd.Series, fast=12, slow=26, sig=9):
    ml = _ema(s, fast) - _ema(s, slow)
    ms = _ema(ml, sig)
    return ml, ms, ml - ms

def _bollinger(s: pd.Series, p=20, k=2.0):
    m = s.rolling(p).mean()
    sd = s.rolling(p).std()
    return m + k*sd, m, m - k*sd

def _keltner(df: pd.DataFrame, p=20, mult=1.5):
    mid = _ema(df["close"], p)
    a   = _atr_series(df, p)
    return mid + mult*a, mid, mid - mult*a

def _atr_series(df: pd.DataFrame, p=14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([(df["high"]-df["low"]).abs(),
                    (df["high"]-pc).abs(),
                    (df["low"]-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()

def _stoch_rsi(s: pd.Series, rp=14, sp=14, sk=3, sd=3):
    r = _rsi(s, rp)
    lo, hi = r.rolling(sp).min(), r.rolling(sp).max()
    k = 100 * (r - lo) / (hi - lo + 1e-10)
    ks = k.rolling(sk).mean()
    return ks, ks.rolling(sd).mean()

def _williams_r(df: pd.DataFrame, p=14) -> pd.Series:
    hi = df["high"].rolling(p).max()
    lo = df["low"].rolling(p).min()
    return -100 * (hi - df["close"]) / (hi - lo + 1e-10)

def _obv(df: pd.DataFrame) -> pd.Series:
    return (np.sign(df["close"].diff().fillna(0)) * df["volume"]).cumsum()

def _mfi(df: pd.DataFrame, p=14) -> pd.Series:
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    mf  = tp * df["volume"]
    pos = mf.where(tp > tp.shift(1), 0).rolling(p).sum()
    neg = mf.where(tp < tp.shift(1), 0).rolling(p).sum()
    return 100 - 100 / (1 + pos / neg.replace(0, np.nan))

def _vwap(df: pd.DataFrame) -> float:
    pv = (df["close"] * df["volume"]).sum()
    v  = df["volume"].sum()
    return float(pv / v) if v > 0 else float(df["close"].iloc[-1])

def _supertrend(df: pd.DataFrame, p=10, mult=3.0) -> Tuple[pd.Series, pd.Series]:
    a = _atr_series(df, p)
    hl2 = (df["high"] + df["low"]) / 2
    up  = hl2 + mult * a
    dn  = hl2 - mult * a
    st  = pd.Series(np.nan, index=df.index)
    dir_= pd.Series(1, index=df.index, dtype=int)
    for i in range(1, len(df)):
        ub = up.iloc[i] if up.iloc[i] < up.iloc[i-1] or df["close"].iloc[i-1] > up.iloc[i-1] else up.iloc[i-1]
        lb = dn.iloc[i] if dn.iloc[i] > dn.iloc[i-1] or df["close"].iloc[i-1] < dn.iloc[i-1] else dn.iloc[i-1]
        if   dir_.iloc[i-1] == -1 and df["close"].iloc[i] > ub: dir_.iat[i] = 1
        elif dir_.iloc[i-1] ==  1 and df["close"].iloc[i] < lb: dir_.iat[i] = -1
        else: dir_.iat[i] = dir_.iloc[i-1]
        st.iat[i] = lb if dir_.iat[i] == 1 else ub
    return st, dir_

def _ichimoku(df: pd.DataFrame) -> Dict:
    t  = (df["high"].rolling(9).max()  + df["low"].rolling(9).min())  / 2
    k  = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    sa = ((t + k) / 2).shift(26)
    sb = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2).shift(26)
    return {"tenkan": t, "kijun": k, "senkou_a": sa, "senkou_b": sb}

def _pivot_sr(df: pd.DataFrame) -> Dict:
    if len(df) < 3:
        return {}
    h, l, c = float(df["high"].iloc[-2]), float(df["low"].iloc[-2]), float(df["close"].iloc[-2])
    p = (h + l + c) / 3
    return {"P": p, "R1": 2*p-l, "R2": p+(h-l), "S1": 2*p-h, "S2": p-(h-l)}

def _market_structure(df: pd.DataFrame) -> str:
    if len(df) < 20:
        return "ranging"
    closes = df["close"].tail(20).values
    peaks   = [closes[i] for i in range(1, len(closes)-1) if closes[i]>closes[i-1] and closes[i]>closes[i+1]]
    troughs = [closes[i] for i in range(1, len(closes)-1) if closes[i]<closes[i-1] and closes[i]<closes[i+1]]
    if len(peaks) >= 2 and len(troughs) >= 2:
        if peaks[-1] > peaks[-2] and troughs[-1] > troughs[-2]: return "bullish"
        if peaks[-1] < peaks[-2] and troughs[-1] < troughs[-2]: return "bearish"
    return "ranging"

def _detect_divergence(df: pd.DataFrame) -> Tuple[str, float]:
    """
    Detecta divergencias precio vs RSI.
    Retorna: ("bullish"|"bearish"|"none", fuerza 0-1)
    """
    if len(df) < 30:
        return "none", 0.0
    closes = df["close"].tail(30)
    rsi_s  = _rsi(closes, 14).tail(30)
    # Últimos 2 mínimos de precio vs RSI
    price_lows = [(i, float(closes.iloc[i])) for i in range(1, len(closes)-1)
                  if closes.iloc[i] < closes.iloc[i-1] and closes.iloc[i] < closes.iloc[i+1]]
    if len(price_lows) >= 2:
        p1, p2 = price_lows[-2], price_lows[-1]
        r1 = float(rsi_s.iloc[p1[0]])
        r2 = float(rsi_s.iloc[p2[0]])
        # Precio hace LL pero RSI hace HL → divergencia alcista
        if p2[1] < p1[1] and r2 > r1 + 2:
            strength = min(1.0, (r2 - r1) / 20)
            return "bullish", round(strength, 2)
    # Últimos 2 máximos de precio vs RSI
    price_highs = [(i, float(closes.iloc[i])) for i in range(1, len(closes)-1)
                   if closes.iloc[i] > closes.iloc[i-1] and closes.iloc[i] > closes.iloc[i+1]]
    if len(price_highs) >= 2:
        p1, p2 = price_highs[-2], price_highs[-1]
        r1 = float(rsi_s.iloc[p1[0]])
        r2 = float(rsi_s.iloc[p2[0]])
        # Precio hace HH pero RSI hace LH → divergencia bajista
        if p2[1] > p1[1] and r2 < r1 - 2:
            strength = min(1.0, (r1 - r2) / 20)
            return "bearish", round(strength, 2)
    return "none", 0.0

def _volume_spike(df: pd.DataFrame) -> Tuple[bool, float]:
    """Detecta spike de volumen (>2.5x media). Retorna (is_spike, ratio)"""
    if len(df) < 20:
        return False, 1.0
    vol_ma = float(df["volume"].rolling(20).mean().iloc[-1])
    vol_now = float(df["volume"].iloc[-1])
    ratio = vol_now / vol_ma if vol_ma > 0 else 1.0
    return ratio >= 2.5, round(ratio, 2)

# ══════════════════════════════════════════════════════════
#  SCORING POR TIMEFRAME
# ══════════════════════════════════════════════════════════

def score_tf(df: pd.DataFrame, tf_label: str = "") -> Tuple[float, Dict]:
    cfg_min = TF_CONFIG.get(tf_label, {}).get("min_bars", 60)
    if len(df) < cfg_min:
        return 0.0, {"skip": "not enough bars"}

    c     = df["close"]
    price = float(c.iloc[-1])
    score = 0.0
    det   = {}

    # ── 1. EMAs ───────────────────────────────────────────
    e7   = float(_ema(c,   7).iloc[-1])
    e21  = float(_ema(c,  21).iloc[-1])
    e50  = float(_ema(c,  50).iloc[-1])
    e200 = float(_ema(c, 200).iloc[-1]) if len(df) >= 200 else None

    if   price > e7 > e21 > e50: ema_s = 3.0
    elif price < e7 < e21 < e50: ema_s = -3.0
    elif price > e21 > e50:      ema_s = 1.5
    elif price < e21 < e50:      ema_s = -1.5
    elif price > e7:              ema_s = 0.5
    else:                         ema_s = -0.5
    if e200:
        ema_s += 1.0 if price > e200 else -1.0
    # Golden / Death cross
    e7p, e21p = float(_ema(c, 7).iloc[-2]), float(_ema(c, 21).iloc[-2])
    if   e7p <= e21p and e7 > e21: ema_s += 2.0   # golden cross fresco
    elif e7p >= e21p and e7 < e21: ema_s -= 2.0   # death cross fresco
    score += ema_s
    det["ema_s"] = round(ema_s, 2)

    # ── 2. SuperTrend ─────────────────────────────────────
    _, st_dir = _supertrend(df)
    st_s = 1.5 if int(st_dir.iloc[-1]) == 1 else -1.5
    if int(st_dir.iloc[-1]) != int(st_dir.iloc[-2]):
        st_s *= 2.0   # flip reciente = señal muy fuerte
    score += st_s
    det["st_s"]   = round(st_s, 2)
    det["st_dir"] = int(st_dir.iloc[-1])

    # ── 3. Ichimoku ───────────────────────────────────────
    ich = _ichimoku(df)
    tk  = float(ich["tenkan"].iloc[-1]) if not pd.isna(ich["tenkan"].iloc[-1]) else price
    kj  = float(ich["kijun"].iloc[-1])  if not pd.isna(ich["kijun"].iloc[-1])  else price
    idx = -27 if len(df) > 27 else -1
    sa  = float(ich["senkou_a"].iloc[idx]) if not pd.isna(ich["senkou_a"].iloc[idx]) else price
    sb  = float(ich["senkou_b"].iloc[idx]) if not pd.isna(ich["senkou_b"].iloc[idx]) else price
    cloud_top, cloud_bot = max(sa, sb), min(sa, sb)
    ichi_s = 0.0
    if   price > cloud_top: ichi_s += 1.5
    elif price < cloud_bot: ichi_s -= 1.5
    ichi_s += 0.5 if tk > kj else -0.5
    ichi_s += 0.3 if sa > sb else -0.3
    score += ichi_s
    det["ichi_s"] = round(ichi_s, 2)

    # ── 4. RSI + divergencia ──────────────────────────────
    rval     = float(_rsi(c, 14).iloc[-1])
    rval_prev= float(_rsi(c, 14).iloc[-2])
    if   rval < 20:  rsi_s = 3.0
    elif rval < 30:  rsi_s = 2.0
    elif rval < 40:  rsi_s = 0.8
    elif rval > 80:  rsi_s = -3.0
    elif rval > 70:  rsi_s = -2.0
    elif rval > 60:  rsi_s = -0.8
    else:             rsi_s = 0.0
    div_type, div_str = _detect_divergence(df)
    if div_type == "bullish":   rsi_s += 1.5 * div_str
    elif div_type == "bearish": rsi_s -= 1.5 * div_str
    score += rsi_s
    det["rsi"]      = round(rval, 1)
    det["rsi_s"]    = round(rsi_s, 2)
    det["div_type"] = div_type
    det["div_str"]  = div_str

    # ── 5. Stoch RSI ──────────────────────────────────────
    sk, sd_s = _stoch_rsi(c)
    skv, sdv = float(sk.iloc[-1]), float(sd_s.iloc[-1])
    skp, sdp = float(sk.iloc[-2]), float(sd_s.iloc[-2])
    if   skv < 20 and skv > sdv and skp <= sdp: stoch_s = 2.5
    elif skv < 20 and skv > sdv:                 stoch_s = 1.2
    elif skv > 80 and skv < sdv and skp >= sdp: stoch_s = -2.5
    elif skv > 80 and skv < sdv:                 stoch_s = -1.2
    else:                                         stoch_s = 0.0
    score += stoch_s
    det["stoch_k"] = round(skv, 1)
    det["stoch_s"] = round(stoch_s, 2)

    # ── 6. MACD ───────────────────────────────────────────
    ml, ms, mh = _macd(c)
    mhv, mhp = float(mh.iloc[-1]), float(mh.iloc[-2])
    if   mhv > 0 and mhp <= 0: macd_s = 3.0   # cross alcista
    elif mhv > 0 and mhv > mhp: macd_s = 1.2
    elif mhv > 0:               macd_s = 0.5
    elif mhv < 0 and mhp >= 0: macd_s = -3.0  # cross bajista
    elif mhv < 0 and mhv < mhp: macd_s = -1.2
    else:                        macd_s = -0.5
    score += macd_s
    det["macd_hist"] = round(mhv, 6)
    det["macd_s"]    = round(macd_s, 2)

    # ── 7. Bollinger + Keltner Squeeze ───────────────────
    bu, bm, bl = _bollinger(c)
    ku, km, kl = _keltner(df)
    buv, blv = float(bu.iloc[-1]), float(bl.iloc[-1])
    kuv, klv = float(ku.iloc[-1]), float(kl.iloc[-1])
    bb_s = 0.0
    if   price <= blv: bb_s = 2.0 if price < klv else 1.5
    elif price >= buv: bb_s = -2.0 if price > kuv else -1.5
    # Squeeze: BB dentro de Keltner → energía acumulada
    squeeze = buv < kuv and blv > klv
    if squeeze:
        # Bonus por dirección dentro del squeeze
        bb_s += 0.5 if price > float(bm.iloc[-1]) else -0.5
    score += bb_s
    det["bb_s"]    = round(bb_s, 2)
    det["squeeze"] = squeeze

    # ── 8. Williams %R ────────────────────────────────────
    wr = float(_williams_r(df).iloc[-1])
    if   wr < -85: wr_s = 1.5
    elif wr < -70: wr_s = 0.8
    elif wr > -15: wr_s = -1.5
    elif wr > -30: wr_s = -0.8
    else:           wr_s = 0.0
    score += wr_s
    det["wr"]   = round(wr, 1)
    det["wr_s"] = round(wr_s, 2)

    # ── 9. MFI ───────────────────────────────────────────
    mfi_v = float(_mfi(df).iloc[-1])
    if   mfi_v < 20: mfi_s = 1.5
    elif mfi_v < 30: mfi_s = 0.8
    elif mfi_v > 80: mfi_s = -1.5
    elif mfi_v > 70: mfi_s = -0.8
    else:             mfi_s = 0.0
    score += mfi_s
    det["mfi"]   = round(mfi_v, 1)
    det["mfi_s"] = round(mfi_s, 2)

    # ── 10. OBV ───────────────────────────────────────────
    obv_s = _obv(df)
    obv_e = _ema(obv_s, 20)
    obv_trending = float(obv_s.iloc[-1]) > float(obv_e.iloc[-1])
    obs_s = 0.8 if obv_trending else -0.8
    score += obs_s
    det["obv_s"] = round(obs_s, 2)

    # ── 11. Volumen spike ─────────────────────────────────
    is_spike, vol_ratio = _volume_spike(df)
    candle_bull = float(c.iloc[-1]) > float(df["open"].iloc[-1])
    if is_spike:
        vol_s = 2.0 if candle_bull else -2.0   # spike con dirección = señal fuerte
    elif vol_ratio > 1.5:
        vol_s = 0.8 if candle_bull else -0.8
    else:
        vol_s = 0.0
    score += vol_s
    det["vol_ratio"] = round(vol_ratio, 2)
    det["vol_s"]     = round(vol_s, 2)
    det["vol_spike"] = is_spike

    # ── 12. Estructura de mercado ─────────────────────────
    struct = _market_structure(df)
    struct_s = 1.2 if struct == "bullish" else (-1.2 if struct == "bearish" else 0.0)
    score += struct_s
    det["structure"]  = struct
    det["struct_s"]   = round(struct_s, 2)

    # ── 13. VWAP ─────────────────────────────────────────
    vwap_v = _vwap(df)
    vwap_dist = (price - vwap_v) / vwap_v if vwap_v > 0 else 0
    vwap_s = 0.8 if price > vwap_v else -0.8
    # Precio muy lejos del VWAP = posible reversión
    if abs(vwap_dist) > 0.02:
        vwap_s *= 0.5
    score += vwap_s
    det["vwap"]   = round(vwap_v, 4)
    det["vwap_s"] = round(vwap_s, 2)

    # ── ATR ───────────────────────────────────────────────
    atr_v = float(_atr_series(df).iloc[-1])
    det["atr"]   = round(atr_v, 6)
    det["price"] = round(price, 6)

    score = max(-10.0, min(10.0, score))
    det["score"] = round(score, 3)
    return score, det


# ══════════════════════════════════════════════════════════
#  ANÁLISIS MULTI-TIMEFRAME — MOTOR PRINCIPAL
# ══════════════════════════════════════════════════════════

def analyze_symbol(client, symbol: str,
                   timeframes: List[str] = None,
                   fast_mode: bool = False) -> Dict[str, Any]:
    """
    Motor de análisis SNIPER + AGRESIVO.
    Detecta automáticamente el modo de entrada óptimo:
      - AGGRESSIVE: squeeze detectado → entra rápido antes del breakout
      - MOMENTUM:   volumen explosivo + tendencia → entra fuerte
      - STANDARD:   análisis técnico puro multi-TF
    """
    if fast_mode:
        timeframes = ["15", "60", "240"]
    elif timeframes is None:
        timeframes = ALL_TF

    mark_price = client.get_mark_price(symbol)
    ob         = client.get_orderbook(symbol, limit=25)

    tf_details: Dict[str, Dict]   = {}
    weighted_scores: Dict[str, float] = {}
    primary_atr: Optional[float]  = None
    any_squeeze = False
    any_vol_spike = False
    entry_mode = MODE_STANDARD

    for tf in timeframes:
        cfg   = TF_CONFIG.get(tf, {"label": tf, "category": "mid", "weight": 0.1, "min_bars": 60})
        label = cfg["label"]
        limit = max(300, cfg["min_bars"] + 50)
        klines = client.get_klines(symbol, tf, limit=limit)
        df     = to_df(klines)
        if df.empty or len(df) < cfg["min_bars"]:
            continue

        sc, det = score_tf(df, tf)
        weighted_scores[tf]  = sc * cfg["weight"]
        tf_details[label]    = {"tf": tf, "category": cfg["category"], **det}

        if det.get("squeeze"):
            any_squeeze = True
        if det.get("vol_spike"):
            any_vol_spike = True
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
    ob_score = _ob_imbalance(ob)
    composite = composite * 0.90 + ob_score * 0.10
    composite = max(-10.0, min(10.0, composite))

    # ── DETERMINAR MODO DE ENTRADA ─────────────────────────
    # 1. Squeeze: umbral bajo, entrar antes del breakout
    if any_squeeze and abs(composite) >= 2.5:
        entry_mode = MODE_AGGRESSIVE
        threshold  = 2.5
    # 2. Volumen spike + tendencia clara: momentum trade
    elif any_vol_spike and macro_bias != "NEUTRAL" and abs(composite) >= 3.0:
        entry_mode = MODE_MOMENTUM
        threshold  = 3.0
    # 3. Estándar: análisis completo
    else:
        entry_mode = MODE_STANDARD
        threshold  = 4.0

    # ── SEÑAL ──────────────────────────────────────────────
    # Modo AGGRESSIVE/MOMENTUM: macro NO debe contradecir (no requiere alineación)
    # Modo STANDARD: macro + mid deben estar de acuerdo
    signal = "FLAT"
    if composite >= threshold:
        if entry_mode == MODE_STANDARD:
            if macro_bias != "SHORT" and mid_bias != "SHORT":
                signal = "LONG"
        else:
            if macro_bias != "SHORT":   # solo bloquear si macro es opuesto
                signal = "LONG"
    elif composite <= -threshold:
        if entry_mode == MODE_STANDARD:
            if macro_bias != "LONG" and mid_bias != "LONG":
                signal = "SHORT"
        else:
            if macro_bias != "LONG":
                signal = "SHORT"

    aligned = (macro_bias == mid_bias == entry_bias and macro_bias != "NEUTRAL")
    confidence = min(1.0, abs(composite) / 10.0)
    if aligned:       confidence = min(1.0, confidence * 1.3)
    if any_squeeze:   confidence = min(1.0, confidence * 1.15)
    if any_vol_spike: confidence = min(1.0, confidence * 1.10)

    # TP/SL dinámicos según modo
    tp_mult = 1.8 if entry_mode == MODE_AGGRESSIVE else (2.2 if entry_mode == MODE_MOMENTUM else 2.5)
    sl_mult = 1.0 if entry_mode == MODE_AGGRESSIVE else (1.1 if entry_mode == MODE_MOMENTUM else 1.2)
    tp, sl  = _calc_tp_sl(signal, mark_price, primary_atr, tp_mult, sl_mult)

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
        "tf_details":      tf_details,
        "ts":              int(time.time()),
    }


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
            # En el scan, umbral más bajo para no perder oportunidades
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
    # Cap ATR al 30% del precio para evitar TPs negativos (ej: POWERUSDT)
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
        return "🟢" if b=="LONG" else ("🔴" if b=="SHORT" else "⚪")

    tfd = a.get("tf_details", {})
    tf_lines = []
    for label in ["1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1D","1W"]:
        d = tfd.get(label)
        if not d: continue
        s  = d.get("score", 0)
        st = "▲" if d.get("st_dir", 0) == 1 else "▼"
        sq = "⚡" if d.get("squeeze") else ""
        vs = "💥" if d.get("vol_spike") else ""
        dv = f" div:{d.get('div_type','')[:3]}" if d.get("div_type","none") != "none" else ""
        tf_lines.append(
            f"  <code>{label:>3}</code> {_bar(s)} {st}{sq}{vs}  RSI:{d.get('rsi','-')}{dv}"
        )

    lines = [
        f"<b>📊 {sym}  {sig_emoji}</b>  {mode_emoji}<i>{mode}</i>",
        f"Score: {_bar(score)}  (umbral: {thresh})",
        f"Confianza: {conf:.0%}  {'✅ ALINEADO' if a.get('aligned') else '⚠️ PARCIAL'}",
        "",
        f"Macro {_be(a['macro_bias'])} <b>{a['macro_bias']}</b>  "
        f"Mid {_be(a['mid_bias'])} <b>{a['mid_bias']}</b>  "
        f"Entry {_be(a['entry_bias'])} <b>{a['entry_bias']}</b>",
        "",
        "<b>Timeframes:</b>",
    ] + tf_lines

    extras = []
    if a.get("squeeze"):   extras.append("⚡ SQUEEZE — explosión inminente")
    if a.get("vol_spike"): extras.append("💥 VOLUMEN EXPLOSIVO")
    if a.get("tp"):        extras.append(f"TP: <code>{a['tp']}</code>  SL: <code>{a['sl']}</code>")
    if extras:
        lines.append("")
        lines.extend(extras)

    return "\n".join(lines)
