"""
bybit_client.py v3 — Bybit V5 API
Demo:  https://api-demo.bybit.com
Real:  https://api.bybit.com

CÓMO OBTENER CLAVES DEMO:
  1. bybit.com → activa "Trading de Prueba" (banner naranja)
  2. Mi Perfil → Gestión de API → Crear clave API
  3. ¡DEBES ESTAR EN MODO DEMO cuando la crees!
  Esas claves funcionan contra api-demo.bybit.com
"""

import hashlib, hmac, time, json, logging, math
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
import requests
import asyncio

log = logging.getLogger("bybit_client")

DEMO_BASE   = "https://api-demo.bybit.com"
LIVE_BASE   = "https://api.bybit.com"
PUBLIC_BASE = "https://api.bybit.com"   # datos de mercado siempre reales


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        self.api_key    = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.paper      = paper
        self.base       = DEMO_BASE if paper else LIVE_BASE
        self.session    = requests.Session()
        mode = "DEMO (api-demo.bybit.com)" if paper else "REAL (api.bybit.com)"
        log.info(f"BybitClient → {mode}")
        if self.api_key:
            log.info(f"API Key: {self.api_key[:6]}...{self.api_key[-4:]}")
        else:
            log.error("❌ BYBIT_API_KEY vacía")
        # instrument cache for symbol rules (qty step, min qty, price precision)
        self._instrument_cache: Dict[str, Dict[str, Any]] = {}

    # ── Firma HMAC-SHA256 Bybit v5 ────────────────────────────────────────────
    def _sign(self, ts: str, recv: str, payload: str) -> str:
        pre = ts + self.api_key + recv + payload
        return hmac.new(
            self.api_secret.encode(), pre.encode(), hashlib.sha256
        ).hexdigest()

    def _auth_headers(self, payload: str, recv: str = "5000") -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "X-BAPI-API-KEY":     self.api_key,
            "X-BAPI-SIGN":        self._sign(ts, recv, payload),
            "X-BAPI-TIMESTAMP":   ts,
            "X-BAPI-RECV-WINDOW": recv,
            "Content-Type":       "application/json",
        }

    # ── HTTP ──────────────────────────────────────────────────────────────────
    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        params = params or {}
        qs = urlencode(sorted(params.items()))
        try:
            r = self.session.get(
                self.base + path, params=params,
                headers=self._auth_headers(qs), timeout=12
            )
            r.raise_for_status()
            data = r.json()
            code = data.get("retCode", 0)
            if code != 0:
                msg = data.get("retMsg", "")
                log.warning(f"GET {path} [{code}] {msg}")
                if code in (10003, 10004, 33004):
                    log.error(
                        "❌ API KEY INVÁLIDA — para Paper Trading las claves\n"
                        "   deben crearse DENTRO del modo 'Trading de Prueba' de Bybit."
                    )
            return data
        except Exception as e:
            log.error(f"GET {path}: {e}")
            return {}

    def _post(self, path: str, body: Dict) -> Dict:
        payload = json.dumps(body, separators=(",", ":"))
        try:
            r = self.session.post(
                self.base + path, data=payload,
                headers=self._auth_headers(payload), timeout=12
            )
            r.raise_for_status()
            data = r.json()
            if data.get("retCode", 0) != 0:
                code = data.get('retCode')
                msg = data.get('retMsg')
                # Non-critical: leverage unchanged (110043) — log at debug
                if code == 110043:
                    log.debug(f"POST {path} [{code}] {msg}")
                else:
                    log.warning(f"POST {path} [{code}] {msg}")
            return data
        except Exception as e:
            log.error(f"POST {path}: {e}")
            return {}

    def _pub(self, path: str, params: Optional[Dict] = None) -> Dict:
        """Datos de mercado públicos (sin auth, siempre api.bybit.com)"""
        try:
            r = requests.get(PUBLIC_BASE + path, params=params or {}, timeout=12)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"PUB {path}: {e}")
            return {}

    # ── Test ──────────────────────────────────────────────────────────────────
    def test_connection(self) -> bool:
        data = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        ok = data.get("retCode", -1) == 0
        if ok:
            log.info("✅ Autenticación API verificada OK")
        return ok

    # ── Market data (público) ─────────────────────────────────────────────────
    def get_klines(self, symbol: str, interval: str, limit: int = 300) -> List[Dict]:
        data = self._pub("/v5/market/kline", {
            "category": "linear", "symbol": symbol,
            "interval": interval, "limit": limit,
        })
        out = []
        for row in (data.get("result", {}).get("list", []) or []):
            try:
                out.append({
                    "open_time": int(row[0]),
                    "open":  float(row[1]), "high":   float(row[2]),
                    "low":   float(row[3]), "close":  float(row[4]),
                    "volume": float(row[5]),
                })
            except Exception:
                continue
        out.reverse()   # más antiguo primero
        return out

    def get_tickers(self, category: str = "linear") -> List[Dict]:
        data = self._pub("/v5/market/tickers", {"category": category})
        return data.get("result", {}).get("list", []) or []

    def get_orderbook(self, symbol: str, limit: int = 25) -> Dict:
        data = self._pub("/v5/market/orderbook", {
            "category": "linear", "symbol": symbol, "limit": limit,
        })
        return data.get("result", {}) or {}

    def get_mark_price(self, symbol: str) -> float:
        data = self._pub("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        items = data.get("result", {}).get("list", [])
        return float(items[0].get("markPrice", 0)) if items else 0.0

    def get_instruments(self, category: str = "linear") -> List[Dict]:
        data = self._pub("/v5/market/instruments-info",
                          {"category": category, "limit": 500})
        return data.get("result", {}).get("list", []) or []

    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        # cached lookup for instrument details
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        instruments = self.get_instruments()
        info = {}
        for it in instruments:
            if it.get('symbol') == symbol:
                info = it
                break
        # basic normalization: try to extract qty step and min qty from common paths
        qty_step = 0.0001
        min_qty = 0.0001
        price_filter = 0.0001
        if info:
            # try several possible keys
            lf = info.get('lotSizeFilter') or info.get('lotSize') or {}
            try:
                qty_step = float(lf.get('qtyStep') or lf.get('step') or qty_step)
            except Exception as e:
                log.debug(f"Error parsing qtyStep for {symbol}: {e}")
            try:
                min_qty = float(lf.get('minOrderQty') or lf.get('minQty') or min_qty)
            except Exception as e:
                log.debug(f"Error parsing minOrderQty for {symbol}: {e}")
            pf = info.get('priceScale') or info.get('priceFilter') or {}
            try:
                price_filter = float(pf.get('tickSize') or pf.get('tick') or price_filter)
            except Exception as e:
                log.debug(f"Error parsing tickSize for {symbol}: {e}")
        
        # Log what we found for debugging
        if not info:
            log.warning(f"Symbol {symbol} not found in instruments, using defaults")
        else:
            log.debug(f"Symbol {symbol}: qty_step={qty_step}, min_qty={min_qty}")
        
        out = {'raw': info, 'qty_step': qty_step, 'min_qty': min_qty, 'tick': price_filter}
        self._instrument_cache[symbol] = out
        return out

    def normalize_qty(self, symbol: str, qty: float) -> float:
        try:
            info = self.get_symbol_info(symbol)
            step = float(info.get('qty_step', 0.0001))
            minq = float(info.get('min_qty', 0.0001))
            
            # Fallback defaults for common symbols if step is too small
            # (likely means parsing failed)
            if step < 0.00001 and symbol in ['XRPUSDT', 'DOGEUSDT', 'SHIBUSDT']:
                step = 1.0 if qty > 100 else 0.1
                minq = max(1.0, minq)
            
            if step <= 0:
                return round(qty, 4)
            
            # floor to multiple of step
            multiple = int(qty / step)
            norm = multiple * step
            
            # rounding to reasonable precision
            prec = max(0, min(8, int(abs(round(-1 * (step and (math.log10(step) if step else 0)))))))
            norm = round(norm, 8)
            
            # Ensure minimum
            if norm < minq:
                log.warning(f"Normalized qty {norm} < min_qty {minq} for {symbol}, returning 0")
                return 0.0
            
            log.debug(f"Normalize {symbol}: {qty} → {norm} (step={step}, min={minq})")
            return norm
        except Exception as e:
            log.error(f"normalize_qty error for {symbol}: {e}")
            return round(qty, 4)

    # ── Account (requiere auth) ───────────────────────────────────────────────
    def get_wallet_balance(self, account_type: str = "UNIFIED") -> Dict:
        """
        Retorna dict {coin: {equity, available, unrealisedPnl, walletBalance}}
        Intenta UNIFIED primero; si falla intenta CONTRACT (futuros separado).
        """
        for acct in [account_type, "CONTRACT", "UNIFIED"]:
            data = self._get("/v5/account/wallet-balance", {"accountType": acct})
            if data.get("retCode", -1) != 0:
                continue
            lists = data.get("result", {}).get("list", []) or []
            if not lists:
                continue
            coins = lists[0].get("coin", []) or []
            result = {}
            for c in coins:
                # --- FIX: algunos campos pueden ser "" en la cuenta demo
                def _f(v):
                    try:
                        return float(v) if v != "" else 0.0
                    except (TypeError, ValueError):
                        return 0.0

                result[c.get("coin", "")] = {
                    "equity":        _f(c.get("equity")),
                    "available":     _f(c.get("availableToWithdraw"))
                                     or _f(c.get("availableToBorrow"))
                                     or _f(c.get("walletBalance")),
                    "unrealisedPnl": _f(c.get("unrealisedPnl")),
                    "walletBalance": _f(c.get("walletBalance")),
                }
            if result:
                log.debug(f"Balance leído con accountType={acct}")
                return result
        log.warning("get_wallet_balance: no se pudo obtener balance.")
        return {}

    def get_usdt_balance(self) -> float:
        """Retorna el balance disponible en USDT (busca en USDT y USDC)"""
        bal = self.get_wallet_balance()
        # Intentar USDT primero, luego USDC
        for coin in ["USDT", "USDC"]:
            info = bal.get(coin, {})
            avail = info.get("available", 0) or info.get("walletBalance", 0)
            if avail > 0:
                return float(avail)
        # Si hay algo en cualquier moneda devolver el total equity
        total = sum(v.get("equity", 0) for v in bal.values())
        return float(total)

    # ── Posiciones (FUTUROS lineales) ─────────────────────────────────────────
    def get_positions(self, symbol: Optional[str] = None, category: str = "linear") -> List[Dict]:
        """category='linear' = futuros perpetuos USDT"""
        params: Dict[str, Any] = {"category": category, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        return self._get("/v5/position/list", params).get("result", {}).get("list", []) or []

    def get_open_orders(self, symbol: Optional[str] = None, category: str = "linear") -> List[Dict]:
        params: Dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        return self._get("/v5/order/realtime", params).get("result", {}).get("list", []) or []

    def get_closed_pnl(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict]:
        params: Dict[str, Any] = {"category": "linear", "limit": limit}
        if symbol:
            params["symbol"] = symbol
        return self._get("/v5/position/closed-pnl", params).get("result", {}).get("list", []) or []

    # ── Órdenes de futuros ────────────────────────────────────────────────────
    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        return self._post("/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage": str(leverage), "sellLeverage": str(leverage),
        })

    def set_position_mode(self, symbol: Optional[str] = None, mode: int = 0) -> Dict:
        """0 = One-Way (más simple), 3 = Hedge"""
        body: Dict[str, Any] = {"category": "linear", "mode": str(mode)}
        if symbol:
            body["symbol"] = symbol
        else:
            body["coin"] = "USDT"
        return self._post("/v5/position/switch-mode", body)

    def place_order(
        self,
        symbol: str,
        side: str,            # "Buy" | "Sell"
        qty: float,
        order_type: str = "Market",
        price: Optional[float] = None,
        tp: Optional[float] = None,
        sl: Optional[float] = None,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
        position_idx: int = 0,   # 0 = one-way
    ) -> Dict:
        """
        Coloca orden en Bybit V5 con TP/SL correctamente configurado.
        FIX: Agrega tpslMode requerido por la API V5.
        """
        # Normalize qty to instrument rules to avoid "Qty invalid" errors
        try:
            norm_qty = float(self.normalize_qty(symbol, float(qty)))
        except Exception as e:
            log.error(f"Exception normalizing qty for {symbol}: {e}")
            norm_qty = float(qty)
        
        if norm_qty <= 0:
            err_msg = f"Invalid qty after normalization for {symbol}: requested={qty} normalized={norm_qty}"
            log.error(err_msg)
            return {"retCode": 10001, "retMsg": f"Qty invalid (normalized to 0): {err_msg}"}
        
        log.debug(f"place_order {symbol} {side}: qty_requested={qty} qty_normalized={norm_qty}")

        body: Dict[str, Any] = {
            "category":    "linear",
            "symbol":      symbol,
            "side":        side,
            "orderType":   order_type,
            "qty":         str(norm_qty),
            "timeInForce": time_in_force,
            "positionIdx": position_idx,
        }
        # Precio para órdenes limit
        if price is not None:
            assert price is not None
            body["price"] = str(round(float(price), 8))

        # ── FIX CRÍTICO: TP/SL con tpslMode ─────────────────────────
        # Bybit V5 requiere tpslMode cuando se usa takeProfit/stopLoss
        has_tp = tp is not None and tp > 0
        has_sl = sl is not None and sl > 0

        if has_tp or has_sl:
            body["tpslMode"] = "Full"  # ← CAMPO REQUERIDO

            if has_tp:
                assert tp is not None
                body["takeProfit"]  = str(round(float(tp), 8))
                body["tpTriggerBy"] = "MarkPrice"
                # NO incluir tpOrderType - causa el error

            if has_sl:
                assert sl is not None
                body["stopLoss"]    = str(round(float(sl), 8))
                body["slTriggerBy"] = "MarkPrice"
                # NO incluir slOrderType - causa el error

        if reduce_only:
            body["reduceOnly"] = True

        log.debug(f"Order body: {body}")
        return self._post("/v5/order/create", body)

    def cancel_all_orders(self, symbol: str) -> Dict:
        return self._post("/v5/order/cancel-all", {
            "category": "linear", "symbol": symbol,
        })

    def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        """Cierra posición al mercado con reduceOnly"""
        close_side = "Sell" if side.lower() in ("buy", "long") else "Buy"
        return self.place_order(
            symbol=symbol, side=close_side, qty=qty,
            order_type="Market", reduce_only=True,
        )

    def set_tp_sl(self, symbol: str, tp: Optional[float] = None, sl: Optional[float] = None,
                  position_idx: int = 0) -> Dict:
        """Actualiza TP/SL de una posición existente"""
        body: Dict[str, Any] = {
            "category":    "linear",
            "symbol":      symbol,
            "positionIdx": position_idx,
            "tpslMode":    "Full",  # ← REQUERIDO
        }
        if tp is not None:
            assert tp is not None
            body["takeProfit"]  = str(round(float(tp), 8))
            body["tpTriggerBy"] = "MarkPrice"
        if sl is not None:
            assert sl is not None
            body["stopLoss"]    = str(round(float(sl), 8))
            body["slTriggerBy"] = "MarkPrice"
        return self._post("/v5/position/trading-stop", body)

    # -------------------- Async wrappers --------------------
    # These provide async-friendly versions that call the existing
    # synchronous methods via asyncio.to_thread so they can be awaited
    async def async_get_klines(self, symbol: str, interval: str, limit: int = 100):
        return await asyncio.to_thread(self.get_klines, symbol, interval, limit)

    async def async_get_ticker(self, symbol: str):
        # reuse get_tickers and return first item
        def _sync():
            lst = self.get_tickers()
            for it in lst:
                if it.get('symbol') == symbol:
                    return it
            return {}
        return await asyncio.to_thread(_sync)

    async def async_get_orderbook(self, symbol: str, limit: int = 25):
        return await asyncio.to_thread(self.get_orderbook, symbol, limit)

    async def async_set_stop_loss(self, symbol: str, stop_price: float, position_idx: int = 0):
        return await asyncio.to_thread(self.set_tp_sl, symbol, None, stop_price, position_idx)

    async def async_get_balance(self) -> float:
        return await asyncio.to_thread(self.get_usdt_balance)

    async def async_get_positions(self, symbol: Optional[str] = None):
        return await asyncio.to_thread(self.get_positions, symbol)

    async def async_create_order(self, symbol: str, side: str, qty: float,
                                 order_type: str = "Market", price: Optional[float] = None,
                                 tp: Optional[float] = None, sl: Optional[float] = None,
                                 reduce_only: bool = False, time_in_force: str = "GTC",
                                 position_idx: int = 0):
        return await asyncio.to_thread(self.place_order, symbol, side, qty,
                                       order_type, price, tp, sl,
                                       reduce_only, time_in_force, position_idx)

    async def async_close_position(self, symbol: str, side: str, qty: float):
        return await asyncio.to_thread(self.close_position, symbol, side, qty)
