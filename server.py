#!/usr/bin/env python3
# server.py — Webhook de Trading (Conexión + Lógica completa)
# Endpoints:
#   GET  /                     -> health
#   POST /webhook_test         -> prueba de conexión (valida secret, NO opera)
#   POST /signal               -> valida secret; SIMULA si ENABLE_TRADING=false; OPERA si true
#   POST /cancel_brackets      -> cancela órdenes abiertas (TP/SL)
#   POST /close_position       -> cierra posición al mercado (reduceOnly) y cancela brackets
#   GET  /filters/{symbol}     -> muestra filtros (minQty/stepSize/tickSize) del símbolo
#
# .env esperado:
#   SIGNAL_SECRET=tu_secreto_fuerte
#   ENABLE_TRADING=false
#   BINANCE_TESTNET=true
#   BINANCE_API_KEY=...
#   BINANCE_API_SECRET=...
#   TELEGRAM_BOT_TOKEN=
#   TELEGRAM_CHAT_ID=

import os
import math
import time
import traceback
from typing import Optional, Dict, Any

import requests
from fastapi import FastAPI, Request, HTTPException, Header
from dotenv import load_dotenv
from decimal import Decimal, ROUND_DOWN
from fastapi import Body
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from proposal_manager import store as proposal_store
from position_manager import PM
from order_router import open_order, close_order



# -------------------- Carga .env y config --------------------
load_dotenv()

def as_bool(v: str, default=False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

SECRET          = (os.getenv("SIGNAL_SECRET") or "").strip()
ENABLE_TRADING  = as_bool(os.getenv("ENABLE_TRADING"), default=True)
BINANCE_TESTNET = as_bool(os.getenv("BINANCE_TESTNET"), default=True)
API_KEY         = (os.getenv("BINANCE_API_KEY") or "").strip()
API_SECRET      = (os.getenv("BINANCE_API_SECRET") or "").strip()
TG_TOKEN        = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TG_CHAT         = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

# Cliente Binance (solo si trading activado)
client = None
if ENABLE_TRADING:
    try:
        from binance.client import Client
        client = Client(API_KEY, API_SECRET, testnet=BINANCE_TESTNET)
        if BINANCE_TESTNET:
            client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
    except Exception as e:
        raise RuntimeError(f"ENABLE_TRADING=true pero Binance no inicializa: {e}")

app = FastAPI(title="Trading Webhook API", version="1.0.0")

# -------------------- Utilidades --------------------
# ==== PATCH: modelos Proposal/Order ====
class OrderSpec(BaseModel):
    action: str = Field(..., pattern="^(OPEN|CLOSE)$")
    symbol: str
    side: Optional[str] = Field(None, pattern="^(BUY|SELL|LONG|SHORT)$")
    entry: Dict[str, Any] = Field(default_factory=lambda: {"type":"MARKET"})
    risk: Optional[Dict[str, Any]] = None
    tp: Optional[Any] = None
    sl: Optional[Any] = None

class ProposalIn(BaseModel):
    order: OrderSpec
    snapshot: Dict[str, Any] = Field(default_factory=dict)
    expires_in_sec: int = 60

class ProposalOut(BaseModel):
    id: str
    status: str
    created_at: float
    order: Dict[str, Any]
    snapshot_hash: str
# ==== PATCH END ====

def log(*args, **kwargs):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *args, **kwargs, flush=True)

def tg(msg: str):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=8
        )
    except Exception as e:
        log("Telegram send error:", e)

def check_secret(x_secret: str, body: Dict[str, Any]):
    """Valida X-SECRET o body['secret'] contra SIGNAL_SECRET (si está configurado)."""
    if not SECRET:
        return
    ok = False
    if x_secret and x_secret == SECRET:
        ok = True
    if not ok and isinstance(body, dict) and body.get("secret") == SECRET:
        ok = True
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized")

# -------------------- Precisión (Decimal) --------------------
def _decimals_from_step(step: float) -> int:
    s = f"{step:.10f}".rstrip('0')
    if '.' in s:
        return len(s.split('.')[1])
    return 0

def fmt_step(value: float, step: float) -> str:
    """Cuantiza hacia abajo al múltiplo de step y devuelve string con decimales correctos."""
    decs = _decimals_from_step(step)
    q = Decimal(str(step))
    v = (Decimal(str(value)) / q).to_integral_value(rounding=ROUND_DOWN) * q
    return f"{v:.{decs}f}"

def fmt_tick(value: float, tick: float) -> str:
    """Cuantiza hacia abajo al múltiplo de tick y devuelve string con decimales correctos."""
    decs = _decimals_from_step(tick)
    q = Decimal(str(tick))
    v = (Decimal(str(value)) / q).to_integral_value(rounding=ROUND_DOWN) * q
    return f"{v:.{decs}f}"

def round_tick_float(value: float, tick: float) -> float:
    """Equivalente float (para cálculo/log), redondeo hacia abajo por tick."""
    if tick <= 0:
        return value
    decs = _decimals_from_step(tick)
    v = math.floor(Decimal(str(value)) / Decimal(str(tick))) * Decimal(str(tick))
    return float(round(v, decs))

# -------------------- Filtros y helpers mercado --------------------
def futures_filters(symbol: str):
    if client is None:
        # Para poder consultar filters con trading desactivado,
        # levantamos un cliente efímero SOLO lectura si hay credenciales.
        from binance.client import Client as C2
        tmp = C2(API_KEY, API_SECRET, testnet=BINANCE_TESTNET) if API_KEY and API_SECRET else None
        if tmp and BINANCE_TESTNET:
            tmp.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        ex = (tmp or client).futures_exchange_info()
    else:
        ex = client.futures_exchange_info()

    sym = next((s for s in ex["symbols"] if s["symbol"] == symbol.upper()), None)
    if not sym:
        raise HTTPException(status_code=400, detail=f"Symbol {symbol} not found in exchange info")
    lot  = next((f for f in sym["filters"] if f["filterType"] == "LOT_SIZE"), None)
    pf   = next((f for f in sym["filters"] if f["filterType"] == "PRICE_FILTER"), None)
    if not lot or not pf:
        raise HTTPException(status_code=500, detail="Filters not found for symbol")
    min_qty   = float(lot["minQty"])
    step_size = float(lot["stepSize"])
    tick_size = float(pf["tickSize"])
    return min_qty, step_size, tick_size

def ensure_isolated_and_leverage(symbol: str, lev: int):
    try:
        client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
    except Exception:
        pass
    try:
        client.futures_change_leverage(symbol=symbol, leverage=int(lev))
    except Exception as e:
        log("Warn leverage:", e)

# -------------------- Core: abrir orden con brackets --------------------
def place_market_with_brackets(
    symbol: str,
    side: str,  # "BUY" o "SELL"
    sl_pct: float = 0.004,
    tp_pct: float = 0.008,
    risk_usdt: Optional[float] = None,
    fixed_qty: Optional[float] = None,
    leverage: int = 10,
    # parciales / trailing
    tp1_pct: Optional[float] = None,
    tp1_ratio: float = 0.5,
    trailing_callback: Optional[float] = None
) -> Dict[str, Any]:
    symbol = symbol.upper()
    log("place_market_with_brackets:", symbol, side, "lev", leverage)

    ensure_isolated_and_leverage(symbol, leverage)

    # Precio de marca
    mark_info = client.futures_mark_price(symbol=symbol)
    mark = float(mark_info["markPrice"])

    # Filtros
    min_qty, step, tick = futures_filters(symbol)

    # Precios SL/TP
    if side == "BUY":
        sl_price  = round_tick_float(mark * (1 - sl_pct), tick)
        tp2_price = round_tick_float(mark * (1 + tp_pct), tick)
        if tp1_pct is not None:
            tp1_price = round_tick_float(mark * (1 + tp1_pct), tick)
    else:
        sl_price  = round_tick_float(mark * (1 + sl_pct), tick)
        tp2_price = round_tick_float(mark * (1 - tp_pct), tick)
        if tp1_pct is not None:
            tp1_price = round_tick_float(mark * (1 - tp1_pct), tick)

    entry_price = round_tick_float(mark, tick)

    # Qty
    if risk_usdt is not None:
        dist = abs(entry_price - sl_price)
        if dist <= 0:
            raise HTTPException(status_code=400, detail="SL inválido: distancia <= 0")
        qty = float(risk_usdt) / dist
    else:
        qty = float(fixed_qty) if fixed_qty is not None else step

    # Ajuste a stepSize (string con precisión correcta)
    qty_str = fmt_step(qty, step)
    if Decimal(qty_str) < Decimal(str(min_qty)):
        raise HTTPException(status_code=400, detail=f"qty {qty_str} < minQty {min_qty}")

    # Strings de precios con precisión correcta
    sl_str  = fmt_tick(sl_price,  tick)
    tp2_str = fmt_tick(tp2_price, tick)
    if tp1_pct is not None:
        tp1_str = fmt_tick(tp1_price, tick)

    # MARKET
    try:
        order = client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty_str
        )
    except Exception as e:
        log("Market order error:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Market order error: {e}")

    # SL (closePosition)
    close_side = "SELL" if side == "BUY" else "BUY"
    try:
        client.futures_create_order(
            symbol=symbol, side=close_side,
            type="STOP_MARKET", stopPrice=sl_str,
            closePosition=True, workingType="MARK_PRICE"
        )
    except Exception as e:
        log("Error placing SL:", e)

    # TPs
    try:
        if tp1_pct is not None and 0 < float(tp1_ratio) < 1:
            # qty parcial
            qty_tp1 = Decimal(qty_str) * Decimal(str(tp1_ratio))
            qty_tp1_str = fmt_step(float(qty_tp1), step)
            # seguridad
            if Decimal(qty_tp1_str) >= Decimal(qty_str):
                qty_tp1_str = fmt_step(float(Decimal(qty_str) * Decimal("0.5")), step)

            # TP1 parcial reduceOnly
            client.futures_create_order(
                symbol=symbol, side=close_side,
                type="TAKE_PROFIT_MARKET", stopPrice=tp1_str,
                reduceOnly=True, quantity=qty_tp1_str, workingType="MARK_PRICE"
            )
            # TP2 cierra resto
            client.futures_create_order(
                symbol=symbol, side=close_side,
                type="TAKE_PROFIT_MARKET", stopPrice=tp2_str,
                closePosition=True, workingType="MARK_PRICE"
            )
        else:
            # TP único
            client.futures_create_order(
                symbol=symbol, side=close_side,
                type="TAKE_PROFIT_MARKET", stopPrice=tp2_str,
                closePosition=True, workingType="MARK_PRICE"
            )
    except Exception as e:
        log("Error placing TP orders:", e)

    # Trailing (opcional)
    if trailing_callback is not None and float(trailing_callback) > 0:
        try:
            client.futures_create_order(
                symbol=symbol, side=close_side,
                type="TRAILING_STOP_MARKET",
                callbackRate=float(trailing_callback),
                workingType="MARK_PRICE",
                reduceOnly=True
            )
        except Exception as e:
            log("Error placing trailing stop:", e)

    # Notificación
    msg = (
        f"✅ Orden {'LONG' if side=='BUY' else 'SHORT'} {symbol}\n"
        f"Qty: {qty_str}\nEntrada(MARK): {fmt_tick(entry_price, tick)}\n"
        f"SL: {sl_str}\nTP2: {tp2_str}"
    )
    if tp1_pct is not None:
        msg += f"\nTP1: {tp1_str}"
    if trailing_callback:
        msg += f"\nTrailing: {trailing_callback}%"
    tg(msg)
    log(msg)

    resp = {
        "symbol": symbol,
        "side": "LONG" if side == "BUY" else "SHORT",
        "entry_mark": float(entry_price),
        "qty": float(qty_str),
        "sl": float(sl_str),
        "tp2": float(tp2_str),
        "leverage": leverage,
        "testnet": BINANCE_TESTNET,
        "orderId": order.get("orderId")
    }
    if tp1_pct is not None:
        resp.update({"tp1": float(tp1_str), "tp1_ratio": float(tp1_ratio)})
    if trailing_callback:
        resp.update({"trailing_callback": float(trailing_callback)})

    return resp

# -------------------- FastAPI endpoints --------------------
# ===== Compat layer v1.5: rutas que espera el bot =====
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from fastapi import Header, HTTPException, Query

# Usa tu secreto actual
SIGNAL_SECRET = os.getenv("SIGNAL_SECRET", "oPy73_QpX!39dLz")

# Memoria simple para proposals (en producción → Postgres)
PROPOSALS: Dict[str, Dict[str, Any]] = {}

class ProposeIn(BaseModel):
    symbol: str
    side: str = Field(..., pattern="^(LONG|SHORT|BUY|SELL)$")
    risk_usdt: float
    leverage: int
    # opcional: sl_pct/tp_pct si tu /signal los usa
    sl_pct: Optional[float] = None
    tp_pct: Optional[float] = None
    secret: Optional[str] = None  # compat cuerpo

class ProposalAcceptIn(BaseModel):
    proposal_id: str
    secret: Optional[str] = None

class CloseIn(BaseModel):
    symbol: str
    secret: Optional[str] = None

def _check_secret(h_secret: Optional[str], b_secret: Optional[str]):
    # Si SIGNAL_SECRET no está configurado, se permite cualquier request
    if not SIGNAL_SECRET:
        return True
    sec = h_secret or b_secret
    if sec != SIGNAL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

@app.post("/propose")
def propose(payload: ProposeIn, x_secret: Optional[str] = Header(default=None)):
    _check_secret(x_secret, payload.secret)
    symbol = payload.symbol.replace("/", "").upper()
    side = payload.side.upper().replace("BUY", "LONG").replace("SELL", "SHORT")
    if side not in ("LONG", "SHORT"):
        raise HTTPException(400, detail="side debe ser LONG|SHORT")

    # ID simple y auditable
    ts = int(time.time() * 1000)
    pid = f"{ts}_{symbol}_{side}"

    # Guarda propuesta en memoria (en real → DB)
    PROPOSALS[pid] = {
        "id": pid,
        "status": "PENDING",
        "created_at": ts,
        "order": {
            "action": "OPEN",
            "symbol": symbol,
            "side": side,
            "risk_usdt": payload.risk_usdt,
            "leverage": payload.leverage,
            "sl_pct": payload.sl_pct,
            "tp_pct": payload.tp_pct,
        },
    }
    return {"ok": True, "proposal": PROPOSALS[pid]}

@app.post("/proposal/accept")
def proposal_accept(payload: ProposalAcceptIn, x_secret: Optional[str] = Header(default=None)):
    _check_secret(x_secret, payload.secret)
    p = PROPOSALS.get(payload.proposal_id)
    if not p:
        raise HTTPException(404, detail="proposal_id not found")
    if p["status"] != "PENDING":
        return {"ok": True, "proposal": p, "note": "already processed"}

    try:
        symbol = p["order"]["symbol"]
        side   = p["order"]["side"]
        risk   = float(p["order"]["risk_usdt"])
        lev    = int(p["order"]["leverage"])
        slp    = p["order"].get("sl_pct") or 0.004
        tpp    = p["order"].get("tp_pct") or 0.008

        if ENABLE_TRADING and client:
            # Ejecuta la orden real mediante order_router
            exec_resp = open_order({
                "symbol": symbol,
                "side": side,
                "risk_usdt": risk,
                "leverage": lev,
                "sl_pct": slp,
                "tp_pct": tpp,
            })
        else:
            exec_resp = {
                "simulated": True,
                "symbol": symbol,
                "side": side,
                "risk_usdt": risk,
                "leverage": lev,
            }

        p["status"] = "ACCEPTED"
        p["exec_response"] = exec_resp
        tg(f"✅ Propuesta aceptada: {symbol} {side} ${risk} lev{lev}")
        return {"ok": True, "proposal": p}
    except Exception as e:
        p["status"] = "ERROR"
        p["error"] = str(e)
        raise HTTPException(500, detail=f"Error al ejecutar orden: {e}")

@app.post("/proposal/reject")
def proposal_reject(payload: ProposalAcceptIn, x_secret: Optional[str] = Header(default=None)):
    _check_secret(x_secret, payload.secret)
    p = PROPOSALS.get(payload.proposal_id)
    if not p:
        raise HTTPException(404, detail="proposal_id not found")
    p["status"] = "REJECTED"
    return {"ok": True, "proposal": p}

@app.get("/status")
def status(symbol: Optional[str] = Query(default=None), x_secret: Optional[str] = Header(default=None)):
    # Permitimos status sin secreto (solo lectura) o exígelo si prefieres
    # _check_secret(x_secret, None)
    sym = (symbol or "").replace("/", "").upper()
    # Si tenés Position Manager, devolvelo; aquí devolvemos mock + últimas proposals
    last = [v for _, v in sorted(PROPOSALS.items(), key=lambda kv: kv[0], reverse=True)][:10]
    return {
        "ok": True,
        "symbol": sym or None,
        "positions": [],   # TODO: conecta con tu PositionManager si ya existe
        "recent_proposals": last,
    }

@app.post("/close")
def close_endpoint(payload: CloseIn, x_secret: Optional[str] = Header(default=None)):
    _check_secret(x_secret, payload.secret)
    symbol = payload.symbol.replace("/", "").upper()
    try:
        if ENABLE_TRADING and client:
            # Cierre real: obtenemos qty actual y cerramos reduceOnly
            result = _do_close_symbol(symbol)
        else:
            result = close_order(symbol)  # dry-run via order_router
        tg(f"🧯 Cierre enviado: {symbol}")
        return {"ok": True, "symbol": symbol, "result": result}
    except Exception as e:
        raise HTTPException(500, detail=f"close error: {e}")

@app.post("/close_pct")
def close_pct_endpoint(payload: dict = Body(...), x_secret: Optional[str] = Header(default=None)):
    """Cierra un porcentaje de la posición. JSON: {symbol, pct, secret?}"""
    b_secret = payload.get("secret")
    _check_secret(x_secret, b_secret)
    symbol = str(payload.get("symbol", "")).upper().replace("/", "")
    pct    = float(payload.get("pct", 100))
    if not symbol:
        raise HTTPException(400, detail="symbol requerido")
    if not (0 < pct <= 100):
        raise HTTPException(400, detail="pct debe estar entre 1 y 100")

    try:
        # Obtenemos posición actual para calcular qty parcial
        pos_amt = _position_amt(symbol)
        if abs(pos_amt) < 1e-12:
            return {"ok": True, "closed": False, "symbol": symbol, "reason": "no_position"}
        step = _step_size_for(symbol)
        partial_qty = _quantize(abs(pos_amt) * pct / 100, step)
        if partial_qty <= 0:
            return {"ok": True, "closed": False, "symbol": symbol, "reason": "qty_rounded_to_zero"}
        side = "SELL" if pos_amt > 0 else "BUY"
        resp = _reduce_only_market(symbol, side, partial_qty)
        tg(f"🔻 Cierre {pct:.0f}%: {symbol} qty={partial_qty}")
        return {"ok": True, "closed": True, "symbol": symbol, "pct": pct,
                "qty": partial_qty, "exchange_resp": resp}
    except Exception as e:
        raise HTTPException(500, detail=f"close_pct error: {e}")
# ===== Fin compat layer =====

@app.get("/")
def health():
    return {
        "status": "online",
        "testnet": BINANCE_TESTNET,
        "trading_enabled": ENABLE_TRADING
    }

@app.post("/webhook_test")
async def webhook_test(request: Request, x_secret: str = Header(default="")):
    """Ping seguro: valida secret y responde ok (NO opera)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    check_secret(x_secret, body)
    log("webhook_test OK ::", body)
    return {"status": "ok", "test": True, "trading_enabled": ENABLE_TRADING}

@app.get("/filters/{symbol}")
def get_filters(symbol: str):
    """Devuelve minQty/stepSize/tickSize del símbolo (útil para debug)."""
    symbol = symbol.upper()
    min_qty, step, tick = futures_filters(symbol)
    return {"symbol": symbol, "minQty": min_qty, "stepSize": step, "tickSize": tick}

@app.post("/signal")
async def signal(request: Request, x_secret: str = Header(default="")):
    """
    Señal principal:
      - Valida SECRET
      - Si ENABLE_TRADING=false: SOLO SIMULA (no opera)
      - Si ENABLE_TRADING=true: abre MARKET + SL/TP (+parcial/trailing si se envía)
    Body típico:
      symbol, side(LONG/SHORT), risk_usdt o fixed_qty, sl_pct, tp_pct, leverage,
      tp1_pct(opc), tp1_ratio(opc), trailing_callback(opc), secret(opc)
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    check_secret(x_secret, body)

    symbol    = (body.get("symbol") or "XRPUSDT").upper()
    side_in   = (body.get("side") or "LONG").upper()
    side      = "BUY" if side_in == "LONG" else "SELL"
    sl_pct    = float(body.get("sl_pct") or 0.004)
    tp_pct    = float(body.get("tp_pct") or 0.008)
    leverage  = int(body.get("leverage") or 10)
    risk_usdt = body.get("risk_usdt")
    fixed_qty = body.get("fixed_qty")
    tp1_pct   = body.get("tp1_pct")
    tp1_ratio = float(body.get("tp1_ratio") or 0.5)
    trailing_callback = body.get("trailing_callback")

    if not ENABLE_TRADING:
        log("SIGNAL (SIMULADA) ::", body)
        tg(f"📶 Señal recibida (SIMULADA)\n{symbol} {side_in}\nlev {leverage}x\nsl_pct {sl_pct} | tp_pct {tp_pct}")
        return {
            "status": "ok",
            "mode": "SIMULATION_ONLY",
            "symbol": symbol,
            "side": "LONG" if side == "BUY" else "SHORT",
            "received": body,
            "note": "ENABLE_TRADING=false. Conexión verificada."
        }

    if client is None:
        raise HTTPException(status_code=500, detail="Trading habilitado pero Binance no inicializado.")

    try:
        res = place_market_with_brackets(
            symbol=symbol,
            side=side,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            risk_usdt=risk_usdt,
            fixed_qty=fixed_qty,
            leverage=leverage,
            tp1_pct=tp1_pct,
            tp1_ratio=tp1_ratio,
            trailing_callback=trailing_callback
        )
        return {"status": "ok", **res}
    except HTTPException:
        raise
    except Exception as e:
        log("Error /signal:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cancel_brackets")
async def cancel_brackets(request: Request, x_secret: str = Header(default="")):
    """Cancela TODAS las órdenes abiertas del símbolo (TP/SL)."""
    body = await request.json()
    check_secret(x_secret, body)

    symbol = (body.get("symbol") or "XRPUSDT").upper()
    if client is None:
        raise HTTPException(status_code=500, detail="Binance no inicializado.")
    try:
        open_orders = client.futures_get_open_orders(symbol=symbol)
        canceled = []
        for o in open_orders:
            try:
                client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                canceled.append(o["orderId"])
            except Exception:
                pass
        tg(f"✅ Cancelled orders for {symbol}: {canceled}")
        return {"status": "ok", "canceled": canceled, "symbol": symbol}
    except Exception as e:
        log("Error canceling orders:", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/close_position")
async def close_position(request: Request, x_secret: str = Header(default="")):
    """Cierra posición al mercado (reduceOnly) y cancela brackets remanentes."""
    body = await request.json()
    check_secret(x_secret, body)

    symbol = (body.get("symbol") or "XRPUSDT").upper()
    if client is None:
        raise HTTPException(status_code=500, detail="Binance no inicializado.")
    try:
        pos = [p for p in client.futures_position_information(symbol=symbol) if float(p["positionAmt"]) != 0]
        if not pos:
            return {"status": "no-position", "symbol": symbol}
        amt = float(pos[0]["positionAmt"])
        side = "SELL" if amt > 0 else "BUY"
        qty = abs(amt)

        client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=qty, reduceOnly=True)
        # cancelar órdenes remanentes
        for o in client.futures_get_open_orders(symbol=symbol):
            try:
                client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
            except Exception:
                pass
        tg(f"✅ Closed position {symbol} qty={qty}")
        return {"status": "ok", "closed": qty, "symbol": symbol}
    except Exception as e:
        log("Error closing position:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- Startup --------------------
@app.on_event("startup")
def startup_event():
    log("Startup | Testnet:", BINANCE_TESTNET, "| Trading enabled:", ENABLE_TRADING)
    if ENABLE_TRADING and client:
        try:
            client.futures_ping()
            log("Binance futures ping OK")
        except Exception as e:
            log("Binance ping failed:", e)
    if TG_TOKEN and TG_CHAT:
        log("Telegram ON")
    else:
        log("Telegram OFF (sin TG_TOKEN/TG_CHAT)")

# --- WATCHLIST endpoints (activar/desactivar pares para el monitor) ---
from fastapi import Body
import os, json, time

WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", "watchlist.json")
SIGNAL_SECRET  = os.getenv("SIGNAL_SECRET", "")

def _wl_load():
    if not os.path.exists(WATCHLIST_FILE): return {}
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _wl_save(d: dict):
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

@app.get("/arms")
def list_arms():
    return {"watchlist": _wl_load()}

@app.post("/arm")
def arm_pair(payload: dict = Body(...)):
    """
    JSON esperado (ejemplo mínimo):
    {
      "secret":"...", "symbol":"XRPUSDT", "active":true,
      "intervals":["1m","5m","15m","1h"],
      "risk_usdt":10, "leverage":10, "sl_pct":0.004, "tp_pct":0.008,
      "open_score_threshold":5.0,
      "confirm_min_abs_delta":1000,
      "confirm_min_imbalance":0.15,
      "trail_atr_mult":1.2,
      "exit_flip_score":2.0,
      "cooldown_seconds":20,
      "bias":"any"  // "any"|"long_only"|"short_only"
    }
    """
    if payload.get("secret") != SIGNAL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    sym = str(payload.get("symbol","")).upper().replace("/","").replace("-","")
    if not sym: 
        raise HTTPException(status_code=400, detail="symbol requerido")

    wl = _wl_load()
    cfg = wl.get(sym, {})
    # merge con defaults sencillos
    cfg.update({
        "active": bool(payload.get("active", True)),
        "intervals": payload.get("intervals", ["1m","5m","15m","1h"]),
        "risk_usdt": float(payload.get("risk_usdt", 10.0)),
        "leverage": int(payload.get("leverage", 10)),
        "sl_pct": float(payload.get("sl_pct", 0.004)),
        "tp_pct": float(payload.get("tp_pct", 0.008)),
        "open_score_threshold": float(payload.get("open_score_threshold", 5.0)),
        "confirm_min_abs_delta": float(payload.get("confirm_min_abs_delta", 1000.0)),
        "confirm_min_imbalance": float(payload.get("confirm_min_imbalance", 0.15)),
        "trail_atr_mult": float(payload.get("trail_atr_mult", 1.2)),
        "exit_flip_score": float(payload.get("exit_flip_score", 2.0)),
        "cooldown_seconds": int(payload.get("cooldown_seconds", 20)),
        "bias": (payload.get("bias","any") or "any").lower()
    })
    wl[sym] = cfg
    _wl_save(wl)
    return {"status":"ok","symbol":sym,"config":cfg}

@app.post("/disarm")
def disarm_pair(payload: dict = Body(...)):
    if payload.get("secret") != SIGNAL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    sym = str(payload.get("symbol","")).upper().replace("/","").replace("-","")
    if not sym:
        raise HTTPException(status_code=400, detail="symbol requerido")
    wl = _wl_load()
    if sym in wl:
        wl[sym]["active"] = False
        _wl_save(wl)
        return {"status":"ok","symbol":sym,"active":False}
    return {"status":"ok","symbol":sym,"active":False,"note":"no estaba en watchlist"}

# ================== CLOSE ENDPOINTS (USDT-M Futures) ==================
# Cierra la posición abierta de un símbolo con reduceOnly MARKET.
# Soporta modo one-way (sin positionSide). Si usas hedge mode, añade positionSide según tu lado.

import os, time, hmac, hashlib, json, math
from typing import Any, Dict, Optional
import requests
from fastapi import HTTPException, Body

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_BASE_URL   = os.getenv("BINANCE_BASE_URL", "https://testnet.binancefuture.com").rstrip("/")
RECV_WINDOW        = int(os.getenv("RECV_WINDOW", "5000"))

def _now_ms() -> int:
    return int(time.time() * 1000)

def _sign(qs: str) -> str:
    return hmac.new(BINANCE_API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()

def _headers() -> Dict[str, str]:
    if not BINANCE_API_KEY:
        raise HTTPException(status_code=500, detail="Falta BINANCE_API_KEY en .env")
    return {"X-MBX-APIKEY": BINANCE_API_KEY}

def _signed_get(path: str, params: Dict[str, Any]) -> Any:
    params = dict(params or {})
    params["timestamp"] = _now_ms()
    params["recvWindow"] = RECV_WINDOW
    qs = "&".join([f"{k}={params[k]}" for k in sorted(params)])
    sig = _sign(qs)
    url = f"{BINANCE_BASE_URL}{path}?{qs}&signature={sig}"
    r = requests.get(url, headers=_headers(), timeout=10)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

def _signed_post(path: str, params: Dict[str, Any]) -> Any:
    params = dict(params or {})
    params["timestamp"] = _now_ms()
    params["recvWindow"] = RECV_WINDOW
    qs = "&".join([f"{k}={params[k]}" for k in sorted(params)])
    sig = _sign(qs)
    url = f"{BINANCE_BASE_URL}{path}?{qs}&signature={sig}"
    r = requests.post(url, headers=_headers(), timeout=10)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

def _exchange_info_symbol(symbol: str) -> Dict[str, Any]:
    url = f"{BINANCE_BASE_URL}/fapi/v1/exchangeInfo?symbol={symbol}"
    r = requests.get(url, headers=_headers(), timeout=10)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    data = r.json()
    if "symbols" not in data or not data["symbols"]:
        raise HTTPException(status_code=400, detail=f"exchangeInfo vacío para {symbol}")
    return data["symbols"][0]

def _step_size_for(symbol: str) -> float:
    info = _exchange_info_symbol(symbol)
    for f in info.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            return float(f.get("stepSize"))
    # fallback prudente
    return 0.001

def _quantize(qty: float, step: float) -> float:
    # ajusta qty al múltiplo válido de stepSize
    if step <= 0: 
        return qty
    precision = int(round(-math.log10(step)))
    q = math.floor(qty / step) * step
    return float(f"{q:.{precision}f}")

def _position_risk(symbol: str) -> Dict[str, Any]:
    # fapi/v2/positionRisk o v1. v2 retorna mismos campos relevantes.
    data = _signed_get("/fapi/v2/positionRisk", {"symbol": symbol})
    if isinstance(data, list) and data:
        # en one-way suele ser un único ítem
        for it in data:
            if it.get("symbol") == symbol:
                return it
        return data[0]
    if isinstance(data, dict):
        return data
    return {}

def _position_amt(symbol: str) -> float:
    pr = _position_risk(symbol)
    amt = pr.get("positionAmt")
    try:
        return float(amt)
    except:
        return 0.0

def _reduce_only_market(symbol: str, side: str, qty: float) -> Any:
    """
    side: BUY para cerrar short, SELL para cerrar long
    qty: cantidad positiva ya cuantizada
    """
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true",   # muy importante para no abrir en contra
        "newOrderRespType": "RESULT",
    }
    return _signed_post("/fapi/v1/order", params)

# /close (duplicate route removed — implementación consolidada arriba)
# La función real de cierre con posición viva queda disponible internamente:
def _do_close_symbol(symbol: str) -> dict:
    """Cierra la posición abierta del símbolo usando la API de Binance directamente."""
    pos_amt = _position_amt(symbol)
    if abs(pos_amt) < 1e-12:
        return {"status":"ok","closed":False,"symbol":symbol,"reason":"no_position"}
    step = _step_size_for(symbol)
    qty  = _quantize(abs(pos_amt), step)
    if qty <= 0:
        return {"status":"ok","closed":False,"symbol":symbol,"reason":"qty_rounded_to_zero"}
    side = "SELL" if pos_amt > 0 else "BUY"
    resp = _reduce_only_market(symbol, side, qty)
    return {"status":"ok","closed":True,"symbol":symbol,"side_sent":side,"qty":qty,"exchange_resp":resp}


@app.post("/close_all")
def close_all_positions(payload: dict = Body(...)):
    """
    Intenta cerrar todas las posiciones con tamaño != 0 (one-way).
    JSON: { "secret":"..." }
    """
    server_secret = os.getenv("SIGNAL_SECRET", "")
    if server_secret and payload.get("secret") != server_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Trae todas las posiciones de la cuenta
    data = _signed_get("/fapi/v2/positionRisk", {})
    if not isinstance(data, list):
        raise HTTPException(status_code=500, detail="Respuesta inesperada de positionRisk")

    results = []
    for it in data:
        try:
            symbol = it.get("symbol")
            amt = float(it.get("positionAmt", "0"))
            if abs(amt) < 1e-12:
                continue
            step = _step_size_for(symbol)
            qty  = _quantize(abs(amt), step)
            if qty <= 0:
                results.append({"symbol":symbol,"skipped":"qty_rounded_to_zero"})
                continue
            side = "SELL" if amt > 0 else "BUY"
            resp = _reduce_only_market(symbol, side, qty)
            results.append({"symbol":symbol,"closed":True,"side_sent":side,"qty":qty,"resp":resp})
        except Exception as e:
            results.append({"symbol":it.get("symbol"),"error":str(e)})
    return {"status":"ok","results":results}
# ================== /CLOSE ENDPOINTS ==================
