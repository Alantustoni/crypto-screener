import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
# v3.1 — FAPI desabilitado: funding rates + OI retornam vazio sem chamada de rede.
# Sem sessões: requests.get() direto, sem pool de conexões, sem CLOSE_WAIT.

BINANCE_BASE  = "https://api.binance.com/api/v3"
BINANCE_FAPI  = "https://fapi.binance.com/fapi/v1"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "close",
}

def _get(url: str, params=None, timeout=10):
    """Wrapper de requests.get sem sessão. Nunca reutiliza conexões."""
    return requests.get(url, params=params, headers=_HEADERS, timeout=timeout)

TOP_PAIRS = [
    # ── Top 50 originais ──────────────────────────────────────────────
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","DOTUSDT","POLUSDT",
    "LINKUSDT","UNIUSDT","AAVEUSDT","MKRUSDT","COMPUSDT",
    "ATOMUSDT","LTCUSDT","NEARUSDT","APTUSDT","SUIUSDT",
    "ARBUSDT","OPUSDT","SEIUSDT","INJUSDT","TIAUSDT",
    "FETUSDT","RENDERUSDT","WLDUSDT","AGIXUSDT","OCEANUSDT",
    "PEPEUSDT","FLOKIUSDT","WIFUSDT","BONKUSDT","SHIBUSDT",
    "PYTHUSDT","TONUSDT","ENAUSDT","PENDLEUSDT","EIGENUSDT",
    "JUPUSDT","TAOUSDT","ARKMUSDT","NOTUSDT","WOOUSDT",
    "STXUSDT","RUNEUSDT","KASUSDT","CELOUSDT","GALAUSDT",
    # ── Novas 42 (removidas as de baixa liquidez: PORTAL,PIXEL,REZ,HOOK,BEAM,ACE,PEOPLE,BLUR) ───
    "TRXUSDT","XLMUSDT","HBARUSDT","ICPUSDT","FILUSDT",
    "LDOUSDT","CRVUSDT","GMXUSDT","DYDXUSDT","SNXUSDT",
    "SANDUSDT","MANAUSDT","AXSUSDT","IMXUSDT","FLOWUSDT",
    "EGLDUSDT","FTMUSDT","CAKEUSDT","GRTUSDT","VETUSDT",
    "ORDIUSDT","MNTUSDT","ALTUSDT","ZKUSDT",
    "DYMUSDT","ZROUSDT","LISTAUSDT","CFXUSDT","STGUSDT",
    "1INCHUSDT","BALUSDT","ALGOUSDT","XTZUSDT","BANDUSDT",
    "BBUSDT","ZRXUSDT","ENJUSDT","CYBERUSDT",
    "CHZUSDT","APEUSDT","GMTUSDT","MAGICUSDT",
]

# ── Classificação por setor (para detecção de cascade)
SETORES: dict = {
    "BTC":     ["BTCUSDT"],
    "ETH":     ["ETHUSDT"],
    "L1":      ["SOLUSDT","AVAXUSDT","NEARUSDT","APTUSDT","SUIUSDT","TONUSDT","KASUSDT","ADAUSDT",
                 "TRXUSDT","XLMUSDT","HBARUSDT","ICPUSDT","EGLDUSDT","FTMUSDT","FLOWUSDT","ALGOUSDT","VETUSDT"],
    "DeFi":    ["UNIUSDT","AAVEUSDT","MKRUSDT","COMPUSDT","PENDLEUSDT","JUPUSDT",
                 "LDOUSDT","CRVUSDT","GMXUSDT","DYDXUSDT","SNXUSDT","CAKEUSDT","1INCHUSDT","BALUSDT","STGUSDT"],
    "Layer2":  ["ARBUSDT","OPUSDT","POLUSDT","STXUSDT","MNTUSDT","ZKUSDT","ALTUSDT"],
    "AI":      ["FETUSDT","RENDERUSDT","AGIXUSDT","OCEANUSDT","TAOUSDT","WLDUSDT"],
    "Meme":    ["DOGEUSDT","SHIBUSDT","PEPEUSDT","FLOKIUSDT","WIFUSDT","BONKUSDT","NOTUSDT",
                 "LUNCUSDT","PEOPLEUSDT","BTTUSDT"],
    "Interop": ["DOTUSDT","ATOMUSDT","LINKUSDT","RUNEUSDT","XRPUSDT","ZROUSDT","WUSDT"],
    "Exchange":["BNBUSDT","WOOUSDT"],
    "Gaming":  ["SANDUSDT","MANAUSDT","AXSUSDT","IMXUSDT","ENJUSDT","BEAMUSDT","PIXELUSDT","HOOKUSDT",
                 "CHZUSDT","APEUSDT","GMTUSDT","MAGICUSDT"],
    "Infra":   ["FILUSDT","GRTUSDT","BANDUSDT","ZRXUSDT","CFXUSDT","XTZUSDT","DYMUSDT","STGUSDT"],
    "Other":   ["LTCUSDT","INJUSDT","TIAUSDT","SEIUSDT","EIGENUSDT",
                 "PYTHUSDT","ENAUSDT","ARKMUSDT","CELOUSDT","GALAUSDT",
                 "ORDIUSDT","BLURUSDT","LISTAUSDT","BBUSDT","PORTALUSDT",
                 "REZUSDT","ACEUSDT","CYBERUSDT","PEOPLEUSDT"],
}
_SETOR_MAP: dict = {sym: s for s, syms in SETORES.items() for sym in syms}

INTERVALOS = {"15m":"15m","1h":"1h","4h":"4h","1d":"1d"}

TIMEFRAME_SUPERIOR = {"15m":"1h","1h":"4h","4h":"1d","1d":None}

# ─────────────────────────── CONFIG DINÂMICA ───────────────────────────

_CONFIG_PATH = Path(__file__).parent / "screener_config.json"
_config_cache: dict = {}
_config_cache_ts: datetime | None = None
_CONFIG_TTL = 300  # segundos (5 min)

_CONFIG_DEFAULTS: dict = {
    "score_minimo_setup":      8,
    "score_minimo_quase":      5,
    "criterios_minimos_setup": 3,
    "criterios_minimos_quase": 2,
    "rsi_maximo_setup":        58,
    "rsi_maximo_quase":        62,
    "fr_minimo_setup":         1.5,
    "fr_forte_threshold":      3.0,
    "fr_bonus_forte":          2,
    "fr_bonus_normal":         1,
    "fr_penalidade":           1,
    "fr_fraca_threshold":      -1.5,
    "ob_threshold":            0.30,
    "ob_bonus":                1,
    "ob_penalidade":           2,
    "mtf_bonus":               2,
    "mtf_contradiz_penalidade":3,
    "market_bearish_penalidade":2,
    "meme_penalidade":         2,
    "cooldown_min":            240,
    "circuit_breaker_pct":     -7.0,
}


def carregar_config_screener() -> dict:
    """Carrega screener_config.json com cache de 5 min. Retorna defaults se arquivo ausente."""
    global _config_cache, _config_cache_ts
    agora = datetime.now()
    if _config_cache and _config_cache_ts and (agora - _config_cache_ts).total_seconds() < _CONFIG_TTL:
        return _config_cache
    try:
        import json
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = {**_CONFIG_DEFAULTS, **{k: v for k, v in raw.items() if not k.startswith("_")}}
    except Exception:
        cfg = dict(_CONFIG_DEFAULTS)
    _config_cache    = cfg
    _config_cache_ts = agora
    return cfg


# Cache do circuit breaker macro (BTC 24h)
_macro_risco_cache: dict = {"bloqueado": False, "btc_24h": None, "ts": None}
_MACRO_TTL = 300  # 5 min


def verificar_macro_risco() -> dict:
    """Retorna se o mercado macro esta em risco (BTC -5% em 24h). Cache de 5 min."""
    global _macro_risco_cache
    agora = datetime.now()
    ts = _macro_risco_cache.get("ts")
    if ts and (agora - ts).total_seconds() < _MACRO_TTL:
        return _macro_risco_cache
    try:
        import json as _json
        ticker = requests.get(
            f"{BINANCE_BASE}/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            headers=_HEADERS, timeout=5
        ).json()
        btc_24h = float(ticker.get("priceChangePercent", 0))
        limiar = carregar_config_screener().get("circuit_breaker_pct", -7.0)
        bloqueado = btc_24h <= limiar
    except Exception:
        btc_24h, bloqueado = None, False
    _macro_risco_cache = {"bloqueado": bloqueado, "btc_24h": btc_24h, "ts": agora}
    return _macro_risco_cache


# ─────────────────────────── CACHES ───────────────────────────

_funding_cache: dict = {}
_funding_cache_time: datetime | None = None

# {interval: (timestamp, data)}
_btc_cache: dict = {}

# {symbol: (timestamp, oi_value)}
_oi_cache: dict = {}

# Fear & Greed e BTC Dominance (cache 1h)
_fear_greed_cache: dict = {"value": None, "label": None, "ts": None}
_dominance_cache:  dict = {"value": None, "ts": None}

# BTC EMA50 no 4H (cache 1h)
_btc_4h_cache: dict = {"tendencia": None, "ts": None}

# Long/Short Ratio da Binance FAPI (cache 30min por símbolo)
_ls_cache: dict = {}


# ─────────────────────────── INDICADORES ───────────────────────────

def calcular_ema(valores: list, periodo: int) -> list:
    k = 2 / (periodo + 1)
    if len(valores) < periodo:
        return [valores[0]] * len(valores)
    # Usa SMA dos primeiros N períodos como seed (elimina viés de inicialização)
    sma_inicial = sum(valores[:periodo]) / periodo
    ema = [sma_inicial]
    for v in valores[periodo:]:
        ema.append(v * k + ema[-1] * (1 - k))
    # Mantém o tamanho original do array (prefixo com valores[0] antes do período)
    return [valores[0]] * (periodo - 1) + ema


def calcular_rsi(closes: list, periodo: int = 14) -> float:
    if len(closes) < periodo + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    ag = sum(gains[:periodo]) / periodo
    al = sum(losses[:periodo]) / periodo
    for i in range(periodo, len(gains)):
        ag = (ag * (periodo-1) + gains[i]) / periodo
        al = (al * (periodo-1) + losses[i]) / periodo
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag/al), 2)


def calcular_rsi_serie(closes: list, periodo: int = 14) -> list:
    if len(closes) < periodo + 2:
        return [50.0] * len(closes)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    rsi_list = [50.0] * periodo
    ag = sum(max(0,d) for d in deltas[:periodo]) / periodo
    al = sum(max(0,-d) for d in deltas[:periodo]) / periodo
    rsi_list.append(100.0 if al == 0 else round(100 - 100/(1+ag/al), 2))
    for i in range(periodo, len(deltas)):
        ag = (ag*(periodo-1) + max(0, deltas[i])) / periodo
        al = (al*(periodo-1) + max(0,-deltas[i])) / periodo
        rsi_list.append(100.0 if al == 0 else round(100 - 100/(1+ag/al), 2))
    return rsi_list


# ─────────────────────────── DIVERGÊNCIA RSI ───────────────────────────

def encontrar_minimos(valores: list, janela: int = 3) -> list:
    idx = []
    for i in range(janela, len(valores)-janela):
        if all(valores[i] <= valores[i-j] for j in range(1,janela+1)) and \
           all(valores[i] <= valores[i+j] for j in range(1,janela+1)):
            idx.append(i)
    return idx

def encontrar_maximos(valores: list, janela: int = 3) -> list:
    idx = []
    for i in range(janela, len(valores)-janela):
        if all(valores[i] >= valores[i-j] for j in range(1,janela+1)) and \
           all(valores[i] >= valores[i+j] for j in range(1,janela+1)):
            idx.append(i)
    return idx


def detectar_divergencia(closes: list, rsi_serie: list, janela: int = 50) -> str | None:
    n = min(janela, len(closes), len(rsi_serie))
    if n < 20:
        return None
    c = closes[-n:]
    r = rsi_serie[-n:]
    mins = encontrar_minimos(c, janela=3)
    if len(mins) >= 2:
        i1, i2 = mins[-2], mins[-1]
        # Diferença absoluta de +5 pontos RSI (mais rigoroso que % relativo)
        if c[i2] < c[i1] * 0.995 and r[i2] > r[i1] + 5:
            return "bullish"
    maxs = encontrar_maximos(c, janela=3)
    if len(maxs) >= 2:
        i1, i2 = maxs[-2], maxs[-1]
        # Diferença absoluta de -5 pontos RSI
        if c[i2] > c[i1] * 1.005 and r[i2] < r[i1] - 5:
            return "bearish"
    return None


# ─────────────────────────── MARKET STRUCTURE ───────────────────────────

def detectar_market_structure(highs: list, lows: list, janela: int = 5) -> str | None:
    """Detecta HH/HL (bullish) ou LL/LH (bearish) nos últimos 40 candles.
    janela=5 e lookback=40 reduzem falsos positivos vs janela=3/lookback=30.
    """
    if len(highs) < 20 or len(lows) < 20:
        return None
    h = highs[-40:]
    l = lows[-40:]
    swing_highs = encontrar_maximos(h, janela=janela)
    swing_lows  = encontrar_minimos(l, janela=janela)
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        i1h, i2h = swing_highs[-2], swing_highs[-1]
        i1l, i2l = swing_lows[-2], swing_lows[-1]
        hh = h[i2h] > h[i1h] * 1.002
        hl = l[i2l] > l[i1l] * 1.001
        if hh and hl:
            return "bullish"
        ll = l[i2l] < l[i1l] * 0.999
        lh = h[i2h] < h[i1h] * 0.999
        if ll and lh:
            return "bearish"
    return None


# ─────────────────────────── FUNDING RATE ───────────────────────────

def get_all_funding_rates() -> dict:
    global _funding_cache, _funding_cache_time
    agora = datetime.now()
    if _funding_cache_time and (agora - _funding_cache_time) < timedelta(minutes=5):
        return _funding_cache
    try:
        # v3.1 — FAPI bloqueado; retorna cache vazio rápido sem travar
        return _funding_cache
    except Exception:
        return _funding_cache


# ─────────────────────────── BTC PERFORMANCE ───────────────────────────

def get_btc_performance(interval: str = "1h") -> dict | None:
    """Retorna dados de closes do BTC no intervalo (cache 5 min)."""
    global _btc_cache
    agora = datetime.now()
    cached = _btc_cache.get(interval)
    if cached and (agora - cached[0]) < timedelta(minutes=5):
        return cached[1]
    try:
        klines = get_klines("BTCUSDT", interval=interval, limit=50)
        if not klines or len(klines) < 10:
            return None
        closes = [float(k[4]) for k in klines]
        change = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2)
        result = {"closes": closes, "change_pct": change}
        _btc_cache[interval] = (agora, result)
        return result
    except Exception:
        return None


# ─────────────────────────── CVD ───────────────────────────

def get_cvd(symbol: str, limit: int = 200) -> tuple:
    """
    v3.1 — CVD desabilitado (era a causa do scan lento: 50 chamadas extras via /trades).
    Para reativar: remova o 'return None, None' e descomente o bloco abaixo.
    """
    return None, None
    # ── Código desabilitado ──
    # try:
    #     r = _get(f"{BINANCE_BASE}/trades", params={"symbol": symbol, "limit": limit}, timeout=8)
    #     r.raise_for_status()
    #     trades = r.json()
    #     buy_vol  = sum(float(t["qty"]) for t in trades if not t["isBuyerMaker"])
    #     sell_vol = sum(float(t["qty"]) for t in trades if t["isBuyerMaker"])
    #     total    = buy_vol + sell_vol
    #     if total == 0: return None, None
    #     cvd = round((buy_vol - sell_vol) / total * 100, 1)
    #     return cvd, ("bullish" if cvd > 15 else "bearish" if cvd < -15 else "neutro")
    # except Exception:
    #     return None, None


# ─────────────────────────── OPEN INTEREST ───────────────────────────

def get_open_interest_change(symbol: str) -> tuple:
    """
    v3.1 — OI desabilitado (FAPI menos estável que spot API).
    Para reativar: remova o 'return None, None' e descomente o bloco abaixo.
    """
    return None, None
    # ── Código desabilitado ──
    # global _oi_cache
    # agora = datetime.now()
    # try:
    #     r = _get(f"{BINANCE_FAPI}/openInterest", params={"symbol": symbol}, timeout=2)
    #     r.raise_for_status()
    #     oi_atual = float(r.json()["openInterest"])
    # except Exception:
    #     return None, None
    # prev = _oi_cache.get(symbol)
    # _oi_cache[symbol] = (agora, oi_atual)
    # if not prev: return oi_atual, None
    # prev_time, prev_oi = prev
    # diff_min = (agora - prev_time).total_seconds() / 60
    # if 1 <= diff_min <= 120 and prev_oi > 0:
    #     return oi_atual, round((oi_atual - prev_oi) / prev_oi * 100, 3)
    # return oi_atual, None


# ─────────────────────────── FORÇA RELATIVA ───────────────────────────

def calcular_efficiency_ratio(closes: list, n: int = 10) -> float:
    """
    Efficiency Ratio de Kaufman — mede se o mercado está em tendência ou lateralizado.
    ER = |close[-1] - close[-n]| / soma dos movimentos absolutoss
    ER próximo de 1.0 = tendência forte. ER próximo de 0 = lateral/ruído.
    """
    if len(closes) < n + 1:
        return 0.0
    direcao = abs(closes[-1] - closes[-n-1])
    ruido   = sum(abs(closes[-i] - closes[-i-1]) for i in range(1, n+1))
    return round(direcao / ruido, 3) if ruido > 0 else 0.0


def calcular_atr(highs: list, lows: list, closes: list, periodo: int = 14) -> float:
    """ATR (Average True Range) — mede volatilidade real do mercado."""
    if len(highs) < periodo + 1:
        return 0.0
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]))
        for i in range(1, len(highs))
    ]
    return round(sum(trs[-periodo:]) / periodo, 8) if len(trs) >= periodo else 0.0


def calcular_zscore_volume(volumes: list, periodo: int = 20) -> float:
    """
    Z-Score estatístico do volume atual vs histórico.
    Z > 2.0 = volume anômalo (provável entrada institucional).
    Mais preciso que simples ratio 2x.
    """
    if len(volumes) < periodo + 1:
        return 0.0
    historico = volumes[-periodo-1:-1]  # exclui candle atual
    media = sum(historico) / periodo
    variancia = sum((v - media) ** 2 for v in historico) / periodo
    desvio = variancia ** 0.5
    return round((volumes[-1] - media) / desvio, 2) if desvio > 0 else 0.0


def calcular_vwap(klines: list, janela: int = 24) -> float:
    """
    VWAP rolling das últimas `janela` candles.
    Preço acima do VWAP = compradores controlam. Abaixo = vendedores.
    """
    ultimos = klines[-janela:] if len(klines) >= janela else klines
    total_pv = sum(
        ((float(k[2]) + float(k[3]) + float(k[4])) / 3) * float(k[5])
        for k in ultimos
    )
    total_v = sum(float(k[5]) for k in ultimos)
    return round(total_pv / total_v, 8) if total_v > 0 else 0.0


# ─────────────────────────── CONTEXTO DE MERCADO ───────────────────────────

def get_fear_greed() -> dict:
    """
    Índice Fear & Greed (alternative.me). Cache 1h. Grátis, sem autenticação.
    0–24 = Medo Extremo (contrário bullish) | 75–100 = Ganância Extrema (cautela)
    """
    global _fear_greed_cache
    agora = datetime.now()
    ts = _fear_greed_cache.get("ts")
    if ts and (agora - ts) < timedelta(hours=1):
        return {k: v for k, v in _fear_greed_cache.items() if k != "ts"}
    try:
        r = _get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        _fear_greed_cache = {
            "value": int(d["value"]),
            "label": d["value_classification"],
            "ts": agora,
        }
    except Exception:
        pass
    return {k: v for k, v in _fear_greed_cache.items() if k != "ts"}


def get_btc_dominance() -> float | None:
    """
    Dominância BTC no market cap total via CoinGecko. Cache 1h.
    > 60% → BTC dominando, altcoins desfavorecidas
    < 45% → Altseason, alts com mais espaço para subir
    """
    global _dominance_cache
    agora = datetime.now()
    ts = _dominance_cache.get("ts")
    if ts and (agora - ts) < timedelta(hours=1):
        return _dominance_cache["value"]
    try:
        r = _get("https://api.coingecko.com/api/v3/global", timeout=10)
        val = round(r.json()["data"]["market_cap_percentage"]["btc"], 1)
        _dominance_cache = {"value": val, "ts": agora}
        return val
    except Exception:
        return _dominance_cache.get("value")


def get_btc_4h_tendencia() -> str | None:
    """
    Verifica se o BTC está acima ou abaixo da EMA50 no gráfico de 4H. Cache 1h.
    'acima_ema50' = macro bullish | 'abaixo_ema50' = macro bearish para altcoins.
    """
    global _btc_4h_cache
    agora = datetime.now()
    ts = _btc_4h_cache.get("ts")
    if ts and (agora - ts).total_seconds() < 3600:
        return _btc_4h_cache["tendencia"]
    try:
        klines = get_klines("BTCUSDT", interval="4h", limit=55)
        if not klines or len(klines) < 50:
            return None
        closes_4h = [float(k[4]) for k in klines]
        ema50_4h  = calcular_ema(closes_4h, 50)[-1]
        tendencia = "acima_ema50" if closes_4h[-1] > ema50_4h else "abaixo_ema50"
        _btc_4h_cache = {"tendencia": tendencia, "ts": agora}
        return tendencia
    except Exception:
        return None


def get_long_short_ratio(symbol: str) -> float | None:
    """
    Long/Short Account Ratio global da Binance FAPI. Cache 30min por símbolo.
    Ratio = longs/shorts. Ex: 2.0 = 67% do mercado está comprado (long).
    > 2.5 = varejo sobrecarregado long → risco de liquidação descendente (-2)
    > 1.8 = muito long → cautela (-1)
    < 0.55 = maioria short → short squeeze potencial (+1)
    < 0.4  = extremo short → squeeze elevado (+2)
    """
    global _ls_cache
    agora = datetime.now()
    cached = _ls_cache.get(symbol)
    if cached and (agora - cached[0]).total_seconds() < 1800:
        return cached[1]
    try:
        r = _get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": "1h", "limit": 1},
            timeout=5
        )
        data = r.json()
        if isinstance(data, list) and data:
            ratio = float(data[0]["longShortRatio"])
            _ls_cache[symbol] = (agora, ratio)
            return ratio
    except Exception:
        pass
    return None


def get_order_book_imbalance(symbol: str, depth: int = 20) -> float | None:
    """
    Pressão compradora vs vendedora no order book.
    Imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    > +0.30 = compradores dominando | < -0.30 = vendedores dominando
    """
    try:
        r = _get(f"{BINANCE_BASE}/depth",
                 params={"symbol": symbol, "limit": depth}, timeout=8)
        d = r.json()
        bid_vol = sum(float(b[1]) for b in d.get("bids", []))
        ask_vol = sum(float(a[1]) for a in d.get("asks", []))
        total   = bid_vol + ask_vol
        return round((bid_vol - ask_vol) / total, 3) if total > 0 else None
    except Exception:
        return None


def calcular_shadow_rsi(highs: list, lows: list, periodo: int = 14) -> float:
    """
    RSI calculado sobre (high+low)/2 em vez do close.
    Reduz manipulação por wicks — sinal mais limpo.
    Quando Shadow RSI e RSI normal concordam → confirmação dupla.
    """
    medians = [(highs[i] + lows[i]) / 2 for i in range(len(highs))]
    return calcular_rsi(medians, periodo)


def calcular_forca_relativa(closes: list, btc_closes: list, n: int = 12) -> float | None:
    """Retorno da moeda − retorno BTC nos últimos n candles (padrão 12 = ~12h em 1h)."""
    if len(closes) < n+1 or len(btc_closes) < n+1:
        return None
    coin_ret = (closes[-1] - closes[-n-1]) / closes[-n-1] * 100
    btc_ret  = (btc_closes[-1] - btc_closes[-n-1]) / btc_closes[-n-1] * 100
    return round(coin_ret - btc_ret, 2)


# ─────────────────────────── API BINANCE ───────────────────────────

def get_klines(symbol: str, interval: str = "1h", limit: int = 100):
    try:
        r = _get(f"{BINANCE_BASE}/klines",
                              params={"symbol": symbol, "interval": interval, "limit": limit},
                              timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def get_ticker_24h(symbol: str) -> dict:
    try:
        r = _get(f"{BINANCE_BASE}/ticker/24hr",
                              params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


# ─────────────────────────── ANÁLISE BASE ───────────────────────────

def _score_basico(closes: list, volumes: list) -> dict:
    rsi   = calcular_rsi(closes)
    ema9  = calcular_ema(closes, 9)[-1]
    ema21 = calcular_ema(closes, 21)[-1]
    ema50 = calcular_ema(closes, 50)[-1]
    preco = closes[-1]

    vol_media = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else sum(volumes)/len(volumes)
    vol_ratio = round(volumes[-1] / vol_media, 2) if vol_media > 0 else 0

    sinais = []
    score  = 0

    if rsi <= 20:   sinais.append("🔪 Faca Caindo"); score -= 4   # RSI extremo = capitulação, evitar
    elif rsi < 35:  sinais.append("RSI Oversold"); score += 2
    elif rsi < 40:  sinais.append("RSI Baixo");    score += 1
    elif rsi > 70:  sinais.append("RSI Overbought"); score -= 2
    elif rsi > 60:  sinais.append("RSI Alto");       score -= 1

    # EMA Spread — força da tendência de curto prazo (mede %divergência EMA9 vs EMA21)
    ema_spread_pct = round((ema9 - ema21) / ema21 * 100, 3) if ema21 > 0 else 0.0
    if ema_spread_pct > 1.0:    sinais.append("📈 EMA spread forte"); score += 2
    elif ema_spread_pct > 0.5:  sinais.append("📈 EMA spread +");     score += 1
    elif ema_spread_pct < -1.0: sinais.append("📉 EMA spread fraco"); score -= 2
    elif ema_spread_pct < -0.5: sinais.append("📉 EMA spread -");     score -= 1

    if ema21 > ema50: sinais.append("Tendência Alta"); score += 1
    else:             sinais.append("Tendência Baixa"); score -= 1

    if preco > ema50: score += 1
    else:             score -= 1

    if vol_ratio > 2: sinais.append(f"Volume x{vol_ratio} ⚡"); score += 1

    def fmt(v):
        return round(v,6) if v<0.01 else round(v,4) if v<1 else round(v,2)

    return {
        "rsi":rsi, "ema9":fmt(ema9), "ema21":fmt(ema21), "ema50":fmt(ema50),
        "vol_ratio":vol_ratio, "sinais":sinais, "score":score,
        "ema_spread_pct": ema_spread_pct,
    }


# ─────────────────────────── PADRÕES DE VELA ───────────────────────────

def detectar_padrao_vela(opens: list, highs: list, lows: list, closes: list) -> str | None:
    """
    Detecta padrões de reversão bullish no candle mais recente.
    - Hammer:   pavio inferior ≥ 2x corpo, pavio superior pequeno
    - Engulfing: candle verde engole o candle vermelho anterior
    - Doji:      corpo < 10% da amplitude (indecisão no suporte)
    """
    if len(closes) < 3:
        return None

    o,  h,  l,  c  = opens[-1],  highs[-1],  lows[-1],  closes[-1]
    o2, h2, l2, c2 = opens[-2],  highs[-2],  lows[-2],  closes[-2]

    corpo     = abs(c  - o)
    amplitude = h - l
    if amplitude == 0:
        return None

    pavio_inf = min(o, c) - l
    pavio_sup = h - max(o, c)

    # Hammer: corpo no topo, sombra inferior longa
    if corpo > 0 and pavio_inf >= 2 * corpo and pavio_sup <= 0.3 * corpo:
        return "hammer"

    # Bullish Engulfing: anterior vermelho, atual verde engolindo
    if c2 < o2 and c > o and c >= o2 and o <= c2:
        return "engulfing"

    # Doji: indecisão — candle com corpo < 10% da amplitude
    if corpo / amplitude < 0.10:
        return "doji"

    return None


# ─────────────────────────── ANÁLISE COMPLETA ───────────────────────────

def analisar_par(symbol: str, interval: str = "1h",
                 funding_rates: dict = None, btc_data: dict = None,
                 fear_greed: dict = None):

    cfg = carregar_config_screener()   # parâmetros dinâmicos (auto-calibração)

    # Win rate histórico desta moeda (calculado pela auto-calibração)
    win_rates_por_moeda = cfg.get("win_rate_por_moeda", {})
    wr_moeda = win_rates_por_moeda.get(symbol)  # ex: {"win_rate": 65, "n": 8}

    klines = get_klines(symbol, interval=interval, limit=100)
    if not klines or len(klines) < 60:
        return None

    opens   = [float(k[1]) for k in klines]
    closes  = [float(k[4]) for k in klines]
    highs   = [float(k[2]) for k in klines]
    lows    = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    base = _score_basico(closes, volumes)

    # ── Divergência RSI
    rsi_serie   = calcular_rsi_serie(closes)
    divergencia = detectar_divergencia(closes, rsi_serie)
    if divergencia == "bullish":
        base["sinais"].append("📐 Div. Bullish"); base["score"] += 2
    elif divergencia == "bearish":
        base["sinais"].append("📐 Div. Bearish"); base["score"] -= 2

    # ── Padrões de Vela
    padrao_vela = detectar_padrao_vela(opens, highs, lows, closes)
    if padrao_vela == "hammer":
        base["sinais"].append("🔨 Hammer"); base["score"] += 1
    elif padrao_vela == "engulfing":
        base["sinais"].append("🕯️ Engulfing Bullish"); base["score"] += 1
    elif padrao_vela == "doji":
        base["sinais"].append("➕ Doji (indecisão)")  # neutro, só informa

    # ── Funding Rate
    fr = (funding_rates or {}).get(symbol)
    funding_sinal = None
    if fr is not None:
        if fr < -0.05:
            funding_sinal = "squeeze"
            base["sinais"].append(f"🔥 Short Squeeze ({fr}%)")
            base["score"] += 2
        elif fr < -0.01:
            funding_sinal = "negativo"
            base["sinais"].append(f"⚠️ Funding neg. ({fr}%)")
            base["score"] += 1
        elif fr > 0.05:
            funding_sinal = "positivo"
            base["sinais"].append(f"⬆️ Funding pos. ({fr}%)")
            base["score"] -= 1

    # ── Multi-Timeframe: 1h→4h, 4h→1d (confirma ou contradiz o sinal)
    tf_sup = TIMEFRAME_SUPERIOR.get(interval)
    mtf_score = None
    mtf_tendencia = None
    if tf_sup:
        klines_sup = get_klines(symbol, interval=tf_sup, limit=60)
        if klines_sup and len(klines_sup) >= 30:
            c_sup = [float(k[4]) for k in klines_sup]
            v_sup = [float(k[5]) for k in klines_sup]
            sup = _score_basico(c_sup, v_sup)
            mtf_score = sup["score"]
            if mtf_score >= 2:
                mtf_tendencia = "alta"
                base["sinais"].append(f"✅ {tf_sup.upper()} confirma tendência")
                base["score"] += 2
            elif mtf_score <= -2:
                mtf_tendencia = "baixa"
                base["sinais"].append(f"❌ {tf_sup.upper()} contradiz (tendência oposta!)")
                base["score"] -= cfg["mtf_contradiz_penalidade"]

    # ── Tendência Diária (1D) — não comprar contra a tendência maior
    # Para 1h: verifica 1D. Para 4h: já pegou 1D via TIMEFRAME_SUPERIOR.
    tendencia_diaria = None
    if interval == "1h":
        klines_1d = get_klines(symbol, interval="1d", limit=30)
        if klines_1d and len(klines_1d) >= 20:
            c_1d = [float(k[4]) for k in klines_1d]
            v_1d = [float(k[5]) for k in klines_1d]
            daily = _score_basico(c_1d, v_1d)
            if daily["score"] >= 2:
                tendencia_diaria = "alta"
                base["sinais"].append("📅 1D: tendência diária de alta")
                base["score"] += 1
            elif daily["score"] <= -2:
                tendencia_diaria = "baixa"
                base["sinais"].append("📅 1D: tendência diária de baixa ⚠️")
                base["score"] -= 2

    # ── Estrutura de Mercado (HH/HL ou LL/LH)
    market_structure = detectar_market_structure(highs, lows)
    if market_structure == "bullish":
        base["sinais"].append("🏗️ Estrutura Alta (HH/HL)"); base["score"] += 1
    elif market_structure == "bearish":
        base["sinais"].append("🏗️ Estrutura Baixa (LL/LH)"); base["score"] -= cfg["market_bearish_penalidade"]

    # ── Penalidade de setor: Meme coins historicamente underperformam vs o resto
    setor_atual = _SETOR_MAP.get(symbol, "Other")
    if setor_atual == "Meme":
        base["sinais"].append("⚠️ Setor Meme (alto risco)")
        base["score"] -= cfg["meme_penalidade"]

    # Win rate histórico por moeda (mínimo 5 sinais para ativar)
    if wr_moeda and wr_moeda.get("n", 0) >= 5:
        wr = wr_moeda["win_rate"]
        if wr >= 65:
            base["sinais"].append(f"🏆 Histórico forte ({wr}% acerto, n={wr_moeda['n']})")
            base["score"] += 1
        elif wr <= 30:
            base["sinais"].append(f"⚠️ Histórico fraco ({wr}% acerto, n={wr_moeda['n']})")
            base["score"] -= 1

    # ── CVD (Cumulative Volume Delta)
    cvd_pct, cvd_sinal = get_cvd(symbol)
    if cvd_sinal == "bullish":
        base["sinais"].append(f"💹 CVD +{cvd_pct}% (compradores)"); base["score"] += 1
    elif cvd_sinal == "bearish":
        base["sinais"].append(f"📉 CVD {cvd_pct}% (vendedores)"); base["score"] -= 1

    # ── Open Interest Change + Funding combo
    oi_atual, oi_change = get_open_interest_change(symbol)
    oi_sinal = None
    if oi_change is not None:
        if oi_change > 2 and funding_sinal in ("squeeze", "negativo"):
            oi_sinal = "squeeze_fuel"
            base["sinais"].append(f"⚡ OI +{oi_change}% + Funding neg.")
            base["score"] += 2
        elif oi_change > 2 and funding_sinal == "positivo":
            oi_sinal = "longs_buildup"
            base["sinais"].append(f"⚠️ OI +{oi_change}% (longs excessivos)")
            base["score"] -= 1
        elif oi_change < -2:
            oi_sinal = "desalavancagem"
            base["sinais"].append(f"📊 OI {oi_change}% (desalavancagem)")

    # ── Força Relativa vs BTC (escalonada: >3% muito forte = +2, >1.5% = +1)
    forca_relativa = None
    if btc_data and symbol != "BTCUSDT":
        btc_closes = btc_data.get("closes", [])
        if btc_closes:
            forca_relativa = calcular_forca_relativa(closes, btc_closes)
            if forca_relativa is not None:
                if forca_relativa > cfg["fr_forte_threshold"]:
                    base["sinais"].append(f"🔥 Muito Forte vs BTC (+{forca_relativa}%)")
                    base["score"] += cfg["fr_bonus_forte"]
                elif forca_relativa > cfg["fr_minimo_setup"]:
                    base["sinais"].append(f"💪 Forte vs BTC (+{forca_relativa}%)")
                    base["score"] += cfg["fr_bonus_normal"]
                elif forca_relativa < cfg["fr_fraca_threshold"]:
                    base["sinais"].append(f"😮 Fraca vs BTC ({forca_relativa}%)")
                    base["score"] -= cfg["fr_penalidade"]

    # ── Efficiency Ratio (Kaufman) — tendência vs lateral
    er = calcular_efficiency_ratio(closes)
    if er > 0.4:
        base["sinais"].append(f"📈 ER {er} (tendência)")
        base["score"] += 1
    elif er < 0.2:
        base["sinais"].append(f"〰️ ER {er} (lateral)")
        base["score"] -= 1

    # ── Volume Z-Score — volume anômalo vs estatístico
    vol_zscore = calcular_zscore_volume(volumes)
    if vol_zscore > 2.0:
        base["sinais"].append(f"📊 Vol Z={vol_zscore} (anômalo)")
        base["score"] += 1

    # ── VWAP 24h — preço vs preço justo ponderado por volume
    vwap = calcular_vwap(klines, janela=24)
    preco_acima_vwap = closes[-1] > vwap if vwap > 0 else None
    if preco_acima_vwap:
        base["sinais"].append(f"✅ Preço > VWAP ({round((closes[-1]/vwap-1)*100, 2)}%)")
        base["score"] += 1
    elif preco_acima_vwap is False:
        base["sinais"].append(f"⬇️ Preço < VWAP ({round((closes[-1]/vwap-1)*100, 2)}%)")
        base["score"] -= 1

    # ── ATR Expansion — volatilidade em expansão = bom timing de entrada
    atr_atual  = calcular_atr(highs[-20:], lows[-20:], closes[-20:], periodo=7)
    atr_base   = calcular_atr(highs[-50:-20], lows[-50:-20], closes[-50:-20], periodo=14)
    atr_expansao = False
    if atr_base > 0 and atr_atual > 0:
        atr_ratio = round(atr_atual / atr_base, 2)
        if atr_ratio > 1.3:
            base["sinais"].append(f"💥 ATR x{atr_ratio} (expansão)")
            base["score"] += 1
            atr_expansao = True

    # ── RSI Recovery — saindo de oversold (< 40) com slope positivo
    rsi_recovery = False
    if len(rsi_serie) >= 6:
        rsi_recente = rsi_serie[-6:]
        veio_de_oversold = any(r < 40 for r in rsi_recente[:-1])   # era 35, agora 40
        slope_positivo   = rsi_recente[-1] > rsi_recente[-2] > rsi_recente[-3]
        acima_de_38      = rsi_recente[-1] > 38
        # Só conta como recovery se preço estiver acima da EMA50 (evita "gato morto")
        preco_acima_ema50 = closes[-1] > base["ema50"]
        if veio_de_oversold and acima_de_38 and slope_positivo and preco_acima_ema50:
            rsi_recovery = True
            base["sinais"].append(f"🔄 RSI Recovery ({round(rsi_recente[-1],1)})")
            base["score"] += 2

    # ── Bollinger Bands — squeeze seguido de breakout = início de movimento
    bb_sinal = None
    if len(closes) >= 20:
        bb_closes = closes[-20:]
        bb_media  = sum(bb_closes) / 20
        bb_std    = (sum((x - bb_media) ** 2 for x in bb_closes) / 20) ** 0.5
        bb_sup    = bb_media + 2 * bb_std
        bb_inf    = bb_media - 2 * bb_std
        bb_width  = round((bb_sup - bb_inf) / bb_media * 100, 2) if bb_media > 0 else 0
        preco_bb  = closes[-1]
        # Squeeze: banda estreita (< 6% da média) — tensão acumulada
        if bb_width < 6:
            bb_sinal = "squeeze"
            base["sinais"].append(f"🤏 BB Squeeze (banda {bb_width}%) — tensão")
            base["score"] += 1
        # Breakout: preço tocou a banda superior com força
        elif preco_bb >= bb_sup * 0.99:
            bb_sinal = "breakout_sup"
            base["sinais"].append(f"🚀 BB Breakout superior ({round(preco_bb/bb_sup*100,1)}%)")
            base["score"] += 2
        # Toque na banda inferior = oversold extremo
        elif preco_bb <= bb_inf * 1.01:
            bb_sinal = "toque_inf"
            base["sinais"].append(f"📉 BB Banda inferior (sobrevenda técnica)")
            base["score"] += 1
    else:
        bb_sinal = None
        bb_width = None

    # ── Distância do EMA50 — não comprar quando já esticado
    ema50_val = base["ema50"]
    ema_distancia_pct = round((closes[-1] - ema50_val) / ema50_val * 100, 2) if ema50_val > 0 else 0
    if ema_distancia_pct > 12:
        base["sinais"].append(f"🚫 Muito esticado (+{ema_distancia_pct}% acima EMA50)")
        base["score"] -= 4
    elif ema_distancia_pct > 8:
        base["sinais"].append(f"⚠️ Esticado (+{ema_distancia_pct}% acima EMA50)")
        base["score"] -= 2

    # ── Volume Confirmado — últimas 2 velas acima da média de 20
    vol_media_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
    volume_confirmado = False
    ratio_vol = 1.0  # default: assume volume normal se não há histórico suficiente
    if vol_media_20 and vol_media_20 > 0:
        vol_media_2 = (volumes[-1] + volumes[-2]) / 2
        ratio_vol = round(vol_media_2 / vol_media_20, 2)
        if ratio_vol >= 1.2:
            volume_confirmado = True
            base["sinais"].append(f"📊 Volume confirmado ({ratio_vol}× média)")
            base["score"] += 1
        elif ratio_vol < 0.6:
            base["sinais"].append(f"📉 Volume muito baixo ({int(ratio_vol*100)}% da média)")
            base["score"] -= 2  # penalidade maior (era -1)

    # ── Shadow RSI — RSI calculado no preço mediano (high+low)/2
    shadow_rsi_val = calcular_shadow_rsi(highs, lows)
    if base["rsi"] < 40 and shadow_rsi_val < 40:
        base["sinais"].append(f"🌑 Shadow RSI {shadow_rsi_val} (dupla confirmação)")
        base["score"] += 1

    # ── Order Book Imbalance — pressão compradora/vendedora em tempo real
    # Dado: MTF alta + OB>0.3 = 83.3% win rate em 4h (vs 35.5% média) → peso aumentado
    ob_imbalance = get_order_book_imbalance(symbol)
    if ob_imbalance is not None:
        if ob_imbalance > cfg["ob_threshold"]:
            base["sinais"].append(f"📚 OB {ob_imbalance:+.2f} (compradores dominando)")
            base["score"] += cfg["ob_bonus"]
        elif ob_imbalance < -cfg["ob_threshold"]:
            base["sinais"].append(f"📚 OB {ob_imbalance:+.2f} (vendedores dominando)")
            base["score"] -= cfg["ob_penalidade"]

    # ── Long/Short Ratio (Binance FAPI) — sentimento do mercado de derivativos
    # Quando 70%+ do varejo está comprado, market makers caçam esses stops para baixo.
    ls_ratio = None
    ls_sinal = None
    if symbol != "BTCUSDT":
        ls_ratio = get_long_short_ratio(symbol)
        if ls_ratio is not None:
            long_pct = round(ls_ratio / (1 + ls_ratio) * 100, 1)
            if ls_ratio > 2.5:
                ls_sinal = "extremo_long"
                base["sinais"].append(f"🚨 L/S {long_pct}% long — varejo sobrecarregado (risco caçada de stops!)")
                base["score"] -= 2
            elif ls_ratio > 1.8:
                ls_sinal = "muito_long"
                base["sinais"].append(f"⚠️ L/S {long_pct}% long — varejo muito long, cautela")
                base["score"] -= 1
            elif ls_ratio < 0.4:
                ls_sinal = "extremo_short"
                base["sinais"].append(f"🔥 L/S {long_pct}% long — short squeeze elevado!")
                base["score"] += 2
            elif ls_ratio < 0.55:
                ls_sinal = "short_squeeze"
                base["sinais"].append(f"💹 L/S {long_pct}% long — potencial short squeeze")
                base["score"] += 1

    # ── Fear & Greed — contexto global de sentimento do mercado
    # NOTA: Medo extremo em sinais 1h = risco, não oportunidade contrária.
    # Contrarian só funciona em horizonte de dias/semanas, não em entradas horárias.
    fg_value = (fear_greed or {}).get("value")
    if fg_value is not None:
        if fg_value <= 20:
            base["sinais"].append(f"🚨 Pânico F&G={fg_value} — mercado em capitulação")
            base["score"] -= 2
        elif fg_value <= 30:
            base["sinais"].append(f"😱 Medo Extremo F&G={fg_value} — cautela em entradas")
            base["score"] -= 1
        elif fg_value >= 80:
            base["sinais"].append(f"🤑 Ganância Extrema F&G={fg_value} (cautela)")
            base["score"] -= 2

    # ── BTC Gatekeeper: penaliza se BTC em queda nos últimos 6 candles
    # Queda leve (-1% a -2%): -1 no score, não bloqueia setup
    # Queda severa (< -2%):   -3 no score, bloqueia setup completo
    btc_risco = False
    btc_change_pct = None
    if btc_data and symbol != "BTCUSDT":
        btc_closes_all = btc_data.get("closes", [])
        btc_change_pct = btc_data.get("change_pct")
        if len(btc_closes_all) >= 6:
            btc_trend_6 = round(
                (btc_closes_all[-1] - btc_closes_all[-6]) / btc_closes_all[-6] * 100, 2
            )
        else:
            btc_trend_6 = btc_change_pct or 0
        if btc_trend_6 < -2.0:
            btc_risco = True   # queda severa: bloqueia setup
            base["sinais"].append(f"⚠️ BTC queda severa ({btc_trend_6}% / 6c)")
            base["score"] -= 3
            btc_change_pct = btc_trend_6
        elif btc_trend_6 < -1.0:
            # queda leve: apenas penaliza, não bloqueia setup
            base["sinais"].append(f"⚠️ BTC leve queda ({btc_trend_6}% / 6c)")
            base["score"] -= 1
            btc_change_pct = btc_trend_6

    # ── BTC EMA50 no 4H — filtro macro de tendência
    btc_4h_tendencia = (btc_data or {}).get("btc_4h_tendencia")
    if symbol != "BTCUSDT":
        if btc_4h_tendencia == "abaixo_ema50":
            base["sinais"].append("📉 BTC abaixo EMA50(4H) — tendência macro negativa")
            base["score"] -= 2
        elif btc_4h_tendencia == "acima_ema50":
            base["sinais"].append("📈 BTC acima EMA50(4H) — macro favorável")
            base["score"] += 1

    # ── Modo Sniper: força relativa durante queda do BTC
    # Quando BTC cai >1% e a moeda fica flat/positiva com volume → institucional comprando.
    # É o setup de maior probabilidade em crypto de curto prazo.
    sniper_mode = False
    if btc_data and symbol != "BTCUSDT":
        btc_closes_all = btc_data.get("closes", [])
        if len(btc_closes_all) >= 2:
            btc_ultimo_candle = round((btc_closes_all[-1] - btc_closes_all[-2]) / btc_closes_all[-2] * 100, 2)
            coin_ultimo_candle = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2)
            if btc_ultimo_candle < -1.0 and coin_ultimo_candle > -0.2 and ratio_vol >= 1.5:
                sniper_mode = True
                base["sinais"].append(
                    f"🎯 SNIPER: BTC {btc_ultimo_candle}% | "
                    f"{symbol.replace('USDT','')} {'+' if coin_ultimo_candle >= 0 else ''}"
                    f"{coin_ultimo_candle}% (força institucional!)"
                )
                base["score"] += 3

    # ── Ticker 24h
    ticker       = get_ticker_24h(symbol)
    variacao_24h = round(float(ticker.get("priceChangePercent", 0)), 2)

    def fmt(v):
        return round(v,6) if v<0.01 else round(v,4) if  v<1 else round(v,2)

    # Setup completo
    criterios_ativos = sum([
        divergencia == "bullish",
        market_structure == "bullish",
        forca_relativa is not None and forca_relativa > 1.5,
        volume_confirmado or vol_zscore > 2.0,   # volume acima da média ou anômalo
        er > 0.4,
        preco_acima_vwap is True,
        rsi_recovery,
        ob_imbalance is not None and ob_imbalance > 0.30,
        mtf_tendencia == "alta",
        bb_sinal in ("squeeze", "breakout_sup"),
    ])
    # Circuit breaker macro: BTC -5% em 24h bloqueia todos os setups
    macro_bloqueado = (btc_data or {}).get("macro_bloqueado", False)
    if macro_bloqueado and symbol != "BTCUSDT":
        base["sinais"].append("🚨 CIRCUIT BREAKER: BTC -5% em 24h")

    # ── Score mínimo dinâmico por contexto de mercado ─────────────────────
    # Em mercados ruins exige sinal mais forte para entrar
    score_minimo_efetivo = cfg["score_minimo_setup"]
    btc_dom = get_btc_dominance()
    if fg_value is not None and fg_value <= 25:
        score_minimo_efetivo += 2   # Medo extremo: exige score muito alto
    if btc_dom is not None and btc_dom > 55:
        score_minimo_efetivo += 1   # Alta dominância BTC: altcoins em desvantagem
    if btc_4h_tendencia == "abaixo_ema50" and symbol != "BTCUSDT":
        score_minimo_efetivo += 1   # BTC em tendência macro negativa: exige mais força

    # ── Volume bloqueio duro ───────────────────────────────────────────────
    # Se volume < 55% da média E não há anomalia estatística → bloqueia entrada
    volume_insuficiente = (ratio_vol < 0.55 and vol_zscore < 1.5)
    if volume_insuficiente:
        base["sinais"].append(f"🚫 Volume insuficiente ({int(ratio_vol*100)}%) — entrada bloqueada")

    setup_completo = (
        not macro_bloqueado and
        not volume_insuficiente and            # NOVO: volume mínimo obrigatório
        base["score"] >= score_minimo_efetivo and  # NOVO: threshold dinâmico
        criterios_ativos >= cfg["criterios_minimos_setup"] and
        base["rsi"] < cfg["rsi_maximo_setup"] and
        closes[-1] > base["ema50"] and
        not btc_risco and
        mtf_tendencia != "baixa" and
        setor_atual != "Meme" and
        tendencia_diaria != "baixa" and        # não comprar contra tendência 1D
        ema_distancia_pct <= 8.0               # não comprar esticado (>8% do EMA50)
    )
    quase_setup = (
        not setup_completo and
        base["score"] >= cfg["score_minimo_quase"] and
        criterios_ativos >= cfg["criterios_minimos_quase"] and
        base["rsi"] < cfg["rsi_maximo_quase"] and
        not btc_risco and
        mtf_tendencia != "baixa"
    )

    return {
        "symbol":           symbol,
        "preco":            fmt(closes[-1]),
        "variacao_24h":     variacao_24h,
        "rsi":              base["rsi"],
        "ema9":             base["ema9"],
        "ema21":            base["ema21"],
        "ema50":            base["ema50"],
        "vol_ratio":        base["vol_ratio"],
        "sinais":           base["sinais"],
        "score":            base["score"],
        "divergencia":      divergencia,
        "funding_rate":     fr,
        "funding_sinal":    funding_sinal,
        "mtf_score":        mtf_score,
        "mtf_tendencia":    mtf_tendencia,
        "mtf_tf":           tf_sup,
        "setup_completo":   setup_completo,
        "intervalo":        interval,
        "market_structure": market_structure,
        "cvd_pct":          cvd_pct,
        "cvd_sinal":        cvd_sinal,
        "oi_change":        oi_change,
        "oi_sinal":         oi_sinal,
        "forca_relativa":   forca_relativa,
        "btc_risco":        btc_risco,
        "btc_change_pct":   btc_change_pct,
        "er":               er,
        "vol_zscore":       vol_zscore,
        "vwap":             fmt(vwap) if vwap else None,
        "preco_acima_vwap": preco_acima_vwap,
        "atr_expansao":     atr_expansao,
        "rsi_recovery":     rsi_recovery,
        "padrao_vela":      padrao_vela,
        "ema_spread_pct":   base.get("ema_spread_pct", 0),
        "shadow_rsi":       shadow_rsi_val,
        "ob_imbalance":     ob_imbalance,
        "setor":            _SETOR_MAP.get(symbol, "Other"),
        "bb_sinal":              bb_sinal,
        "quase_setup":           quase_setup,
        "criterios_ativos":      criterios_ativos,
        "win_rate_historico":    wr_moeda.get("win_rate") if wr_moeda else None,
        "tendencia_diaria":      tendencia_diaria,
        "ema_distancia_pct":     ema_distancia_pct,
        "volume_confirmado":     volume_confirmado,
        "sniper_mode":           sniper_mode,
        "ls_ratio":              ls_ratio,
        "ls_sinal":              ls_sinal,
        "btc_4h_tendencia":      btc_4h_tendencia,
    }


def analisar_risco_posicao(symbol: str, preco_entrada: float, interval: str = "1h") -> dict:
    """
    Analisa sinais BEARISH numa posição aberta.
    Retorna score de risco (quanto maior, mais perigoso) e lista de alertas.
    Score >= 3 = aviso amarelo | >= 5 = aviso vermelho urgente.
    """
    alertas = []
    score_risco = 0

    klines = get_klines(symbol, interval=interval, limit=100)
    if not klines or len(klines) < 30:
        return {"score_risco": 0, "alertas": [], "urgente": False}

    opens   = [float(k[1]) for k in klines]
    closes  = [float(k[4]) for k in klines]
    highs   = [float(k[2]) for k in klines]
    lows    = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    preco_atual = closes[-1]
    ganho_atual = round((preco_atual - preco_entrada) / preco_entrada * 100, 2)

    # 1 — RSI sobrecomprado
    rsi = calcular_rsi(closes)
    if rsi >= 72:
        alertas.append(f"🔴 RSI {rsi} — sobrecomprado extremo")
        score_risco += 3
    elif rsi >= 68:
        alertas.append(f"🟡 RSI {rsi} — sobrecomprado")
        score_risco += 2

    # 2 — Divergência bearish (preço subindo, RSI caindo)
    rsi_serie = calcular_rsi_serie(closes)
    div = detectar_divergencia(closes, rsi_serie)
    if div == "bearish":
        alertas.append("🔴 Divergência bearish — preço subindo mas RSI caindo")
        score_risco += 3

    # 3 — EMA9 cruzando abaixo da EMA21 (reversão de curto prazo)
    ema9  = calcular_ema(closes, 9)
    ema21 = calcular_ema(closes, 21)
    ema50 = calcular_ema(closes, 50)[-1]
    if ema9[-1] < ema21[-1] and ema9[-2] >= ema21[-2]:
        alertas.append("🔴 EMA9 cruzou abaixo da EMA21 — reversão de tendência")
        score_risco += 3
    elif ema9[-1] < ema21[-1]:
        alertas.append("🟡 EMA9 abaixo da EMA21 — tendência enfraquecendo")
        score_risco += 1

    # 4 — Preço perdendo EMA50    # 4 — Preço perdendo EMA50
    if closes[-1] < ema50:
        alertas.append("🔴 Preço abaixo da EMA50 — suporte perdido")
        score_risco += 2

    # 5 — Volume crescente em queda de preço (distribuição)
    if closes[-1] < closes[-2] and volumes[-1] > volumes[-2] * 1.3:
        alertas.append("🔴 Queda com volume alto — distribuição")
        score_risco += 2

    # 6 — Shadow RSI bearish
    shadow_rsi_val = calcular_shadow_rsi(highs, lows)
    if shadow_rsi_val >= 72:
        alertas.append(f"🟡 Shadow RSI {shadow_rsi_val} — sobrecomprado")
        score_risco += 1

    # 7 — Order Book: vendedores dominando
    ob = get_order_book_imbalance(symbol)
    if ob is not None and ob < -0.30:
        alertas.append(f"🔴 OB {ob:+.2f} — vendedores dominando")
        score_risco += 2

    # 8 — Ganho já > 8%: proteção de lucro
    if ganho_atual >= 8:
        alertas.append(f"🟡 Ganho atual {ganho_atual}% — considere realizar")
        score_risco += 1

    # 9 — BB: preço na banda superior (sobrecomprado técnico)
    if len(closes) >= 20:
        bb_closes = closes[-20:]
        bb_media = sum(bb_closes) / 20
        bb_std   = (sum((x - bb_media) ** 2 for x in bb_closes) / 20) ** 0.5
        bb_sup   = bb_media + 2 * bb_std
        if closes[-1] >= bb_sup * 0.99:
            alertas.append("🟡 Preço na banda superior BB — sobrecomprado")
            score_risco += 1

    # 10 — Padrão de reversão bearish (doji no topo, engulfing bearish)
    padrao = detectar_padrao_vela(opens, highs, lows, closes)
    if padrao == "doji" and ganho_atual > 3:
        alertas.append("🟡 Doji no topo — indecisão/reversão possível")
        score_risco += 1

    # 11 — BTC forte queda (correlação sistêmica)
    btc_klines = get_klines("BTCUSDT", interval=interval, limit=10)
    if btc_klines and len(btc_klines) >= 6:
        btc_c = [float(k[4]) for k in btc_klines]
        btc_trend = round((btc_c[-1] - btc_c[-6]) / btc_c[-6] * 100, 2)
        if btc_trend < -2.0:
            alertas.append(f"🔴 BTC queda {btc_trend}% — risco sistêmico")
            score_risco += 2

    return {
        "score_risco":  score_risco,
        "alertas":      alertas,
        "urgente":      score_risco >= 5,
        "ganho_atual":  ganho_atual,
        "preco_atual":  round(closes[-1], 6),
    }


# ─── RODAR SCREENER ──────────────────────────────────────────────────────────

def rodar_screener(interval: str = "1h", top_n: int = 100) -> list:
    """
    Roda o screener em todos os TOP_PAIRS em paralelo.
    Retorna lista de resultados ordenados por score (desc).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    funding_rates = get_all_funding_rates()
    fear_greed    = get_fear_greed()

    btc_klines = get_klines("BTCUSDT", interval=interval, limit=100)
    btc_data = None
    if btc_klines and len(btc_klines) >= 60:
        btc_closes = [float(k[4]) for k in btc_klines]
        btc_change = round((btc_closes[-1] - btc_closes[-24]) / btc_closes[-24] * 100, 2)
        macro_bloqueado = btc_change <= -5.0
        btc_data = {
            "closes":            btc_closes,
            "change_pct":        btc_change,
            "macro_bloqueado":   macro_bloqueado,
            "btc_4h_tendencia":  get_btc_4h_tendencia(),
        }

    pares = TOP_PAIRS[:top_n]
    resultados = []

    def _analisar(symbol):
        try:
            return analisar_par(
                symbol,
                interval=interval,
                funding_rates=funding_rates,
                btc_data=btc_data,
                fear_greed=fear_greed,
            )
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_analisar, s): s for s in pares}
        for f in as_completed(futures):
            r = f.result()
            if r is not None:
                resultados.append(r)

    resultados.sort(key=lambda x: x["score"], reverse=True)

    # Cascade: detecta quando vários do mesmo setor aparecem juntos
    setor_count = {}
    for r in resultados:
        s = r.get("setor", "Other")
        setor_count[s] = setor_count.get(s, 0) + 1

    for r in resultados:
        s = r.get("setor", "Other")
        cnt = setor_count.get(s, 0)
        if cnt >= 3 and r.get("setup_completo"):
            r["sinais"].append(f"Cascade {s} ({cnt} moedas)")
            r["setor_cascade"] = cnt

    return resultados
