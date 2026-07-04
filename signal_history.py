"""
Historico de Sinais — backend PostgreSQL (Railway) com fallback SQLite (local).
Interface pública idêntica: carregar_historico() -> list[dict],
salvar_historico(list[dict]) -> None.

Detecção automática:
  - DATABASE_URL definida → PostgreSQL (Railway, persistente entre deploys)
  - Sem DATABASE_URL       → SQLite local (desenvolvimento)
"""
import functools
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from datetime import datetime
from screener import get_ticker_24h, get_fear_greed, get_klines
from trading_math import pl_simulado as _pl_simulado_canonico, niveis_saida

log = logging.getLogger("crypto_screener")

# ── Concorrência ──────────────────────────────────────────────────────────────
# Lock global de escrita. Os endpoints do FastAPI rodam em threadpool e os loops
# de background no event loop; sem serialização, dois "carrega lista → muta →
# salva" concorrentes sobrescrevem um ao outro (lost update). Reentrante (RLock)
# para tolerar chamadas aninhadas. Exposto como write_lock para uso externo
# (ex.: loop_protecao_risco em main.py).
write_lock = threading.RLock()
_write_lock = write_lock  # alias interno


def _com_lock(fn):
    """Serializa a função inteira sob o write_lock (carrega→muta→salva atômico)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _write_lock:
            return fn(*args, **kwargs)
    return wrapper


def _proximo_id(historico: list) -> int:
    """ID monotônico baseado no maior id existente (evita colisão que o antigo
    len(historico)+1 causava quando entradas eram removidas)."""
    return max((int(h.get("id") or 0) for h in historico), default=0) + 1


# ── Configuração de ambiente ──────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")  # Railway define automaticamente


def _usar_postgres() -> bool:
    return bool(DATABASE_URL)


# ── Diretórios (SQLite local) ─────────────────────────────────────────────────
def _get_db_dir() -> Path:
    if os.environ.get("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        p = Path(local_app) / "crypto-screener"
        p.mkdir(parents=True, exist_ok=True)
        return p
    return Path(__file__).parent

_DATA_DIR   = Path(__file__).parent
_DB_DIR     = _get_db_dir()
DB_PATH     = _DB_DIR / "signal_history.db"
LEGACY_JSON = _DATA_DIR / "signal_history.json"


# ── Constantes ────────────────────────────────────────────────────────────────
SCORE_MINIMO      = 8
COOLDOWN_WIN_MIN  = 240
COOLDOWN_LOSS_MIN = 480

CIRCUIT_BREAKER_N     = 3
CIRCUIT_BREAKER_HORAS = 3

REGIME_SCORE_MINIMO = 5   # (agressivo) alinhado ao score base — antes 10, depois 7
REGIME_BTC_CANDLES  = 6
REGIME_BTC_QUEDA    = -1.5
REGIME_FG_MAXIMO    = 25

BREAKEVEN_TRIGGER_PCT = 1.5
TRAILING_TP1_PCT      = 2.0

CHECKPOINTS = [
    (60,   "resultado_1h"),
    (120,  "resultado_2h"),
    (240,  "resultado_4h"),
    (480,  "resultado_8h"),
    (1440, "resultado_24h"),
]
STOP_LOSS_PCT = -3.0
TAKE_PROFIT_1 =  3.0
TAKE_PROFIT_2 =  6.0


# ════════════════════════════════════════════════════════════════════════════
#  CAMADA PostgreSQL
# ════════════════════════════════════════════════════════════════════════════

def _pg_conectar():
    import psycopg2
    url = DATABASE_URL
    # Railway usa postgres:// mas psycopg2 precisa de postgresql://
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def _pg_criar_tabela():
    conn = _pg_conectar()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sinais (
                id             INTEGER PRIMARY KEY,
                timestamp      TEXT    NOT NULL,
                symbol         TEXT    NOT NULL,
                setup_completo INTEGER NOT NULL DEFAULT 0,
                dados          TEXT    NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_symbol    ON sinais(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON sinais(timestamp)")
        # Tabela de configuração persistente (sobrevive a redeploys)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS config (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def carregar_config_db() -> dict | None:
    """Carrega screener_config do PostgreSQL. Retorna None se não disponível."""
    if not _usar_postgres():
        return None
    try:
        conn = _pg_conectar()
        cur = conn.cursor()
        cur.execute("SELECT valor FROM config WHERE chave = 'screener_config'")
        row = cur.fetchone()
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception:
        return None


def salvar_config_db(cfg: dict) -> bool:
    """Salva screener_config no PostgreSQL. Retorna True se ok."""
    if not _usar_postgres():
        return False
    try:
        conn = _pg_conectar()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO config (chave, valor) VALUES ('screener_config', %s)
            ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
        """, (json.dumps(cfg, ensure_ascii=False),))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def _pg_carregar() -> list:
    conn = _pg_conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT dados FROM sinais ORDER BY timestamp ASC")
        rows = cur.fetchall()
        return [json.loads(r[0]) for r in rows]
    finally:
        conn.close()


def _pg_salvar(historico: list):
    conn = _pg_conectar()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute("DELETE FROM sinais")
        for h in historico:
            cur.execute(
                "INSERT INTO sinais (id, timestamp, symbol, setup_completo, dados) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    h.get("id"),
                    h.get("timestamp", ""),
                    h.get("symbol", ""),
                    int(h.get("setup_completo", False)),
                    json.dumps(h, ensure_ascii=False),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
#  CAMADA SQLite (local / fallback)
# ════════════════════════════════════════════════════════════════════════════

def _sqlite_conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _sqlite_criar_tabela():
    with _sqlite_conectar() as conn:
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


def _sqlite_migrar_json():
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
    with _sqlite_conectar() as conn:
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


def _sqlite_carregar() -> list:
    with _sqlite_conectar() as conn:
        rows = conn.execute(
            "SELECT dados FROM sinais ORDER BY timestamp ASC"
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _sqlite_salvar(historico: list):
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    with _sqlite_conectar() as conn:
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
#  INTERFACE PÚBLICA — detecta automaticamente o backend
# ════════════════════════════════════════════════════════════════════════════

def _criar_tabela():
    if _usar_postgres():
        _pg_criar_tabela()
    else:
        _sqlite_criar_tabela()
        _sqlite_migrar_json()


def carregar_historico() -> list:
    if _usar_postgres():
        return _pg_carregar()
    return _sqlite_carregar()


def salvar_historico(historico: list):
    if _usar_postgres():
        _pg_salvar(historico)
    else:
        _sqlite_salvar(historico)


# Inicialização na carga do módulo — resiliente: falha de DB não impede o import
try:
    _criar_tabela()
except Exception:
    log.exception("Falha ao inicializar a tabela de sinais no import")


# ════════════════════════════════════════════════════════════════════════════
#  REGISTRO MANUAL
# ════════════════════════════════════════════════════════════════════════════

@_com_lock
def registrar_manual(symbol: str, preco_entrada: float, nota: str = "") -> dict:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    historico = carregar_historico()
    agora = datetime.now()
    entrada = {
        "id": _proximo_id(historico),
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

@_com_lock
def registrar_sinais(resultados: list, score_minimo: int = SCORE_MINIMO) -> list:
    """Registra os setups e RETORNA a lista das entradas novas (para o alerta
    ser 1:1 com o histórico). Lista vazia = nada registrado."""
    historico = carregar_historico()
    agora     = datetime.now()
    novos     = []

    if _circuit_breaker_sequencial_ativo(historico, agora):
        return []

    score_minimo_efetivo = score_minimo
    if _regime_bearish():
        score_minimo_efetivo = max(score_minimo, REGIME_SCORE_MINIMO)

    # Snapshot macro congelado no momento do lote (mesmo p/ todos os sinais desta
    # varredura). Serve p/ a análise de edge medir se contexto de mercado importa.
    try:
        _fg_lote = (get_fear_greed() or {}).get("value")
    except Exception:
        _fg_lote = None

    for r in resultados:
        if not r.get("setup_completo"):
            continue
        # Setups de queda (sniper / reversão) são qualificados por CONFLUÊNCIA
        # de sinais de fundo no screener, não por score — em downtrend o score
        # é naturalmente penalizado. Piso -2 apenas barra o pior ('faca caindo'
        # dá <= -3). Setups normais seguem o piso (base/regime).
        if r.get("sniper_setup") or r.get("reversao_setup"):
            piso = -2
        else:
            piso = score_minimo_efetivo
        if r.get("score", 0) < piso:
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

        entrada = {
            "id": _proximo_id(historico),
            "timestamp": agora.isoformat(timespec="seconds"),
            "symbol": symbol,
            "preco_entrada": r["preco"],
            "score": r["score"],
            "rsi": r["rsi"],
            "divergencia": r.get("divergencia"),
            "funding_sinal": r.get("funding_sinal"),
            "mtf_tendencia": r.get("mtf_tendencia"),
            "setup_completo": r.get("setup_completo", False),
            "sniper_setup": r.get("sniper_setup", False),
            "reversao_setup": r.get("reversao_setup", False),
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
            "atr_pct": r.get("atr_pct"),
            # ── Snapshot de features AO VIVO (congelado na entrada) ────────────
            # Só dá pra medir edge se guardarmos o que sabíamos no momento do
            # sinal. ob_imbalance e cvd são microestrutura (não reconstruíveis no
            # histórico); o resto contextualiza. A análise em /api/edge separa
            # trades por esses campos e vê quais preveem acerto de verdade.
            "ema_spread_pct":   r.get("ema_spread_pct"),
            "vol_zscore":       r.get("vol_zscore"),
            "cvd_sinal":        r.get("cvd_sinal"),
            "cvd_pct":          r.get("cvd_pct"),
            "preco_acima_vwap": r.get("preco_acima_vwap"),
            "bb_sinal":         r.get("bb_sinal"),
            "tendencia_diaria": r.get("tendencia_diaria"),
            "mtf_score":        r.get("mtf_score"),
            "fg_entrada":       _fg_lote,
            "resultado_1h": None, "resultado_2h": None, "resultado_4h": None,
            "resultado_8h": None, "resultado_16h": None, "resultado_24h": None,
            "verificado_em": None,
        }
        # Níveis de saída ajustados à volatilidade da moeda (ATR) — congelados
        # na entrada. Usados por monitorar_stops_ativos e pelo P&L simulado.
        entrada.update(niveis_saida(r.get("atr_pct")))
        historico.append(entrada)
        novos.append(entrada)

    if novos:
        salvar_historico(historico)
    return novos


# ════════════════════════════════════════════════════════════════════════════
#  VERIFICAÇÃO DE CHECKPOINTS
# ════════════════════════════════════════════════════════════════════════════

@_com_lock
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

@_com_lock
def monitorar_stops_ativos(stop_pct: float = STOP_LOSS_PCT,
                           tp1_pct:  float = TAKE_PROFIT_1,
                           tp2_pct:  float = TAKE_PROFIT_2) -> list:
    historico  = carregar_historico()
    agora      = datetime.now()
    disparos   = []
    modificado = False

    # Aberta = ainda não fechada de vez. Moonbag (moonbag_ativo) segue monitorado
    # mesmo depois do checkpoint de 8h, até fechar (trailing largo / 24h / breakeven).
    abertas = [
        h for h in historico
        if h.get("setup_completo") and h.get("tipo") != "manual"
        and not h.get("stop_automatico")
        and not h.get("tp2_automatico")
        and not h.get("moonbag_automatico")
        and (h.get("resultado_8h") is None or h.get("moonbag_ativo"))
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

    def _marcar_tp2(sinal, pct, preco):
        # Atingiu TP2: vende 30% e forma o moonbag de 20% (posição NÃO fecha).
        sinal.update({
            "tp2_automatico_pct": pct, "tp2_automatico_preco": preco,
            "tp2_automatico_ts": agora.isoformat(timespec="seconds"),
            "moonbag_ativo": True, "pico_moonbag": pct,
        })
        _preencher_checkpoint(sinal, pct)
        disparos.append((sinal, pct, preco, "tp2_parcial"))

    def _fechar_runner_full(sinal, pct, preco, motivo=""):
        # Runner (50%) saiu no trailing ANTES do TP2 → fecha 50/50.
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

    def _fechar_moonbag(sinal, pct, preco, motivo=""):
        # Fecha o moonbag de 20% → posição totalmente encerrada (50/30/20).
        sinal.update({
            "moonbag_automatico": True, "moonbag_automatico_pct": pct,
            "moonbag_automatico_preco": preco,
            "moonbag_automatico_ts": agora.isoformat(timespec="seconds"),
            "moonbag_ativo": False,
        })
        if motivo:
            sinal["moonbag_automatico_motivo"] = motivo
        if sinal.get("resultado_8h") is None:
            sinal["resultado_8h"] = pct
        disparos.append((sinal, pct, preco, "moonbag_automatico"))

    for sinal in abertas:
        try:
            ticker      = get_ticker_24h(sinal["symbol"])
            preco_atual = float(ticker.get("lastPrice", 0))
            if preco_atual == 0:
                continue

            entrada = float(sinal["preco_entrada"])
            pct     = round((preco_atual - entrada) / entrada * 100, 2)
            idade_h = (agora - datetime.fromisoformat(sinal["timestamp"])).total_seconds() / 3600

            # Níveis por-sinal (ATR, congelados na entrada) com fallback pros
            # parâmetros/constantes quando o sinal é antigo/manual sem exit_*.
            sl_eff    = -abs(sinal.get("exit_sl")    or abs(stop_pct))
            tp1_eff   =      sinal.get("exit_tp1")   or tp1_pct
            tp2_eff   =      sinal.get("exit_tp2")   or tp2_pct
            trail_eff =      sinal.get("exit_trail") or TRAILING_TP1_PCT
            be_eff    =      sinal.get("exit_be")    or BREAKEVEN_TRIGGER_PCT

            # ── MOONBAG (20%) correndo: trailing largo (1.5×) + 24h + breakeven ──
            if sinal.get("moonbag_ativo"):
                pico_mb = max(sinal.get("pico_moonbag", tp2_eff), pct)
                if pico_mb != sinal.get("pico_moonbag"):
                    sinal["pico_moonbag"] = pico_mb
                    modificado = True
                if idade_h >= 24:
                    _fechar_moonbag(sinal, pct, preco_atual, motivo="timeout_24h")
                elif pct <= 0:
                    # Mesma correção do BE: o moonbag corta na ENTRADA (~0%), não
                    # no preço overshoot visto minutos depois.
                    _fechar_moonbag(sinal, 0.0, entrada, motivo="breakeven")
                elif (pico_mb - pct) >= trail_eff * 1.5:
                    _fechar_moonbag(sinal, pct, preco_atual, motivo="trailing")
                continue

            # ── Stop total (antes de qualquer parcial) ──────────────────────
            if pct <= sl_eff:
                _fechar_stop(sinal, pct, preco_atual)

            elif sinal.get("stop_breakeven_ativo") and pct <= 0 and not sinal.get("tp1_automatico"):
                # BE é uma ordem parada na ENTRADA: preenche em ~0%, NÃO no preço
                # overshoot que o monitor só enxerga minutos depois. Antes, uma
                # queda rápida fazia o "breakeven" fechar em -2/-3% — prejuízo real
                # rotulado como breakeven, afundando taxa de acerto e P&L. Agora
                # registra 0% (taxa/slippage do pl_simulado cuidam do custo real).
                _fechar_stop(sinal, 0.0, entrada, motivo="breakeven")

            # ── Runner (50%) após TP1: TP2 forma moonbag; senão trailing 50/50 ──
            elif sinal.get("tp1_automatico"):
                pico = max(sinal.get("pico_pos_tp1", tp1_eff), pct)
                if pico != sinal.get("pico_pos_tp1"):
                    sinal["pico_pos_tp1"] = pico
                    modificado = True
                if idade_h >= 24:
                    _fechar_runner_full(sinal, pct, preco_atual, motivo="timeout_24h")
                elif pct >= tp2_eff:
                    _marcar_tp2(sinal, pct, preco_atual)          # vende 30%, forma moonbag
                elif (pico - pct) >= trail_eff:
                    _fechar_runner_full(sinal, pct, preco_atual, motivo="trailing_tp1")

            # ── Primeira vez atingindo TP1 → vende 50% ──────────────────────
            elif pct >= tp1_eff:
                sinal.update({
                    "tp1_automatico": True, "tp1_automatico_pct": pct,
                    "tp1_automatico_preco": preco_atual,
                    "tp1_automatico_ts": agora.isoformat(timespec="seconds"),
                    "pico_pos_tp1": pct,
                })
                _preencher_checkpoint(sinal, pct)
                disparos.append((sinal, pct, preco_atual, "tp1_automatico"))

            # Ativa breakeven ao atingir o gatilho por-ATR (só antes do TP1)
            if not sinal.get("tp1_automatico") and pct >= be_eff:
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
    # Delega para a fonte única (trading_math). Antes usava TP1=5/TP2=10,
    # divergente do resto do sistema (3/6) — bug corrigido na auditoria.
    return _pl_simulado_canonico(h, pos)


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
