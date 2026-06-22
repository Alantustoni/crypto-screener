"""
Historico de Sinais — backend SQLite (substitui signal_history.json).
Interface pública idêntica: carregar_historico() -> list[dict],
salvar_historico(list[dict]) -> None.
SQLite garante escrita atomica: sem truncacao por sync do OneDrive.
DB fica em AppData/Local (fora do OneDrive).
"""
import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime
from screener import get_ticker_24h, get_fear_greed, get_klines


# ── Diretórios ──────────────────────────────────────────────────────────────
# DB em AppData\Local (fora do OneDrive -> sem interferencia de sync)
def _get_db_dir() -> Path:
    if os.environ.get("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    local_app = os.environ.get("LOCALAPPDATA")   # C:\Users\...\AppData\Local
    if local_app:
        p = Path(local_app) / "crypto-screener"
        p.mkdir(parents=True, exist_ok=True)
        return p
    return Path(__file__).parent  # fallback (Linux/Railway)

_DATA_DIR   = Path(__file__).parent            # pasta do projeto (configs, JSONs)
_DB_DIR     = _get_db_dir()                    # AppData\Local (sem sync OneDrive)
DB_PATH     = _DB_DIR  / "signal_history.db"
LEGACY_JSON = _DATA_DIR / "signal_history.json"


# ── Constantes ───────────────────────────────────────────────────────────────
SCORE_MINIMO      = 8
COOLDOWN_WIN_MIN  = 240    # 4h se ultima entrada na moeda foi WIN
COOLDOWN_LOSS_MIN = 480    # 8h se ultima entrada na moeda foi LOSS

CIRCUIT_BREAKER_N     = 3
CIRCUIT_BREAKER_HORAS = 3

REGIME_SCORE_MINIMO = 10
REGIME_BTC_CANDLES  = 6
REGIME_BTC_QUEDA    = -1.5
REGIME_FG_MAXIMO    = 25

BREAKEVEN_TRIGGER_PCT = 2.0
TRAILING_TP1_PCT      = 2.0

CHECKPOINTS = [
    (60,   "resultado_1h"),
    (120,  "resultado_2h"),
    (240,  "resultado_4h"),
    (480,  "resultado_8h"),
    (1440, "resultado_24h"),
]
STOP_LOSS_PCT = -3.0
TAKE_PROFIT_1 =  5.0
TAKE_PROFIT_2 = 10.0


# ════════════════════════════════════════════════════════════════════════════
#  CAMADA DE PERSISTÊNCIA — SQLite
# ════════════════════════════════════════════════════════════════════════════

def _conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _criar_tabela():
    with _conectar() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sinais (
                id             INTEGER PRIMARY KEY,
                timestamp      TEXT    NOT NULL,
                symbol         TEXT    NOT NULL,
                setup_completo INTEGER NOT NULL DEFAULT 0,
                dados          TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol    ON sinais(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON sinais(timestamp)")


def _migrar_json():
    """Importa signal_history.json para o SQLite na primeira execucao."""
    if not LEGACY_JSON.exists():
        return
    try:
        raw = LEGACY_JSON.read_text(encoding="utf-8", errors="replace")
        registros = json.loads(raw)
        if not isinstance(registros, list) or not registros:
            return
    except Exception:
        return

    with _conectar() as conn:
        count = conn.execute("SELECT COUNT(*) FROM sinais").fetchone()[0]
        if count > 0:
            LEGACY_JSON.rename(LEGACY_JSON.with_suffix(".json.bak"))
            return
        conn.executemany(
            "INSERT OR REPLACE INTO sinais (id, timestamp, symbol, setup_completo, dados) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    r.get("id", i + 1),
                    r.get("timestamp", ""),
                    r.get("symbol", ""),
                    int(r.get("setup_completo", False)),
                    json.dumps(r, ensure_ascii=False),
                )
                for i, r in enumerate(registros)
            ],
        )
    LEGACY_JSON.rename(LEGACY_JSON.with_suffix(".json.bak"))


# Inicializacao na carga do modulo
_criar_tabela()
_migrar_json()


def carregar_historico() -> list:
    with _conectar() as conn:
        rows = conn.execute(
            "SELECT dados FROM sinais ORDER BY timestamp ASC"
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def salvar_historico(historico: list):
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    with _conectar() as conn:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("DELETE FROM sinais")
        conn.executemany(
            "INSERT INTO sinais (id, timestamp, symbol, setup_completo, dados) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    h.get("id"),
                    h.get("timestamp", ""),
                    h.get("symbol", ""),
                    int(h.get("setup_completo", False)),
                    json.dumps(h, ensure_ascii=False),
                )
                for h in historico
            ],
        )
        conn.execute("COMMIT")


# ════════════════════════════════════════════════════════════════════════════
#  REGISTRO MANUAL
# ════════════════════════════════════════════════════════════════════════════

def registrar_manual(symbol: str, preco_entrada: float, nota: str = "") -> dict:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    historico = carregar_historico()
    agora = datetime.now()
    entrada = {
        "id": len(historico) + 1,
        "timestamp": agora.isoformat(timespec="seconds"),
        "symbol": symbol,
        "preco_entrada": preco_entrada,
        "score": 0, "rsi": None, "divergencia": None, "funding_sinal": None,
        "mtf_tendencia": None, "setup_completo": False, "intervalo": "manual",
        "sinais": [], "market_structure": None, "forca_relativa": None, "er": None,
        "rsi_recovery": False, "padrao_vela": None, "rotation_rank": None,
        "criterios_ativos": 0, "setor": None, "setor_cascade": None,
        "shadow_rsi": None, "ob_imbalance": None, "tipo": "manual", "nota": nota,
        "resultado_1h": None, "resultado_2h": None, "resultado_4h": None,
        "resultado_8h": None, "resultado_16h": None, "resultado_24h": None,
        "verificado_em": None,
    }
    historico.append(entrada)
    salvar_historico(historico)
    return entrada


# ════════════════════════════════════════════════════════════════════════════
#  HELPERS DE QUALIFICAÇÃO
# ════════════════════════════════════════════════════════════════════════════

def _entrada_fechada(h: dict) -> bool:
    return bool(h.get("stop_automatico")) or h.get("resultado_8h") is not None


def _entrada_foi_loss(h: dict) -> bool:
    if h.get("tp2_automatico") or h.get("tp1_automatico"):
        return False
    if h.get("stop_automatico"):
        return h.get("stop_automatico_pct", -3.0) < 0
    r8h = h.get("resultado_8h")
    return r8h is not None and r8h < 0


def _circuit_breaker_sequencial_ativo(historico: list, agora: datetime) -> bool:
    fechadas = [
        h for h in historico
        if h.get("setup_completo") and _entrada_fechada(h) and h.get("tipo") != "manual"
    ]
    if len(fechadas) < CIRCUIT_BREAKER_N:
        return False
    ultimas = sorted(fechadas, key=lambda x: x["timestamp"])[-CIRCUIT_BREAKER_N:]
    if not all(_entrada_foi_loss(h) for h in ultimas):
        return False
    ultima_ts = datetime.fromisoformat(ultimas[-1]["timestamp"])
    return (agora - ultima_ts).total_seconds() < CIRCUIT_BREAKER_HORAS * 3600


def _regime_bearish() -> bool:
    try:
        fg = get_fear_greed()
        if (fg or {}).get("value", 50) > REGIME_FG_MAXIMO:
            return False
        klines = get_klines("BTCUSDT", interval="1h", limit=REGIME_BTC_CANDLES + 2)
        if not klines or len(klines) < REGIME_BTC_CANDLES:
            return False
        closes = [float(k[4]) for k in klines]
        btc_change = (closes[-1] - closes[-REGIME_BTC_CANDLES]) / closes[-REGIME_BTC_CANDLES] * 100
        return btc_change <= REGIME_BTC_QUEDA
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
#  REGISTRO DE SINAIS
# ════════════════════════════════════════════════════════════════════════════

def registrar_sinais(resultados: list, score_minimo: int = SCORE_MINIMO) -> int:
    historico = carregar_historico()
    agora     = datetime.now()
    novos     = 0

    if _circuit_breaker_sequencial_ativo(historico, agora):
        return 0

    score_minimo_efetivo = score_minimo
    if _regime_bearish():
        score_minimo_efetivo = max(score_minimo, REGIME_SCORE_MINIMO)

    for r in resultados:
        if not r.get("setup_completo"):
            continue
        if r.get("score", 0) < score_minimo_efetivo:
            continue

        symbol = r["symbol"]

        entradas_moeda = sorted(
            [h for h in historico[-200:] if h["symbol"] == symbol and h.get("setup_completo")],
            key=lambda x: x["timestamp"],
        )
        if entradas_moeda:
            ultima   = entradas_moeda[-1]
            diff_min = (agora - datetime.fromisoformat(ultima["timestamp"])).total_seconds() / 60
            cooldown = (COOLDOWN_LOSS_MIN if (_entrada_fechada(ultima) and _entrada_foi_loss(ultima))
                        else COOLDOWN_WIN_MIN)
            if diff_min < cooldown:
                continue

        posicao_aberta = any(
            h["symbol"] == symbol and h.get("setup_completo")
            and not _entrada_fechada(h) and h.get("tipo") != "manual"
            for h in historico[-200:]
        )
        if posicao_aberta:
            continue

        historico.append({
            "id": len(historico) + 1,
            "timestamp": agora.isoformat(timespec="seconds"),
            "symbol": symbol,
            "preco_entrada": r["preco"],
            "score": r["score"],
            "rsi": r["rsi"],
            "divergencia": r.get("divergencia"),
            "funding_sinal": r.get("funding_sinal"),
            "mtf_tendencia": r.get("mtf_tendencia"),
            "setup_completo": r.get("setup_completo", False),
            "intervalo": r.get("intervalo", "1h"),
            "sinais": r.get("sinais", []),
            "market_structure": r.get("market_structure"),
            "forca_relativa": r.get("forca_relativa"),
            "er": r.get("er"),
            "rsi_recovery": r.get("rsi_recovery", False),
            "padrao_vela": r.get("padrao_vela"),
            "rotation_rank": r.get("rotation_rank"),
            "criterios_ativos": r.get("criterios_ativos", 0),
            "setor": r.get("setor"),
            "setor_cascade": r.get("setor_cascade"),
            "shadow_rsi": r.get("shadow_rsi"),
            "ob_imbalance": r.get("ob_imbalance"),
            "resultado_1h": None, "resultado_2h": None, "resultado_4h": None,
            "resultado_8h": None, "resultado_16h": None, "resultado_24h": None,
            "verificado_em": None,
        })
        novos += 1

    if novos:
        salvar_historico(historico)
    return novos


# ════════════════════════════════════════════════════════════════════════════
#  VERIFICAÇÃO DE CHECKPOINTS
# ════════════════════════════════════════════════════════════════════════════

def verificar_resultados() -> dict:
    historico        = carregar_historico()
    agora            = datetime.now()
    atualizados      = 0
    novos_resultados = []

    for sinal in historico:
        ts       = datetime.fromisoformat(sinal["timestamp"])
        diff_min = (agora - ts).total_seconds() / 60
        pendentes = [
            (mins, campo) for mins, campo in CHECKPOINTS
            if diff_min >= mins and sinal.get(campo) is None
        ]
        if not pendentes:
            continue

        ticker = get_ticker_24h(sinal["symbol"])
        preco_atual = float(ticker.get("lastPrice", 0))
        if preco_atual == 0:
            continue

        entrada = float(sinal["preco_entrada"])
        pct     = round((preco_atual - entrada) / entrada * 100, 2)

        for mins, campo in pendentes:
            sinal[campo] = pct
            alerta = None
            if pct <= STOP_LOSS_PCT:
                alerta = "stop_loss"
            elif pct >= TAKE_PROFIT_2:
                alerta = "tp2"
            elif pct >= TAKE_PROFIT_1:
                alerta = "tp1"
            novos_resultados.append((sinal, campo.replace("resultado_", "+"), pct, preco_atual, alerta))

        sinal["verificado_em"] = agora.isoformat(timespec="seconds")
        atualizados += 1

    if atualizados:
        salvar_historico(historico)

    return {"atualizados": atualizados, "historico": historico, "novos_resultados": novos_resultados}


# ════════════════════════════════════════════════════════════════════════════
#  MONITORAMENTO DE STOPS / TPs / TRAILING
# ════════════════════════════════════════════════════════════════════════════

def monitorar_stops_ativos(stop_pct: float = STOP_LOSS_PCT,
                           tp1_pct:  float = TAKE_PROFIT_1,
                           tp2_pct:  float = TAKE_PROFIT_2) -> list:
    historico  = carregar_historico()
    agora      = datetime.now()
    disparos   = []
    modificado = False

    abertas = [
        h for h in historico
        if h.get("setup_completo") and h.get("resultado_8h") is None
        and not h.get("stop_automatico") and h.get("tipo") != "manual"
    ]
    if not abertas:
        return disparos

    def _preencher_checkpoint(sinal, pct):
        ts       = datetime.fromisoformat(sinal["timestamp"])
        diff_min = (agora - ts).total_seconds() / 60
        for mins, campo in CHECKPOINTS:
            if diff_min >= mins * 0.5 and sinal.get(campo) is None:
                sinal[campo] = pct
                return campo
        return None

    def _fechar_stop(sinal, pct, preco, motivo=""):
        sinal.update({
            "stop_automatico": True, "stop_automatico_pct": pct,
            "stop_automatico_preco": preco,
            "stop_automatico_ts": agora.isoformat(timespec="seconds"),
        })
        if motivo:
            sinal["stop_automatico_motivo"] = motivo
        _preencher_checkpoint(sinal, pct)
        disparos.append((sinal, pct, preco, "stop_automatico"))

    def _fechar_tp2(sinal, pct, preco, motivo=""):
        sinal.update({
            "tp2_automatico": True, "tp2_automatico_pct": pct,
            "tp2_automatico_preco": preco,
            "tp2_automatico_ts": agora.isoformat(timespec="seconds"),
        })
        if motivo:
            sinal["tp2_automatico_motivo"] = motivo
        if sinal.get("resultado_8h") is None:
            sinal["resultado_8h"] = pct
        disparos.append((sinal, pct, preco, "tp2_automatico"))

    for sinal in abertas:
        try:
            ticker      = get_ticker_24h(sinal["symbol"])
            preco_atual = float(ticker.get("lastPrice", 0))
            if preco_atual == 0:
                continue

            entrada = float(sinal["preco_entrada"])
            pct     = round((preco_atual - entrada) / entrada * 100, 2)

            if pct <= stop_pct:
                _fechar_stop(sinal, pct, preco_atual)

            elif sinal.get("stop_breakeven_ativo") and pct <= 0:
                _fechar_stop(sinal, pct, preco_atual, motivo="breakeven")

            elif sinal.get("tp1_automatico") and not sinal.get("tp2_automatico"):
                pico = max(sinal.get("pico_pos_tp1", tp1_pct), pct)
                if pico != sinal.get("pico_pos_tp1"):
                    sinal["pico_pos_tp1"] = pico
                    modificado = True
                if pct >= tp2_pct:
                    _fechar_tp2(sinal, pct, preco_atual)
                elif (pico - pct) >= TRAILING_TP1_PCT:
                    _fechar_tp2(sinal, pct, preco_atual, motivo="trailing_tp1")

            elif pct >= tp1_pct and not sinal.get("tp1_automatico"):
                sinal.update({
                    "tp1_automatico": True, "tp1_automatico_pct": pct,
                    "tp1_automatico_preco": preco_atual,
                    "tp1_automatico_ts": agora.isoformat(timespec="seconds"),
                    "pico_pos_tp1": pct,
                })
                _preencher_checkpoint(sinal, pct)
                disparos.append((sinal, pct, preco_atual, "tp1_automatico"))

            # Ativa breakeven em +2% (antes do TP1)
            if not sinal.get("tp1_automatico") and pct >= BREAKEVEN_TRIGGER_PCT:
                if not sinal.get("stop_breakeven_ativo"):
                    sinal["stop_breakeven_ativo"] = True
                    modificado = True

        except Exception:
            continue

    if disparos or modificado:
        salvar_historico(historico)
    return disparos


# ════════════════════════════════════════════════════════════════════════════
#  ESTATÍSTICAS E EV
# ════════════════════════════════════════════════════════════════════════════

def _calcular_pl_sinal(h: dict, pos: float = 20.0):
    SL, TP1, TP2 = 3.0, 5.0, 10.0
    if h.get("tp2_automatico"):
        return (h.get("tp1_automatico_pct", TP1) / 100 * pos * 0.5 +
                h.get("tp2_automatico_pct", TP2) / 100 * pos * 0.5)
    if h.get("stop_automatico"):
        return h.get("stop_automatico_pct", -SL) / 100 * pos
    if h.get("tp1_automatico"):
        tp1_pct = h.get("tp1_automatico_pct", TP1)
        vals = [h.get("resultado_{}".format(t)) for t in ("1h", "2h", "4h", "8h", "24h")]
        vals = [v for v in vals if v is not None]
        last = vals[-1] if vals else tp1_pct
        return tp1_pct / 100 * pos * 0.5 + last / 100 * pos * 0.5
    vals = [h.get("resultado_{}".format(t)) for t in ("1h", "2h", "4h", "8h", "24h")]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    max_v, last_v = max(vals), vals[-1]
    if any(v <= -SL for v in vals):
        return -SL / 100 * pos
    if max_v >= TP2:
        return TP2 / 100 * pos
    if max_v >= TP1:
        return TP1 / 100 * pos * 0.5 + last_v / 100 * pos * 0.5
    return last_v / 100 * pos


def _taxa(sinais: list, campo: str) -> dict:
    vals = [h[campo] for h in sinais if h.get(campo) is not None]
    if not vals:
        return {"total": 0, "acertos": 0, "taxa_pct": None, "media_pct": None}
    acertos = sum(1 for v in vals if v > 0)
    return {
        "total":     len(vals),
        "acertos":   acertos,
        "taxa_pct":  round(acertos / len(vals) * 100, 1),
        "media_pct": round(sum(vals) / len(vals), 2),
    }


def calcular_ev(historico: list, pos: float = 20.0) -> dict:
    """
    EV = (WR x ganho_medio) - (LR x perda_media)
    So usa entradas com resultado fechado.
    """
    fechadas = [h for h in historico if h.get("setup_completo") and _entrada_fechada(h)]
    pls      = [_calcular_pl_sinal(h, pos) for h in fechadas]
    pls      = [v for v in pls if v is not None]

    if not pls:
        return {"n": 0, "ev_medio": None, "profit_factor": None,
                "win_rate": None, "ganho_medio": None, "perda_media": None}

    wins   = [v for v in pls if v > 0]
    losses = [v for v in pls if v <= 0]
    return {
        "n":             len(pls),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / len(pls) * 100, 1),
        "ev_medio":      round(sum(pls) / len(pls), 2),
        "ganho_medio":   round(sum(wins) / len(wins), 2)   if wins   else 0,
        "perda_media":   round(sum(losses) / len(losses), 2) if losses else 0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
                         if losses and sum(losses) != 0 else None,
        "pl_total":      round(sum(pls), 2),
    }


def calcular_stats(historico: list) -> dict:
    setups = [h for h in historico if h.get("setup_completo")]
    return {
        "total_sinais": len(historico),
        "ev":           calcular_ev(setups),
        "1h":           _taxa(historico, "resultado_1h"),
        "4h":           _taxa(historico, "resultado_4h"),
        "24h":          _taxa(historico, "resultado_24h"),
        "setup_completo": {
            "total": len(setups),
            "1h":    _taxa(setups, "resultado_1h"),
            "24h":   _taxa(setups, "resultado_24h"),
        },
    }


def obter_historico_com_stats() -> dict:
    historico     = carregar_historico()
    historico_ord = sorted(historico, key=lambda x: x["timestamp"], reverse=True)
    return {"stats": calcular_stats(historico), "historico": historico_ord}


# ════════════════════════════════════════════════════════════════════════════
#  CALIBRACAO
# ════════════════════════════════════════════════════════════════════════════

def calcular_calibracao(historico: list, campo: str = "resultado_1h") -> list:
    from collections import defaultdict
    contadores = defaultdict(lambda: {"apareceu": 0, "ganhou": 0, "soma_pct": 0.0})
    for h in historico:
        resultado = h.get(campo)
        if resultado is None:
            continue
        for txt in h.get("sinais", []):
            contadores[txt]["apareceu"] += 1
            contadores[txt]["soma_pct"] += resultado
            if resultado > 0:
                contadores[txt]["ganhou"] += 1
    result = []
    for txt, c in contadores.items():
        if c["apareceu"] < 3:
            continue
        result.append({
            "sinal":         txt,
            "amostras":      c["apareceu"],
            "ganhou":        c["ganhou"],
            "taxa_pct":      round(c["ganhou"] / c["apareceu"] * 100, 1),
            "media_retorno": round(c["soma_pct"] / c["apareceu"], 2),
        })
    return sorted(result, key=lambda x: x["taxa_pct"], reverse=True)
