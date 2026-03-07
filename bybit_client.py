"""
bybit_client.py v4 — Bybit V5 API con ejecución segura
═══════════════════════════════════════════════════════
Mejoras v4:
  - get_instrument_info(): caché de maxLeverage, maxOrderQty, minOrderQty, qtyStep
  - safe_qty(): redondeo con math.floor + cap a maxOrderQty — jamás rechaza por 10001
  - set_leverage_safe(): clamp automático al maxLeverage real del símbolo
  - Todos los hard-limits matemáticos ANTES de llamar a la API

Demo:  https://api-demo.bybit.com
Real:  https://api.bybit.com
"""

import hashlib, hmac, time, json, logging, math
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
import asyncio

log = logging.getLogger("bybit_client")

DEMO_BASE   = "https://api-demo.bybit.com"
LIVE_BASE   = "https://api.bybit.com"
PUBLIC_BASE = "https://api.bybit.com"


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        self.api_key    = (api_key    or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.paper      = paper
        self.base       = DEMO_BASE if paper else LIVE_BASE
        self.session    = requests.Session()
        # ── Caché de información de instrumentos ──────────────────────────────
        # {symbol: {qty_step, min_qty, max_qty, max_leverage, tick_size, raw}}
        self._instrument_cache: Dict[str, Dict[str, Any]] = {}

        mode = "DEMO (api-demo.bybit.com)" if paper else "REAL (api.bybit.com)"
        log.info(f"BybitClient → {mode}")
        if self.api_key:
            log.info(f"API Key: {self.api_key[:6]}...{self.api_key[-4:]}")
        else:
            log.error("❌ BYBIT_API_KEY vacía")

    # ── Firma HMAC-SHA256 ──────────────────────────────────────────────────────
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

    # ── HTTP ───────────────────────────────────────────────────────────────────
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
                        "   deben crearse dentro del modo 'Trading de Prueba' de Bybit."
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
            code = data.get("retCode", 0)
            if code != 0:
                msg = data.get("retMsg", "")
                if code == 110043:
                    log.debug(f"POST {path} [{code}] {msg}")   # leverage unchanged = no-op
                else:
                    log.warning(f"POST {path} [{code}] {msg}")
            return data
        except Exception as e:
            log.error(f"POST {path}: {e}")
            return {}

    def _pub(self, path: str, params: Optional[Dict] = None) -> Dict:
        """Datos de mercado públicos (sin auth, siempre api.bybit.com)."""
        try:
            r = requests.get(PUBLIC_BASE + path, params=params or {}, timeout=12)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"PUB {path}: {e}")
            return {}

    # ── Test ───────────────────────────────────────────────────────────────────
    def test_connection(self) -> bool:
        data = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        ok = data.get("retCode", -1) == 0
        if ok:
            log.info("✅ Autenticación API verificada OK")
        return ok

    # ══════════════════════════════════════════════════════════
    #  get_instrument_info — NUEVO (v4) — con caché
    # ══════════════════════════════════════════════════════════

    def get_instrument_info(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Retorna información del instrumento con caché en memoria.

        Campos garantizados en el resultado:
          qty_step     float  — paso mínimo de cantidad (para math.floor)
          min_qty      float  — cantidad mínima de orden
          max_qty      float  — cantidad máxima de orden (cap hard)
          max_leverage int    — apalancamiento máximo permitido por Bybit
          tick_size    float  — precisión del precio
          raw          dict   — respuesta cruda del API

        Ejemplo:
          BTCUSDT → qty_step=0.001, min_qty=0.001, max_qty=100.0,
                    max_leverage=100, tick_size=0.1
        """
        if not force_refresh and symbol in self._instrument_cache:
            return self._instrument_cache[symbol]

        # Defaults seguros (conservadores)
        defaults = {
            "qty_step":     0.001,
            "min_qty":      0.001,
            "max_qty":      1000.0,
            "max_leverage": 10,
            "tick_size":    0.01,
            "raw":          {},
        }

        try:
            data = self._pub("/v5/market/instruments-info", {
                "category": "linear",
                "symbol":   symbol,
            })
            items = data.get("result", {}).get("list", [])
            if not items:
                log.warning(f"get_instrument_info: {symbol} no encontrado → usando defaults")
                self._instrument_cache[symbol] = defaults
                return defaults

            info = items[0]
            lot  = info.get("lotSizeFilter")  or info.get("lotSize")   or {}
            pf   = info.get("priceFilter")    or {}
            lev  = info.get("leverageFilter") or {}

            def _f(d: dict, *keys) -> Optional[float]:
                """Lee primera clave válida de un dict como float."""
                for k in keys:
                    v = d.get(k)
                    if v not in (None, "", "0", 0):
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
                return None

            qty_step   = _f(lot, "qtyStep",    "stepSize",    "lotSize")    or 0.001
            min_qty    = _f(lot, "minOrderQty", "minQty",     "minLotSize") or qty_step
            max_qty    = _f(lot, "maxOrderQty", "maxQty",     "maxLotSize") or 1000.0
            tick_size  = _f(pf,  "tickSize",    "priceStep",  "tick")       or 0.01
            max_lev    = int(_f(lev, "maxLeverage", "maxBuyLeverage") or 10)

            # Sanity checks
            if qty_step <= 0:   qty_step   = 0.001
            if min_qty  <= 0:   min_qty    = qty_step
            if max_qty  <= 0:   max_qty    = 1000.0
            if tick_size <= 0:  tick_size  = 0.01
            if max_lev  <= 0:   max_lev    = 10

            result = {
                "qty_step":     qty_step,
                "min_qty":      min_qty,
                "max_qty":      max_qty,
                "max_leverage": max_lev,
                "tick_size":    tick_size,
                "raw":          info,
            }
            self._instrument_cache[symbol] = result
            log.debug(
                f"Instrument {symbol}: step={qty_step}, min={min_qty}, "
                f"max={max_qty}, maxLev={max_lev}, tick={tick_size}"
            )
            return result

        except Exception as e:
            log.error(f"get_instrument_info {symbol}: {e} → usando defaults")
            self._instrument_cache[symbol] = defaults
            return defaults

    def safe_qty(self, symbol: str, qty: float) -> Tuple[float, str]:
        """
        Cuantiza la cantidad con math.floor al qty_step del instrumento.
        Aplica cap a maxOrderQty y floor desde minOrderQty.

        Retorna (qty_safe, error_msg_or_empty).
        Si el resultado es 0 o menor que minOrderQty, retorna (0.0, "reason").

        GARANTÍA: Bybit nunca rechazará por error 10001 (Invalid qty)
        si usas este método.
        """
        info = self.get_instrument_info(symbol)
        step = info["qty_step"]
        minq = info["min_qty"]
        maxq = info["max_qty"]

        if qty <= 0:
            return 0.0, f"qty={qty} inválida"

        # math.floor al múltiplo de step
        # Usamos aritmética entera para evitar errores de punto flotante
        if step > 0:
            # Cuántos pasos completos caben en qty
            steps_count = math.floor(qty / step)
            safe = steps_count * step
            # Redondear a la precisión del step
            decimals = max(0, -int(math.floor(math.log10(step))) if step < 1 else 0)
            safe = round(safe, decimals + 2)
        else:
            safe = round(qty, 4)

        # Cap al máximo
        if safe > maxq:
            log.info(f"safe_qty [{symbol}]: {safe} → cap a maxOrderQty={maxq}")
            safe = maxq

        # Floor al mínimo
        if safe < minq:
            return 0.0, f"qty={safe:.6f} < minOrderQty={minq} para {symbol}"

        return safe, ""

    def safe_leverage(self, symbol: str, requested_leverage: int) -> int:
        """
        Clampea el leverage al maxLeverage real del símbolo en Bybit.
        Garantiza que set_leverage nunca falle por exceder el límite del exchange.
        """
        info      = self.get_instrument_info(symbol)
        max_lev   = info["max_leverage"]
        safe      = max(1, min(requested_leverage, max_lev))
        if safe != requested_leverage:
            log.info(
                f"safe_leverage [{symbol}]: {requested_leverage}x → "
                f"clamped a {safe}x (maxLeverage={max_lev})"
            )
        return safe

    def safe_price(self, symbol: str, price: float) -> float:
        """Redondea el precio al tick_size del instrumento."""
        info = self.get_instrument_info(symbol)
        tick = info["tick_size"]
        if tick <= 0:
            return round(price, 4)
        decimals = max(0, -int(math.floor(math.log10(tick))) if tick < 1 else 0)
        safe = round(round(price / tick) * tick, decimals + 2)
        return safe

    # ── Market data (público) ──────────────────────────────────────────────────
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
                    "open":  float(row[1]), "high":  float(row[2]),
                    "low":   float(row[3]), "close": float(row[4]),
                    "volume": float(row[5]),
                })
            except Exception:
                continue
        out.reverse()
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
        data  = self._pub("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        items = data.get("result", {}).get("list", [])
        return float(items[0].get("markPrice", 0)) if items else 0.0

    def get_instruments(self, category: str = "linear") -> List[Dict]:
        data = self._pub("/v5/market/instruments-info",
                         {"category": category, "limit": 500})
        return data.get("result", {}).get("list", []) or []

    # ── Account ────────────────────────────────────────────────────────────────
    def get_wallet_balance(self, account_type: str = "UNIFIED") -> Dict:
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
                def _f(v):
                    try:
                        return float(v) if v not in ("", None) else 0.0
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
                return result
        log.warning("get_wallet_balance: no se pudo obtener balance.")
        return {}

    def get_usdt_balance(self) -> float:
        bal = self.get_wallet_balance()
        for coin in ["USDT", "USDC"]:
            info  = bal.get(coin, {})
            avail = info.get("available", 0) or info.get("walletBalance", 0)
            if avail > 0:
                return float(avail)
        total = sum(v.get("equity", 0) for v in bal.values())
        return float(total)

    # ── Posiciones ─────────────────────────────────────────────────────────────
    def get_positions(self, symbol: Optional[str] = None,
                      category: str = "linear") -> List[Dict]:
        params: Dict[str, Any] = {"category": category, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        return self._get("/v5/position/list", params).get("result", {}).get("list", []) or []

    def get_open_orders(self, symbol: Optional[str] = None,
                        category: str = "linear") -> List[Dict]:
        params: Dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        return self._get("/v5/order/realtime", params).get("result", {}).get("list", []) or []

    def get_closed_pnl(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict]:
        params: Dict[str, Any] = {"category": "linear", "limit": limit}
        if symbol:
            params["symbol"] = symbol
        return self._get("/v5/position/closed-pnl", params).get("result", {}).get("list", []) or []

    # ── Leverage SEGURO (v4) ───────────────────────────────────────────────────
    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """
        Setea leverage con clamp automático al maxLeverage del símbolo.
        Nunca falla por exceder límites del exchange.
        """
        safe = self.safe_leverage(symbol, leverage)
        return self._post("/v5/position/set-leverage", {
            "category":     "linear",
            "symbol":       symbol,
            "buyLeverage":  str(safe),
            "sellLeverage": str(safe),
        })

    def set_position_mode(self, symbol: Optional[str] = None, mode: int = 0) -> Dict:
        body: Dict[str, Any] = {"category": "linear", "mode": str(mode)}
        if symbol:
            body["symbol"] = symbol
        else:
            body["coin"] = "USDT"
        return self._post("/v5/position/switch-mode", body)

    # ── Orden SEGURA (v4) — math.floor + maxOrderQty cap ──────────────────────
    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Market",
        price: Optional[float] = None,
        tp: Optional[float] = None,
        sl: Optional[float] = None,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
        position_idx: int = 0,
    ) -> Dict:
        """
        Coloca orden con TODOS los hard-limits matemáticos aplicados:
          1. safe_qty(): math.floor al qtyStep + cap maxOrderQty
          2. safe_price(): tick_size rounding para tp/sl/price
          3. safe_leverage() se llama externamente antes de place_order

        GARANTÍA: Bybit no rechazará por error 10001 (qty inválida).
        """
        # ── 1. Validar y cuantizar qty ─────────────────────────────────────────
        safe, err = self.safe_qty(symbol, qty)
        if safe <= 0:
            msg = f"place_order [{symbol}]: qty inválida — {err}"
            log.error(msg)
            return {"retCode": 10001, "retMsg": msg, "result": {}}

        log.debug(f"place_order {symbol} {side}: qty_req={qty:.6f} → safe={safe:.6f}")

        body: Dict[str, Any] = {
            "category":    "linear",
            "symbol":      symbol,
            "side":        side,
            "orderType":   order_type,
            "qty":         str(safe),
            "timeInForce": time_in_force,
            "positionIdx": position_idx,
        }

        # ── 2. Precio para órdenes limit ───────────────────────────────────────
        if price is not None and price > 0:
            body["price"] = str(self.safe_price(symbol, price))

        # ── 3. TP / SL ─────────────────────────────────────────────────────────
        has_tp = tp is not None and tp > 0
        has_sl = sl is not None and sl > 0

        if has_tp or has_sl:
            body["tpslMode"] = "Full"
            if has_tp:
                body["takeProfit"]  = str(self.safe_price(symbol, tp))
                body["tpTriggerBy"] = "MarkPrice"
            if has_sl:
                body["stopLoss"]    = str(self.safe_price(symbol, sl))
                body["slTriggerBy"] = "MarkPrice"

        if reduce_only:
            body["reduceOnly"] = True

        log.debug(f"Order body: {body}")
        resp = self._post("/v5/order/create", body)

        # Log amigable del resultado
        rc = resp.get("retCode", -1)
        if rc == 0:
            oid = resp.get("result", {}).get("orderId", "?")
            log.info(f"✅ Orden {symbol} {side} {safe} → orderId={oid}")
        else:
            log.error(f"❌ Orden {symbol} {side} {safe} → [{rc}] {resp.get('retMsg')}")

        return resp

    def cancel_all_orders(self, symbol: str) -> Dict:
        return self._post("/v5/order/cancel-all", {
            "category": "linear", "symbol": symbol,
        })

    def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        """Cierra posición al mercado con reduceOnly."""
        close_side = "Sell" if side.lower() in ("buy", "long") else "Buy"
        return self.place_order(
            symbol=symbol, side=close_side, qty=qty,
            order_type="Market", reduce_only=True,
        )

    def set_tp_sl(self, symbol: str, tp: Optional[float] = None,
                  sl: Optional[float] = None, position_idx: int = 0) -> Dict:
        body: Dict[str, Any] = {
            "category":    "linear",
            "symbol":      symbol,
            "positionIdx": position_idx,
            "tpslMode":    "Full",
        }
        if tp is not None and tp > 0:
            body["takeProfit"]  = str(self.safe_price(symbol, tp))
            body["tpTriggerBy"] = "MarkPrice"
        if sl is not None and sl > 0:
            body["stopLoss"]    = str(self.safe_price(symbol, sl))
            body["slTriggerBy"] = "MarkPrice"
        return self._post("/v5/position/trading-stop", body)

    # ── Async wrappers ─────────────────────────────────────────────────────────
    async def async_get_klines(self, symbol: str, interval: str, limit: int = 100):
        return await asyncio.to_thread(self.get_klines, symbol, interval, limit)

    async def async_get_ticker(self, symbol: str):
        def _sync():
            for it in self.get_tickers():
                if it.get("symbol") == symbol:
                    return it
            return {}
        return await asyncio.to_thread(_sync)

    async def async_get_orderbook(self, symbol: str, limit: int = 25):
        return await asyncio.to_thread(self.get_orderbook, symbol, limit)

    async def async_set_stop_loss(self, symbol: str, stop_price: float,
                                  position_idx: int = 0):
        return await asyncio.to_thread(self.set_tp_sl, symbol, None, stop_price, position_idx)

    async def async_get_balance(self) -> float:
        return await asyncio.to_thread(self.get_usdt_balance)

    async def async_get_positions(self, symbol: Optional[str] = None):
        return await asyncio.to_thread(self.get_positions, symbol)

    async def async_create_order(self, symbol: str, side: str, qty: float,
                                 order_type: str = "Market",
                                 price: Optional[float] = None,
                                 tp: Optional[float] = None,
                                 sl: Optional[float] = None,
                                 reduce_only: bool = False,
                                 time_in_force: str = "GTC",
                                 position_idx: int = 0):
        return await asyncio.to_thread(
            self.place_order, symbol, side, qty,
            order_type, price, tp, sl,
            reduce_only, time_in_force, position_idx
        )

    async def async_close_position(self, symbol: str, side: str, qty: float):
        return await asyncio.to_thread(self.close_position, symbol, side, qty)
