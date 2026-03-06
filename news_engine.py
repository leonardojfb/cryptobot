"""
news_engine.py — Motor de Noticias en Tiempo Real
══════════════════════════════════════════════════
Fuentes (todas gratuitas, sin API key):
  • CryptoPanic RSS      — noticias cripto agregadas
  • CoinTelegraph RSS    — noticias cripto editoriales
  • Bitcoin Magazine RSS — foco en BTC
  • Decrypt RSS          — noticias generales cripto
  • The Block RSS        — análisis y noticias
  • FED / FOMC / SEC     — noticias macro regulatorias
  • Fear & Greed Index   — sentimiento del mercado (alternative.me)
  • Funding rates        — Bybit (ya disponible sin auth)

Funcionamiento:
  1. Escanea RSS feeds cada 2 minutos
  2. Analiza el texto con NLP simple (palabras clave ponderadas)
  3. Asigna un "news_score" por símbolo: +1 a -1
  4. Si detecta noticia CRÍTICA (score > 0.7 o < -0.7) → notifica Telegram INMEDIATAMENTE
  5. El bot_autonomous puede consultar get_news_bias(symbol) antes de abrir trade
  6. Fear & Greed Index ajusta el umbral general de señal

Categorías de palabras clave:
  BULLISH: ETF, adoption, institutional, buy, approve, launch, partnership,
           upgrade, halving, rally, breakout, bullish, surge, moon, ATH...
  BEARISH: ban, hack, exploit, crash, lawsuit, SEC, regulation, FUD,
           sell-off, dump, bankruptcy, fear, bearish, crash, collapse...
  CRITICAL: exchange down, rug pull, hack confirmed, SEC charges, war,
            emergency, ban confirmed, massive sell-off...
"""

import re
import time
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError

import requests

log = logging.getLogger("news_engine")

# ══════════════════════════════════════════════════════════
#  FUENTES RSS
# ══════════════════════════════════════════════════════════

RSS_FEEDS = [
    {
        "name":     "CryptoPanic",
        "url":      "https://cryptopanic.com/news/rss/",
        "weight":   1.2,
        "category": "crypto",
    },
    {
        "name":     "CoinTelegraph",
        "url":      "https://cointelegraph.com/rss",
        "weight":   1.0,
        "category": "crypto",
    },
    {
        "name":     "Decrypt",
        "url":      "https://decrypt.co/feed",
        "weight":   0.9,
        "category": "crypto",
    },
    {
        "name":     "The Block",
        "url":      "https://www.theblock.co/rss.xml",
        "weight":   1.1,
        "category": "crypto",
    },
    {
        "name":     "Bitcoin Magazine",
        "url":      "https://bitcoinmagazine.com/feed",
        "weight":   0.8,
        "category": "btc",
    },
    {
        "name":     "CoinDesk",
        "url":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "weight":   1.0,
        "category": "crypto",
    },
    {
        "name":     "Investing.com Crypto",
        "url":      "https://www.investing.com/rss/news_301.rss",
        "weight":   0.9,
        "category": "macro",
    },
]

# ══════════════════════════════════════════════════════════
#  PALABRAS CLAVE PONDERADAS
# ══════════════════════════════════════════════════════════

BULLISH_KEYWORDS: Dict[str, float] = {
    # Institucional / adopción
    "etf approved": 0.9, "etf approval": 0.9, "spot etf": 0.8,
    "institutional": 0.5, "adoption": 0.6, "partnership": 0.4,
    "listing": 0.5, "listed on": 0.5,
    # Técnico / mercado
    "breakout": 0.6, "all-time high": 0.8, "ath": 0.7, "rally": 0.6,
    "surge": 0.5, "bullish": 0.5, "buy": 0.3, "accumulation": 0.5,
    "upgrade": 0.4, "launch": 0.4, "mainnet": 0.5,
    # Bitcoin específico
    "halving": 0.7, "bitcoin reserve": 0.8, "strategic reserve": 0.8,
    "legal tender": 0.7, "nation": 0.4,
    # Macro positivo
    "rate cut": 0.6, "pivot": 0.5, "fed pause": 0.5,
    "inflation easing": 0.4, "gdp growth": 0.3,
    # Sentimiento
    "moon": 0.3, "to the moon": 0.4, "green": 0.2, "pumping": 0.3,
    "recovery": 0.4, "rebounding": 0.4, "outperform": 0.4,
    # Gobierno / regulación positiva
    "regulation clarity": 0.6, "framework approved": 0.6,
    "crypto friendly": 0.5, "pro-crypto": 0.6,
}

BEARISH_KEYWORDS: Dict[str, float] = {
    # Regulatorio / legal
    "ban": 0.8, "banned": 0.8, "sec charges": 0.9, "sec sues": 0.9,
    "lawsuit": 0.6, "illegal": 0.7, "seized": 0.7, "arrest": 0.6,
    "crackdown": 0.7, "shutdown": 0.7, "sanction": 0.7,
    # Hack / seguridad
    "hack": 0.9, "hacked": 0.9, "exploit": 0.8, "stolen": 0.8,
    "rug pull": 1.0, "exit scam": 1.0, "breach": 0.7,
    "vulnerability": 0.6, "attack": 0.6,
    # Mercado / precio
    "crash": 0.8, "dump": 0.6, "dumping": 0.6, "sell-off": 0.7,
    "selloff": 0.7, "collapse": 0.8, "plunge": 0.7, "bear": 0.4,
    "bearish": 0.5, "decline": 0.3, "correction": 0.4,
    # Exchange / empresa
    "bankruptcy": 0.9, "insolvent": 0.9, "halted": 0.8,
    "exchange down": 0.9, "withdrawals suspended": 0.9,
    "bank run": 0.9, "insolvency": 0.9,
    # Macro negativo
    "rate hike": 0.5, "inflation spike": 0.5, "recession": 0.5,
    "war": 0.6, "conflict": 0.4, "crisis": 0.5, "emergency": 0.5,
    # Sentimiento
    "fud": 0.4, "panic": 0.6, "fear": 0.4, "red": 0.2,
}

# Palabras que AMPLIFICAN el impacto
AMPLIFIERS = {
    "massive": 1.5, "major": 1.3, "huge": 1.4, "critical": 1.5,
    "urgent": 1.4, "breaking": 1.6, "confirmed": 1.4, "official": 1.3,
    "emergency": 1.5, "unprecedented": 1.4, "billions": 1.3,
    "largest": 1.3, "biggest": 1.3, "historic": 1.2,
}

# Mapa símbolo → términos relacionados para filtrar noticias relevantes
SYMBOL_KEYWORDS: Dict[str, List[str]] = {
    "BTCUSDT":  ["bitcoin", "btc", "satoshi", "crypto", "cryptocurrency"],
    "ETHUSDT":  ["ethereum", "eth", "vitalik", "ether", "erc-20", "defi"],
    "SOLUSDT":  ["solana", "sol", "solana network"],
    "BNBUSDT":  ["binance", "bnb", "bsc", "binance coin"],
    "XRPUSDT":  ["ripple", "xrp", "ripple labs"],
    "ADAUSDT":  ["cardano", "ada"],
    "DOGEUSDT": ["dogecoin", "doge", "elon musk doge"],
    "AVAXUSDT": ["avalanche", "avax"],
    "DOTUSDT":  ["polkadot", "dot"],
    "LINKUSDT": ["chainlink", "link"],
    "LTCUSDT":  ["litecoin", "ltc"],
    "MATICUSDT":["polygon", "matic"],
    "ATOMUSDT": ["cosmos", "atom"],
    "UNIUSDT":  ["uniswap", "uni"],
    "AAVEUSDT": ["aave"],
}

# Términos generales que afectan TODO el mercado cripto
GLOBAL_CRYPTO_TERMS = [
    "crypto", "cryptocurrency", "digital asset", "blockchain",
    "defi", "web3", "bitcoin", "btc", "altcoin",
    "fed", "federal reserve", "interest rate", "inflation",
    "sec", "cftc", "regulation", "cbdc",
]

# Umbral para notificación crítica
CRITICAL_THRESHOLD = 0.65


# ══════════════════════════════════════════════════════════
#  ANALIZADOR DE TEXTO
# ══════════════════════════════════════════════════════════

def analyze_text(text: str) -> Tuple[float, str, List[str]]:
    """
    Analiza un texto y retorna:
      - sentiment_score: -1.0 (muy bajista) a +1.0 (muy alcista)
      - direction: "BULLISH" | "BEARISH" | "NEUTRAL"
      - matched_keywords: lista de palabras clave encontradas
    """
    text_lower = text.lower()

    # Detectar amplificadores
    amplifier = 1.0
    for amp, mult in AMPLIFIERS.items():
        if amp in text_lower:
            amplifier = max(amplifier, mult)

    bull_score = 0.0
    bear_score = 0.0
    matched = []

    for kw, weight in BULLISH_KEYWORDS.items():
        if kw in text_lower:
            bull_score += weight * amplifier
            matched.append(f"+{kw}")

    for kw, weight in BEARISH_KEYWORDS.items():
        if kw in text_lower:
            bear_score += weight * amplifier
            matched.append(f"-{kw}")

    net = bull_score - bear_score
    # Normalizar a [-1, +1]
    max_possible = 5.0
    score = max(-1.0, min(1.0, net / max_possible))

    if score > 0.15:   direction = "BULLISH"
    elif score < -0.15: direction = "BEARISH"
    else:               direction = "NEUTRAL"

    return round(score, 3), direction, matched[:8]


def is_relevant_for_symbol(text: str, symbol: str) -> bool:
    """Determina si una noticia es relevante para un símbolo específico"""
    text_lower = text.lower()
    terms = SYMBOL_KEYWORDS.get(symbol, []) + GLOBAL_CRYPTO_TERMS
    return any(term in text_lower for term in terms)


# ══════════════════════════════════════════════════════════
#  PARSER DE RSS
# ══════════════════════════════════════════════════════════

def fetch_rss(url: str, timeout: int = 8) -> List[Dict]:
    """Descarga y parsea un feed RSS, retorna lista de items"""
    items = []
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"
        })
        with urlopen(req, timeout=timeout) as resp:
            content = resp.read()

        root = ET.fromstring(content)
        # RSS 2.0
        channel = root.find("channel")
        if channel is None:
            channel = root  # Atom feed

        for item in channel.findall("item")[:20]:  # últimos 20
            title   = (item.findtext("title")       or "").strip()
            desc    = (item.findtext("description") or "").strip()
            pub_date= (item.findtext("pubDate")     or "").strip()
            link    = (item.findtext("link")        or "").strip()

            # Limpiar HTML del description
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()

            items.append({
                "title":    title,
                "desc":     desc,
                "pub_date": pub_date,
                "link":     link,
                "full_text": f"{title} {desc}",
            })
    except URLError as e:
        log.debug(f"RSS {url}: {e}")
    except ET.ParseError as e:
        log.debug(f"XML parse {url}: {e}")
    except Exception as e:
        log.debug(f"RSS fetch {url}: {e}")
    return items


# ══════════════════════════════════════════════════════════
#  FEAR & GREED INDEX
# ══════════════════════════════════════════════════════════

def fetch_fear_greed() -> Dict:
    """
    Obtiene el Fear & Greed Index de alternative.me
    Retorna: {"value": 0-100, "label": "Extreme Fear|Fear|Neutral|Greed|Extreme Greed"}
    """
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        data = r.json()
        d = data["data"][0]
        value = int(d["value"])
        label = d["value_classification"]
        score_adj = (value - 50) / 100   # -0.5 a +0.5
        return {
            "value":      value,
            "label":      label,
            "score_adj":  round(score_adj, 3),
            "ts":         int(time.time()),
        }
    except Exception as e:
        log.debug(f"Fear&Greed: {e}")
        return {"value": 50, "label": "Neutral", "score_adj": 0.0, "ts": int(time.time())}


# ══════════════════════════════════════════════════════════
#  MOTOR PRINCIPAL
# ══════════════════════════════════════════════════════════

class NewsEngine:
    """
    Motor de noticias que corre en background y provee señales
    de sentimiento al bot de trading.
    """

    def __init__(self, telegram_notifier=None, scan_interval: int = 120):
        self.tg              = telegram_notifier
        self.scan_interval   = scan_interval

        # Estado interno
        self._seen_urls: set             = set()    # URLs ya procesadas
        self._news_cache: List[Dict]     = []       # últimas noticias analizadas
        self._symbol_bias: Dict[str, float] = {}    # bias por símbolo [-1, +1]
        self._global_bias: float         = 0.0      # bias global del mercado
        self._fear_greed: Dict           = {"value": 50, "label": "Neutral", "score_adj": 0.0}
        self._lock                       = threading.Lock()
        self._running                    = False
        self._last_scan_ts               = 0

        log.info(f"NewsEngine iniciado — scan cada {scan_interval}s")

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="news-engine").start()
        log.info("NewsEngine background thread iniciado")

    def stop(self):
        self._running = False

    # ── Loop principal ────────────────────────────────────────────────────────

    def _loop(self):
        # Primera scan inmediata
        self._scan()
        while self._running:
            time.sleep(self.scan_interval)
            try:
                self._scan()
            except Exception as e:
                log.error(f"NewsEngine loop error: {e}")

    def _scan(self):
        log.info("📰 Escaneando noticias...")
        all_items = []

        # Obtener todos los feeds
        for feed in RSS_FEEDS:
            items = fetch_rss(feed["url"])
            for item in items:
                item["source"]        = feed["name"]
                item["feed_weight"]   = feed["weight"]
                item["feed_category"] = feed["category"]
            all_items.extend(items)
            time.sleep(0.3)

        # Fear & Greed (cada 10 minutos)
        if int(time.time()) - self._fear_greed.get("ts", 0) > 600:
            fg = fetch_fear_greed()
            with self._lock:
                self._fear_greed = fg
            log.info(f"Fear & Greed: {fg['value']} — {fg['label']}")

        # Procesar noticias nuevas
        new_items  = []
        alerts     = []
        sym_scores: Dict[str, List[float]] = {}
        global_scores: List[float]         = []

        for item in all_items:
            url = item.get("link", item["title"])  # usar link como ID, fallback title
            if url in self._seen_urls:
                continue
            self._seen_urls.add(url)

            text  = item["full_text"]
            score, direction, keywords = analyze_text(text)

            if direction == "NEUTRAL":
                continue

            weighted_score = score * item["feed_weight"]
            item["sentiment_score"] = round(score, 3)
            item["direction"]       = direction
            item["keywords"]        = keywords
            new_items.append(item)

            # Acumular score global
            global_scores.append(weighted_score)

            # Asignar a símbolos relevantes
            for sym in list(SYMBOL_KEYWORDS.keys()) + ["BTCUSDT"]:
                if is_relevant_for_symbol(text, sym):
                    sym_scores.setdefault(sym, []).append(weighted_score)

            # Detectar noticias críticas
            if abs(score) >= CRITICAL_THRESHOLD:
                alerts.append({
                    "item":      item,
                    "score":     score,
                    "direction": direction,
                    "keywords":  keywords,
                })

        # Actualizar biases
        with self._lock:
            # Global
            if global_scores:
                self._global_bias = round(
                    sum(global_scores) / len(global_scores), 3
                )
            # Por símbolo
            for sym, scores in sym_scores.items():
                self._symbol_bias[sym] = round(
                    sum(scores) / len(scores), 3
                )
            # Añadir al caché (últimas 50)
            self._news_cache = (new_items + self._news_cache)[:50]

        self._last_scan_ts = int(time.time())

        # Enviar alertas críticas a Telegram
        for alert in alerts[:3]:  # máximo 3 alertas por scan
            self._send_alert(alert)

        # Resumen periódico
        if new_items:
            log.info(
                f"📰 {len(new_items)} noticias nuevas | "
                f"Global bias: {self._global_bias:+.2f} | "
                f"Alertas: {len(alerts)}"
            )
            self._send_summary(new_items[:5])

    def _send_alert(self, alert: Dict):
        """Envía alerta crítica inmediata a Telegram"""
        item      = alert["item"]
        score     = alert["score"]
        direction = alert["direction"]
        keywords  = alert["keywords"]
        emoji     = "🚨🟢" if direction == "BULLISH" else "🚨🔴"

        msg = (
            f"{emoji} <b>NOTICIA CRÍTICA — {direction}</b>\n"
            f"Fuente: {item['source']}\n\n"
            f"<b>{item['title'][:200]}</b>\n\n"
            f"Impacto: <code>{score:+.2f}</code>  "
            f"({'Muy alcista' if score > 0 else 'Muy bajista'})\n"
            f"Keywords: {', '.join(keywords[:5])}\n"
            f"Fear & Greed: {self._fear_greed['value']} — {self._fear_greed['label']}"
        )
        if self.tg:
            self.tg.send(msg)
        log.warning(f"ALERTA CRÍTICA: {item['title'][:80]} | score={score:+.2f}")

    def _send_summary(self, items: List[Dict]):
        """Envía resumen de noticias relevantes a Telegram"""
        if not self.tg:
            return
        fg = self._fear_greed
        lines = [
            f"📰 <b>Resumen de noticias</b>",
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

    # ── API pública ───────────────────────────────────────────────────────────

    def get_news_bias(self, symbol: str) -> Dict:
        """
        Retorna el bias de noticias para un símbolo.
        El bot lo usa para ajustar/bloquear señales técnicas.

        Retorna:
          news_score    : -1.0 a +1.0
          direction     : BULLISH | BEARISH | NEUTRAL
          fear_greed    : 0-100
          fg_label      : Extreme Fear | Fear | Neutral | Greed | Extreme Greed
          fg_adj        : -0.5 a +0.5 (ajuste de score por F&G)
          recent_alerts : número de alertas críticas en las últimas 2h
          should_block  : True si hay una noticia muy negativa que bloquea el trade
        """
        with self._lock:
            sym_score   = self._symbol_bias.get(symbol,
                          self._symbol_bias.get("BTCUSDT", self._global_bias))
            fg          = dict(self._fear_greed)
            cache       = list(self._news_cache)

        # Contar alertas críticas recientes (últimas 2h)
        cutoff = int(time.time()) - 7200
        recent_alerts = sum(
            1 for item in cache
            if abs(item.get("sentiment_score", 0)) >= CRITICAL_THRESHOLD
            and item.get("ts", 0) > cutoff
        )

        # Bloquear trade si hay noticia muy negativa reciente
        should_block = (sym_score <= -0.6 and recent_alerts >= 1)

        direction = "NEUTRAL"
        if sym_score >= 0.2:    direction = "BULLISH"
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
            "global_bias":  nb,
            "fear_greed":   fg.get("value", 50),
            "fg_label":     fg.get("label", "Neutral"),
            "total_cached": ns,
            "last_scan":    self._last_scan_ts,
            "symbol_biases": dict(self._symbol_bias),
        }
