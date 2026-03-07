"""
news_engine.py v2 — Motor de Noticias + Calendario de Eventos Macro
═════════════════════════════════════════════════════════════════════
Nuevo en v2:
  - MacroCalendar: calendario de eventos HIGH_IMPACT (FOMC, CPI, NFP, ...)
  - is_news_freeze_active(): True si ±30 min alrededor de un evento macro
  - Notificación automática al activar/levantar una ventana de freeze
  - RC codes en todos los logs y mensajes de Telegram
  - macro_events.json: archivo externo para actualizar fechas sin tocar código
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

import requests

from reason_codes import RC, NEWS_FREEZE_PRE_MIN, NEWS_FREEZE_POST_MIN

log = logging.getLogger("news_engine")

_FREEZE_PRE_SEC  = NEWS_FREEZE_PRE_MIN  * 60   # 1800 s
_FREEZE_POST_SEC = NEWS_FREEZE_POST_MIN * 60   # 1800 s

MACRO_EVENTS_FILE = "macro_events.json"

# ══════════════════════════════════════════════════════════
#  FUENTES RSS  (sin cambios respecto a v1)
# ══════════════════════════════════════════════════════════

RSS_FEEDS = [
    {"name":"CryptoPanic",  "url":"https://cryptopanic.com/news/rss/",              "weight":1.2,"category":"crypto"},
    {"name":"CoinTelegraph","url":"https://cointelegraph.com/rss",                   "weight":1.0,"category":"crypto"},
    {"name":"Decrypt",      "url":"https://decrypt.co/feed",                        "weight":0.9,"category":"crypto"},
    {"name":"The Block",    "url":"https://www.theblock.co/rss.xml",                "weight":1.1,"category":"crypto"},
    {"name":"Bitcoin Mag",  "url":"https://bitcoinmagazine.com/feed",               "weight":0.8,"category":"btc"},
    {"name":"CoinDesk",     "url":"https://www.coindesk.com/arc/outboundfeeds/rss/","weight":1.0,"category":"crypto"},
    {"name":"Investing",    "url":"https://www.investing.com/rss/news_301.rss",     "weight":0.9,"category":"macro"},
]

# ══════════════════════════════════════════════════════════
#  PALABRAS CLAVE  (sin cambios respecto a v1)
# ══════════════════════════════════════════════════════════

BULLISH_KEYWORDS: Dict[str, float] = {
    "etf approved":0.9,"etf approval":0.9,"spot etf":0.8,
    "institutional":0.5,"adoption":0.6,"partnership":0.4,
    "listing":0.5,"listed on":0.5,"breakout":0.6,
    "all-time high":0.8,"ath":0.7,"rally":0.6,"surge":0.5,
    "bullish":0.5,"buy":0.3,"accumulation":0.5,"upgrade":0.4,
    "launch":0.4,"mainnet":0.5,"halving":0.7,
    "bitcoin reserve":0.8,"strategic reserve":0.8,"legal tender":0.7,
    "rate cut":0.6,"pivot":0.5,"fed pause":0.5,
    "inflation easing":0.4,"gdp growth":0.3,
    "moon":0.3,"recovery":0.4,"rebounding":0.4,"outperform":0.4,
    "regulation clarity":0.6,"framework approved":0.6,
    "crypto friendly":0.5,"pro-crypto":0.6,
}

BEARISH_KEYWORDS: Dict[str, float] = {
    "ban":0.8,"banned":0.8,"sec charges":0.9,"sec sues":0.9,
    "lawsuit":0.6,"illegal":0.7,"seized":0.7,"arrest":0.6,
    "crackdown":0.7,"shutdown":0.7,"sanction":0.7,
    "hack":0.9,"hacked":0.9,"exploit":0.8,"stolen":0.8,
    "rug pull":1.0,"exit scam":1.0,"breach":0.7,
    "crash":0.8,"dump":0.6,"sell-off":0.7,"selloff":0.7,
    "collapse":0.8,"plunge":0.7,"bearish":0.5,"decline":0.3,
    "bankruptcy":0.9,"insolvent":0.9,"halted":0.8,
    "exchange down":0.9,"withdrawals suspended":0.9,
    "bank run":0.9,"insolvency":0.9,
    "rate hike":0.5,"inflation spike":0.5,"recession":0.5,
    "war":0.6,"conflict":0.4,"crisis":0.5,"emergency":0.5,
    "fud":0.4,"panic":0.6,"fear":0.4,
}

AMPLIFIERS: Dict[str, float] = {
    "massive":1.5,"major":1.3,"huge":1.4,"critical":1.5,
    "urgent":1.4,"breaking":1.6,"confirmed":1.4,"official":1.3,
    "emergency":1.5,"unprecedented":1.4,"billions":1.3,
    "largest":1.3,"biggest":1.3,"historic":1.2,
}

SYMBOL_KEYWORDS: Dict[str, List[str]] = {
    "BTCUSDT":  ["bitcoin","btc","satoshi","crypto","cryptocurrency"],
    "ETHUSDT":  ["ethereum","eth","vitalik","ether","erc-20","defi"],
    "SOLUSDT":  ["solana","sol"],
    "BNBUSDT":  ["binance","bnb","bsc"],
    "XRPUSDT":  ["ripple","xrp"],
    "ADAUSDT":  ["cardano","ada"],
    "DOGEUSDT": ["dogecoin","doge"],
    "AVAXUSDT": ["avalanche","avax"],
    "DOTUSDT":  ["polkadot","dot"],
    "LINKUSDT": ["chainlink","link"],
    "LTCUSDT":  ["litecoin","ltc"],
    "MATICUSDT":["polygon","matic"],
    "ATOMUSDT": ["cosmos","atom"],
    "UNIUSDT":  ["uniswap","uni"],
    "AAVEUSDT": ["aave"],
}

GLOBAL_CRYPTO_TERMS = [
    "crypto","cryptocurrency","digital asset","blockchain",
    "defi","web3","bitcoin","btc","altcoin",
    "fed","federal reserve","interest rate","inflation",
    "sec","cftc","regulation","cbdc",
]

CRITICAL_THRESHOLD = 0.65


# ══════════════════════════════════════════════════════════
#  ANÁLISIS DE TEXTO
# ══════════════════════════════════════════════════════════

def analyze_text(text: str) -> Tuple[float, str, List[str]]:
    t = text.lower()
    amp = 1.0
    for a, m in AMPLIFIERS.items():
        if a in t:
            amp = max(amp, m)
    bull = bear = 0.0
    matched: List[str] = []
    for kw, w in BULLISH_KEYWORDS.items():
        if kw in t:
            bull += w * amp
            matched.append(f"+{kw}")
    for kw, w in BEARISH_KEYWORDS.items():
        if kw in t:
            bear += w * amp
            matched.append(f"-{kw}")
    net   = bull - bear
    score = max(-1.0, min(1.0, net / 5.0))
    if score >  0.15: direction = "BULLISH"
    elif score < -0.15: direction = "BEARISH"
    else: direction = "NEUTRAL"
    return round(score, 3), direction, matched[:8]


def is_relevant_for_symbol(text: str, symbol: str) -> bool:
    tl    = text.lower()
    terms = SYMBOL_KEYWORDS.get(symbol, []) + GLOBAL_CRYPTO_TERMS
    return any(term in tl for term in terms)


# ══════════════════════════════════════════════════════════
#  RSS PARSER
# ══════════════════════════════════════════════════════════

def fetch_rss(url: str, timeout: int = 8) -> List[Dict]:
    items: List[Dict] = []
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as r:
            content = r.read()
        root    = ET.fromstring(content)
        channel = root.find("channel") or root
        for item in channel.findall("item")[:20]:
            title   = (item.findtext("title")       or "").strip()
            desc    = (item.findtext("description") or "").strip()
            pub_date= (item.findtext("pubDate")     or "").strip()
            link    = (item.findtext("link")        or "").strip()
            desc    = re.sub(r"<[^>]+>", " ", desc)
            desc    = re.sub(r"\s+", " ", desc).strip()
            items.append({
                "title": title, "desc": desc,
                "pub_date": pub_date, "link": link,
                "full_text": f"{title} {desc}",
                "ts": int(time.time()),
            })
    except (URLError, ET.ParseError, Exception) as e:
        log.debug(f"RSS {url}: {e}")
    return items


def fetch_fear_greed() -> Dict:
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        d = r.json()["data"][0]
        v = int(d["value"])
        return {"value": v, "label": d["value_classification"],
                "score_adj": round((v - 50) / 100, 3), "ts": int(time.time())}
    except Exception as e:
        log.debug(f"Fear&Greed: {e}")
        return {"value": 50, "label": "Neutral", "score_adj": 0.0, "ts": int(time.time())}


# ══════════════════════════════════════════════════════════
#  MACRO EVENT CALENDAR
# ══════════════════════════════════════════════════════════

def _ts(dt_str: str) -> int:
    """'YYYY-MM-DD HH:MM' UTC → timestamp Unix."""
    return int(
        datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _hardcoded_events() -> List[Dict]:
    """
    Eventos macro HIGH_IMPACT hardcoded para 2026.
    Actualiza periódicamente o usa macro_events.json para sobreescribir.
    Cada evento: {name, ts_utc, impact, category}
    """
    return [
        # ── FED / FOMC ─────────────────────────────────────────────────────────
        {"name":"FOMC Decision",        "ts_utc":_ts("2026-03-18 18:00"),"impact":"HIGH","category":"FED"},
        {"name":"FOMC Decision",        "ts_utc":_ts("2026-05-06 18:00"),"impact":"HIGH","category":"FED"},
        {"name":"FOMC Decision",        "ts_utc":_ts("2026-06-17 18:00"),"impact":"HIGH","category":"FED"},
        {"name":"FOMC Decision",        "ts_utc":_ts("2026-07-29 18:00"),"impact":"HIGH","category":"FED"},
        {"name":"Fed Powell Speech",    "ts_utc":_ts("2026-03-07 15:00"),"impact":"HIGH","category":"FED"},
        # ── CPI / INFLACIÓN ────────────────────────────────────────────────────
        {"name":"US CPI",               "ts_utc":_ts("2026-03-11 12:30"),"impact":"HIGH","category":"INFLATION"},
        {"name":"US CPI",               "ts_utc":_ts("2026-04-10 12:30"),"impact":"HIGH","category":"INFLATION"},
        {"name":"US CPI",               "ts_utc":_ts("2026-05-13 12:30"),"impact":"HIGH","category":"INFLATION"},
        {"name":"US PPI",               "ts_utc":_ts("2026-03-12 12:30"),"impact":"MEDIUM","category":"INFLATION"},
        # ── NFP / EMPLEO ───────────────────────────────────────────────────────
        {"name":"US Non-Farm Payrolls", "ts_utc":_ts("2026-03-06 13:30"),"impact":"HIGH","category":"EMPLOYMENT"},
        {"name":"US Non-Farm Payrolls", "ts_utc":_ts("2026-04-03 12:30"),"impact":"HIGH","category":"EMPLOYMENT"},
        {"name":"US Jobless Claims",    "ts_utc":_ts("2026-03-12 12:30"),"impact":"MEDIUM","category":"EMPLOYMENT"},
        # ── PIB ────────────────────────────────────────────────────────────────
        {"name":"US GDP Q4 Final",      "ts_utc":_ts("2026-03-26 12:30"),"impact":"HIGH","category":"GDP"},
        {"name":"US GDP Q1 Advance",    "ts_utc":_ts("2026-04-29 12:30"),"impact":"HIGH","category":"GDP"},
        # ── REGULACIÓN CRIPTO ──────────────────────────────────────────────────
        {"name":"SEC Crypto Hearing",   "ts_utc":_ts("2026-03-20 14:00"),"impact":"HIGH","category":"CRYPTO_REG"},
        {"name":"US Treasury Crypto",   "ts_utc":_ts("2026-04-15 15:00"),"impact":"HIGH","category":"CRYPTO_REG"},
    ]


def _load_macro_events() -> List[Dict]:
    """
    Carga eventos desde macro_events.json si existe,
    de lo contrario usa los hardcoded.
    """
    if os.path.exists(MACRO_EVENTS_FILE):
        try:
            with open(MACRO_EVENTS_FILE) as f:
                events = json.load(f)
            log.info(f"Calendario macro: {len(events)} eventos desde {MACRO_EVENTS_FILE}")
            return events
        except Exception as e:
            log.warning(f"Error leyendo {MACRO_EVENTS_FILE}: {e} → usando hardcoded")
    return _hardcoded_events()


# ══════════════════════════════════════════════════════════
#  MACRO FREEZE DETECTOR
# ══════════════════════════════════════════════════════════

class MacroFreezeDetector:
    """
    Detecta ventanas de congelamiento alrededor de eventos HIGH_IMPACT.
    Ventana: [evento_ts - PRE_SEC, evento_ts + POST_SEC]

    is_freeze_active() retorna (bool, evento_info | None).
    Notifica a Telegram cuando la ventana se activa o se levanta.
    """

    def __init__(self, tg_notifier=None) -> None:
        self.tg      = tg_notifier
        self._events = _load_macro_events()
        self._lock   = threading.Lock()

        self._freeze_active: bool          = False
        self._active_event:  Optional[Dict] = None
        self._last_check:    float          = 0.0   # throttle

        log.info(
            f"MacroFreezeDetector: {len(self._events)} eventos | "
            f"ventana ±{NEWS_FREEZE_PRE_MIN}min"
        )

    def reload(self) -> None:
        """Recarga el calendario desde disco (sin reiniciar el bot)."""
        with self._lock:
            self._events = _load_macro_events()
        log.info(f"Calendario macro recargado: {len(self._events)} eventos")

    def is_freeze_active(self) -> Tuple[bool, Optional[Dict]]:
        """
        Retorna (True, evento) si estamos dentro de la ventana de freeze,
        (False, None) si no.

        Solo evalúa eventos impact == "HIGH".
        Throttled: no recalcula más de 1 vez cada 15 segundos.
        """
        now = time.time()
        if now - self._last_check < 15:
            return self._freeze_active, self._active_event
        self._last_check = now

        with self._lock:
            events = list(self._events)

        prev_freeze  = self._freeze_active
        prev_event   = self._active_event
        new_freeze   = False
        active_event: Optional[Dict] = None

        for evt in events:
            if evt.get("impact") != "HIGH":
                continue
            ts       = int(evt["ts_utc"])
            win_start= ts - _FREEZE_PRE_SEC
            win_end  = ts + _FREEZE_POST_SEC
            if win_start <= now <= win_end:
                new_freeze   = True
                active_event = evt
                break

        self._freeze_active = new_freeze
        self._active_event  = active_event

        # Detectar cambios de estado
        if new_freeze and not prev_freeze:
            self._on_activated(active_event, now)   # type: ignore[arg-type]
        elif not new_freeze and prev_freeze and prev_event:
            self._on_lifted(prev_event)

        return new_freeze, active_event

    def next_event(self) -> Optional[Dict]:
        now = time.time()
        future = [e for e in self._events
                  if e.get("impact") == "HIGH" and int(e["ts_utc"]) > now]
        return min(future, key=lambda e: e["ts_utc"]) if future else None

    def upcoming(self, within_hours: int = 24) -> List[Dict]:
        now    = time.time()
        cutoff = now + within_hours * 3600
        return [e for e in self._events
                if e.get("impact") == "HIGH"
                and now <= int(e["ts_utc"]) <= cutoff]

    def _on_activated(self, evt: Dict, now: float) -> None:
        ts     = int(evt["ts_utc"])
        is_pre = now < ts
        code   = RC.NEWS_FREEZE_PRE_EVENT if is_pre else RC.NEWS_FREEZE_POST_EVENT
        phase  = "PRE" if is_pre else "POST"
        mins   = abs(int(now - ts)) // 60
        log.warning(
            RC.fmt(code, event=evt["name"], phase=phase,
                   mins_from_event=mins, category=evt.get("category","?"))
        )
        if self.tg:
            self.tg.send(
                RC.tg(code,
                      evento=evt["name"],
                      categoría=evt.get("category","?"),
                      fase=phase,
                      minutos_del_evento=mins,
                      ts_evento=time.strftime("%Y-%m-%d %H:%M UTC",
                                              time.gmtime(ts)))
            )

    def _on_lifted(self, evt: Dict) -> None:
        log.info(RC.fmt(RC.NEWS_FREEZE_LIFTED, event=evt["name"]))
        if self.tg:
            self.tg.send(RC.tg(RC.NEWS_FREEZE_LIFTED, evento=evt["name"]))

    def get_status(self) -> Dict:
        freeze, evt = self.is_freeze_active()
        return {
            "freeze_active": freeze,
            "active_event":  evt,
            "next_event":    self.next_event(),
            "upcoming_6h":   self.upcoming(6),
            "total_events":  len(self._events),
        }


# ══════════════════════════════════════════════════════════
#  MOTOR PRINCIPAL
# ══════════════════════════════════════════════════════════

class NewsEngine:
    """Motor de noticias v2 con Macro Freeze integrado."""

    def __init__(self, telegram_notifier=None, scan_interval: int = 120) -> None:
        self.tg            = telegram_notifier
        self.scan_interval = scan_interval

        self._seen_urls:   set                  = set()
        self._news_cache:  List[Dict]            = []
        self._symbol_bias: Dict[str, float]      = {}
        self._global_bias: float                 = 0.0
        self._fear_greed:  Dict                  = {
            "value": 50, "label": "Neutral", "score_adj": 0.0, "ts": 0
        }
        self._lock    = threading.Lock()
        self._running = False
        self._last_scan_ts: int = 0

        # ── Macro Freeze Detector ──────────────────────────────────────────────
        self.freeze = MacroFreezeDetector(tg_notifier=telegram_notifier)

        log.info(f"NewsEngine v2 iniciado — scan cada {scan_interval}s")

    # ── Ciclo de vida ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        threading.Thread(
            target=self._loop, daemon=True, name="news-engine"
        ).start()
        log.info("NewsEngine background thread iniciado")

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        self._scan()
        while self._running:
            time.sleep(self.scan_interval)
            try:
                self._scan()
            except Exception as e:
                log.error(f"NewsEngine loop error: {e}")

    def _scan(self) -> None:
        log.info("📰 Escaneando noticias...")
        all_items: List[Dict] = []

        for feed in RSS_FEEDS:
            items = fetch_rss(feed["url"])
            for item in items:
                item["source"]        = feed["name"]
                item["feed_weight"]   = feed["weight"]
                item["feed_category"] = feed["category"]
            all_items.extend(items)
            time.sleep(0.3)

        # Fear & Greed (cada 10 min)
        if int(time.time()) - self._fear_greed.get("ts", 0) > 600:
            fg = fetch_fear_greed()
            with self._lock:
                self._fear_greed = fg
            log.info(f"Fear & Greed: {fg['value']} — {fg['label']}")

        new_items:    List[Dict]              = []
        alerts:       List[Dict]              = []
        sym_scores:   Dict[str, List[float]]  = {}
        global_scores:List[float]             = []

        for item in all_items:
            url = item.get("link") or item["title"]
            if url in self._seen_urls:
                continue
            self._seen_urls.add(url)

            text             = item["full_text"]
            score, direction, keywords = analyze_text(text)
            if direction == "NEUTRAL":
                continue

            weighted = score * item["feed_weight"]
            item["sentiment_score"] = round(score, 3)
            item["direction"]       = direction
            item["keywords"]        = keywords
            new_items.append(item)
            global_scores.append(weighted)

            for sym in list(SYMBOL_KEYWORDS.keys()) + ["BTCUSDT"]:
                if is_relevant_for_symbol(text, sym):
                    sym_scores.setdefault(sym, []).append(weighted)

            if abs(score) >= CRITICAL_THRESHOLD:
                alerts.append({"item":item,"score":score,
                                "direction":direction,"keywords":keywords})

        with self._lock:
            if global_scores:
                self._global_bias = round(
                    sum(global_scores) / len(global_scores), 3
                )
            for sym, sc in sym_scores.items():
                self._symbol_bias[sym] = round(sum(sc) / len(sc), 3)
            self._news_cache = (new_items + self._news_cache)[:50]

        self._last_scan_ts = int(time.time())

        for alert in alerts[:3]:
            self._send_alert(alert)

        # Advertir próximos eventos macro en ventana de 60 min
        for evt in self.freeze.upcoming(within_hours=1):
            mins = (int(evt["ts_utc"]) - int(time.time())) // 60
            if 0 < mins <= 60:
                log.info(
                    RC.fmt(RC.NEWS_MACRO_EVENT_UPCOMING,
                           event=evt["name"], in_min=mins,
                           category=evt.get("category","?"))
                )

        if new_items:
            log.info(
                f"📰 {len(new_items)} noticias | "
                f"bias={self._global_bias:+.2f} | alerts={len(alerts)}"
            )
            self._send_summary(new_items[:5])

    def _send_alert(self, alert: Dict) -> None:
        item = alert["item"]
        score = alert["score"]
        direction = alert["direction"]
        emoji = "🚨🟢" if direction == "BULLISH" else "🚨🔴"
        fg    = self._fear_greed
        msg   = (
            f"{emoji} <b>[{RC.NEWS_CRITICAL_ALERT}] {direction}</b>\n"
            f"Fuente: {item['source']}\n\n"
            f"<b>{item['title'][:200]}</b>\n\n"
            f"Impacto: <code>{score:+.2f}</code>\n"
            f"Keywords: {', '.join(alert['keywords'][:5])}\n"
            f"Fear & Greed: {fg['value']} — {fg['label']}"
        )
        if self.tg:
            self.tg.send(msg)
        log.warning(
            RC.fmt(RC.NEWS_CRITICAL_ALERT,
                   title=item['title'][:60], score=f"{score:+.2f}",
                   direction=direction)
        )

    def _send_summary(self, items: List[Dict]) -> None:
        if not self.tg:
            return
        fg          = self._fear_greed
        freeze_on, fevt = self.freeze.is_freeze_active()
        freeze_line = ""
        if freeze_on and fevt:
            freeze_line = f"\n❄️ <b>NEWS FREEZE ACTIVO: {fevt['name']}</b>"

        lines = [
            f"📰 <b>Resumen de noticias</b>{freeze_line}",
            f"Sentimiento global: <code>{self._global_bias:+.2f}</code>",
            f"Fear & Greed: {fg['value']} — <b>{fg['label']}</b>",
            "",
        ]
        for item in items:
            d = item.get("direction", "")
            e = "🟢" if d == "BULLISH" else "🔴"
            s = item.get("sentiment_score", 0)
            lines.append(
                f"{e} [{item['source']}] {item['title'][:120]}\n"
                f"   Impacto: <code>{s:+.2f}</code>"
            )
        self.tg.send("\n".join(lines))

    # ══════════════════════════════════════════════════════
    #  API PÚBLICA
    # ══════════════════════════════════════════════════════

    def is_news_freeze_active(self) -> Tuple[bool, Optional[Dict]]:
        """
        Retorna (True, evento_info) si hay ventana de congelamiento macro activa.
        El bot llama esto ANTES de intentar abrir cualquier posición nueva.

        Ventana: [evento.ts - 30min, evento.ts + 30min]
        Solo aplica a eventos impact == "HIGH".
        Las posiciones ABIERTAS se gestionan normalmente.
        """
        return self.freeze.is_freeze_active()

    def get_news_bias(self, symbol: str) -> Dict:
        with self._lock:
            sym_score = self._symbol_bias.get(
                symbol,
                self._symbol_bias.get("BTCUSDT", self._global_bias)
            )
            fg    = dict(self._fear_greed)
            cache = list(self._news_cache)

        cutoff = int(time.time()) - 7200
        recent_alerts = sum(
            1 for item in cache
            if abs(item.get("sentiment_score", 0)) >= CRITICAL_THRESHOLD
            and item.get("ts", 0) > cutoff
        )
        should_block  = sym_score <= -0.6 and recent_alerts >= 1
        direction     = "NEUTRAL"
        if sym_score >=  0.2: direction = "BULLISH"
        elif sym_score <= -0.2: direction = "BEARISH"

        return {
            "news_score":    sym_score,
            "direction":     direction,
            "fear_greed":    fg.get("value", 50),
            "fg_label":      fg.get("label", "Neutral"),
            "fg_adj":        fg.get("score_adj", 0.0),
            "recent_alerts": recent_alerts,
            "should_block":  should_block,
            "last_scan":     self._last_scan_ts,
        }

    def get_fear_greed(self) -> Dict:
        with self._lock:
            return dict(self._fear_greed)

    def get_recent_news(self, n: int = 10) -> List[Dict]:
        with self._lock:
            return list(self._news_cache[:n])

    def get_global_bias(self) -> float:
        with self._lock:
            return self._global_bias

    def get_status(self) -> Dict:
        with self._lock:
            fg = dict(self._fear_greed)
            nb = self._global_bias
            ns = len(self._news_cache)
        return {
            "global_bias":   nb,
            "fear_greed":    fg.get("value", 50),
            "fg_label":      fg.get("label", "Neutral"),
            "total_cached":  ns,
            "last_scan":     self._last_scan_ts,
            "symbol_biases": dict(self._symbol_bias),
            "freeze_status": self.freeze.get_status(),
        }
