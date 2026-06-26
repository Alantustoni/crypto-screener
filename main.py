import asyncio
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from screener import rodar_screener, INTERVALOS, TOP_PAIRS, get_ticker_24h, get_fear_greed, get_btc_dominance, get_btc_4h_tendencia, analisar_risco_posicao, carregar_config_screener
from backtesting import backtest
from telegram_alert import checar_e_alertar, testar_conexao, carregar_config, salvar_config, enviar_relatorio_diario, enviar_resumo_posicoes, enviar_fechamento, processar_comandos
from signal_history import registrar_sinais, registrar_manual, verificar_resultados, obter_historico_com_stats, carregar_historico, calcular_calibracao, monitorar_stops_ativos

# ─── AUTH ───────────────────────────────────────────────────────────────────
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
INTERNAL_API_KEY   = os.environ.get("INTERNAL_API_KEY", "")
_valid_tokens: set = set()

_PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/tela", "/api/tela/clear"}
_PUBLIC_PREFIXES = ("/static/",)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not DASHBOARD_PASSWORD:
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)
        # Chave interna para automação (scheduled tasks)
        if INTERNAL_API_KEY and request.headers.get("X-Internal-Key") == INTERNAL_API_KEY:
            return await call_next(request)
        token = request.cookies.get("session_token", "")
        if token and token in _valid_tokens:
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login")

# ─── APP ────────────────────────────────────────────────────────────────────
app = FastAPI(title="Crypto Screener")
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")

_CONFIG_PATH = Path(__file__).parent / "screener_config.json"
_CALIBRACAO_TRIGGER = 50

# ─── CACHE DE SCAN (30s TTL) ────────────────────────────────────────────────
_scan_cache: dict = {}     # interval → resultados
_scan_cache_ts: dict = {}  # interval → datetime
_scan_cache_ctx: dict = {} # interval → contexto_mercado
_SCAN_CACHE_TTL = 30       # segundos


def _ler_config_json():
    try:
        from screener import carregar_config_screener
        return carregar_config_screener()
    except Exception:
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}


def _incrementar_sinais_config(n):
    try:
        from screener import carregar_config_screener, salvar_config_screener
        raw = carregar_config_screener()
        raw["sinais_desde_calibracao"] = raw.get("sinais_desde_calibracao", 0) + n
        salvar_config_screener(raw)
        return raw["sinais_desde_calibracao"]
    except Exception:
        return 0


def auto_calibrar_e_notificar():
    try:
        from auto_calibracao import auto_calibrar
        tg = carregar_config()

        def enviar_tg(msg):
            if not tg.get("ativo") or not tg.get("bot_token") or not tg.get("chat_id"):
                return
            import requests
            requests.post(
                "https://api.telegram.org/bot{}/sendMessage".format(tg["bot_token"]),
                json={"chat_id": tg["chat_id"], "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )

        return auto_calibrar(enviar_telegram_fn=enviar_tg)
    except Exception as e:
        return {"status": "erro", "erro": str(e)}


# ─── ENDPOINTS ───────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/login")
def login_page():
    return FileResponse("static/login.html")


@app.post("/api/auth/login")
def auth_login(password: str = Query(...), response: Response = None):
    if not DASHBOARD_PASSWORD:
        return {"sucesso": True, "msg": "auth desabilitada"}
    if password == DASHBOARD_PASSWORD:
        token = secrets.token_hex(32)
        _valid_tokens.add(token)
        response.set_cookie(
            "session_token", token,
            httponly=True, samesite="lax",
            max_age=86400 * 30
        )
        return {"sucesso": True}
    return JSONResponse({"sucesso": False, "msg": "Senha incorreta"}, status_code=401)


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    token = None  # will be cleared via cookie
    response.delete_cookie("session_token")
    return {"sucesso": True}


@app.get("/api/auth/check")
def auth_check():
    return {"autenticado": True}


@app.get("/api/scan")
def scan(
    interval: str = Query(default="1h"),
    top_n: int = Query(default=100, ge=5, le=100),
    telegram: bool = Query(default=True),
):
    if interval not in INTERVALOS:
        return {"error": "Intervalo invalido. Use: {}".format(list(INTERVALOS.keys()))}

    # Circuit breaker de portfolio: bloqueia se P&L do dia < -5% do capital
    tg_cfg = carregar_config()
    _capital = float(tg_cfg.get("capital_total", 200))
    _base    = float(tg_cfg.get("base_valor_trade", 20))
    try:
        from datetime import date as _date
        _hist = carregar_historico()
        hoje_str = str(_date.today())
        _pl_hoje = 0.0
        for _h in _hist:
            if not _h.get("setup_completo"):
                continue
            if not _h["timestamp"].startswith(hoje_str):
                continue
            _r = (_h.get("resultado_8h") or _h.get("resultado_4h") or
                  _h.get("resultado_2h") or _h.get("resultado_1h"))
            if _r is not None:
                _s   = _h.get("score", 8)
                _pos = min(_base * (2.0 if _s >= 10 else 1.5 if _s >= 9 else 1.0), _capital * 0.10)
                _pl_hoje += _r / 100 * _pos
        _cb_limite = _capital * 0.05
        if _capital > 0 and _pl_hoje < -_cb_limite:
            return {
                "interval": interval, "total": 0, "resultados": [],
                "alertas_telegram": [],
                "sinais_registrados": 0,
                "calibracao_rodou": False,
                "circuit_breaker_portfolio": True,
                "mensagem": "CIRCUIT BREAKER PORTFOLIO: P&L do dia -${:.2f} (limite -${:.2f})".format(
                    abs(_pl_hoje), _cb_limite),
                "contexto_mercado": {
                    "fear_greed":    get_fear_greed(),
                    "btc_dominance": get_btc_dominance(),
                },
            }
    except Exception:
        pass

    # ── Cache de 30s: retorna resultado anterior se ainda fresco
    agora_scan = datetime.now()
    cache_ts = _scan_cache_ts.get(interval)
    if cache_ts and (agora_scan - cache_ts).total_seconds() < _SCAN_CACHE_TTL and interval in _scan_cache:
        return {
            "interval": interval,
            "total": len(_scan_cache[interval]),
            "resultados": _scan_cache[interval],
            "alertas_telegram": [],
            "sinais_registrados": 0,
            "calibracao_rodou": False,
            "from_cache": True,
            "contexto_mercado": _scan_cache_ctx.get(interval, {}),
        }

    try:
        resultados = rodar_screener(interval=interval, top_n=top_n)
    except Exception as e:
        return {"error": "Erro no screener: {}".format(e), "resultados": [], "total": 0}

    alertas = []
    try:
        if telegram:
            alertas = checar_e_alertar(resultados)
    except Exception:
        pass

    novos_sinais = 0
    try:
        # Usa score_minimo_setup do config dinâmico (sincronizado com auto-calibração)
        _score_min = int(carregar_config_screener().get("score_minimo_setup", 8))
        novos_sinais = registrar_sinais(resultados, score_minimo=_score_min)
    except Exception:
        pass

    calibracao_rodou = False
    if novos_sinais > 0:
        try:
            total = _incrementar_sinais_config(novos_sinais)
            if total >= _CALIBRACAO_TRIGGER:
                calibracao_rodou = True
                auto_calibrar_e_notificar()
        except Exception:
            pass

    ctx = {
        "fear_greed":    get_fear_greed(),
        "btc_dominance": get_btc_dominance(),
        "btc_4h":        get_btc_4h_tendencia(),
        "score_minimo_efetivo": _calcular_score_minimo(),
    }

    # Salvar no cache
    _scan_cache[interval] = resultados
    _scan_cache_ts[interval] = agora_scan
    _scan_cache_ctx[interval] = ctx

    return {
        "interval": interval,
        "total": len(resultados),
        "resultados": resultados,
        "alertas_telegram": alertas,
        "sinais_registrados": novos_sinais,
        "calibracao_rodou": calibracao_rodou,
        "contexto_mercado": ctx,
    }


def _calcular_score_minimo() -> int:
    """Retorna o score mínimo efetivo com todos os ajustes aplicados."""
    try:
        cfg = carregar_config_screener()
        base = int(cfg.get("score_minimo_setup", 8))
        fg = get_fear_greed()
        fg_v = (fg or {}).get("value")
        dom = get_btc_dominance()
        btc4h = get_btc_4h_tendencia()
        return base + (2 if fg_v is not None and fg_v <= 25 else 0) \
                    + (1 if dom is not None and dom > 55 else 0) \
                    + (1 if btc4h == "abaixo_ema50" else 0)
    except Exception:
        return 8


@app.get("/api/mercado")
def contexto_mercado():
    return {
        "fear_greed":    get_fear_greed(),
        "btc_dominance": get_btc_dominance(),
        "btc_4h":        get_btc_4h_tendencia(),
        "score_minimo_efetivo": _calcular_score_minimo(),
    }


@app.get("/api/backtest")
def api_backtest(
    symbol: str = Query(default="BTCUSDT"),
    interval: str = Query(default="1h"),
    rsi_compra: float = Query(default=35),
    rsi_venda: float = Query(default=65),
    stop_loss: float = Query(default=5.0),
    capital: float = Query(default=1000.0),
):
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    return backtest(symbol=sym, interval=interval, rsi_compra=rsi_compra,
                    rsi_venda=rsi_venda, stop_loss_pct=stop_loss, capital_inicial=capital)


@app.get("/api/telegram/config")
def get_telegram_config():
    config = carregar_config()
    token = config.get("bot_token", "")
    config["bot_token_mascarado"] = (token[:10] + "...") if (len(token) > 10 and "SEU_TOKEN" not in token) else ""
    config.pop("bot_token", None)
    return config


@app.post("/api/telegram/salvar")
def salvar_telegram(
    ativo: bool = Query(default=False),
    bot_token: str = Query(default=""),
    chat_id: str = Query(default=""),
    score_minimo: int = Query(default=3),
):
    config = carregar_config()
    config["ativo"] = ativo
    config["score_minimo"] = score_minimo
    config["chat_id"] = chat_id
    if bot_token and "SEU_TOKEN" not in bot_token:
        config["bot_token"] = bot_token
    salvar_config(config)
    return {"sucesso": True, "mensagem": "Configuracoes salvas!"}


@app.post("/api/telegram/testar")
def testar_telegram(
    bot_token: str = Query(default=""),
    chat_id: str = Query(default=""),
):
    return testar_conexao(bot_token, chat_id)


@app.get("/api/moedas")
def listar_moedas():
    return [p.replace("USDT", "") for p in TOP_PAIRS]


@app.get("/api/historico")
def historico():
    return obter_historico_com_stats()


@app.get("/api/historico/precos-atuais")
def precos_atuais_historico():
    from concurrent.futures import ThreadPoolExecutor
    historico_data = carregar_historico()
    simbolos = list({h["symbol"] for h in historico_data})

    def buscar(sym):
        try:
            t = get_ticker_24h(sym)
            return sym, float(t.get("lastPrice", 0))
        except Exception:
            return sym, None

    precos = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for sym, preco in ex.map(buscar, simbolos):
            if preco:
                precos[sym] = preco
    return precos


@app.post("/api/historico/verificar")
def verificar_historico():
    resultado = verificar_resultados()
    return {
        "atualizados": resultado["atualizados"],
        "total":       len(resultado["historico"]),
        "mensagem":    "{} sinal(is) atualizado(s).".format(resultado["atualizados"]),
    }


@app.post("/api/historico/adicionar")
def adicionar_manual(
    symbol: str = Query(...),
    preco: float = Query(...),
    nota: str = Query(default=""),
):
    try:
        entrada = registrar_manual(symbol=symbol, preco_entrada=preco, nota=nota)
        return {"sucesso": True, "mensagem": "{} adicionada.".format(entrada["symbol"]), "entrada": entrada}
    except Exception as e:
        return {"sucesso": False, "mensagem": str(e)}


@app.post("/api/calibrar")
def forcar_calibracao():
    return auto_calibrar_e_notificar()


@app.get("/api/calibrar/status")
def status_calibracao():
    try:
        raw = _ler_config_json()
        return {
            "versao":                  raw.get("versao"),
            "score_minimo_setup":      raw.get("score_minimo_setup"),
            "fr_minimo_setup":         raw.get("fr_minimo_setup"),
            "win_rate_atual":          raw.get("win_rate_atual"),
            "total_calibracoes":       raw.get("total_calibracoes", 0),
            "ultima_calibracao":       raw.get("ultima_calibracao"),
            "sinais_desde_calibracao": raw.get("sinais_desde_calibracao", 0),
            "proximo_trigger_em":      max(0, _CALIBRACAO_TRIGGER - raw.get("sinais_desde_calibracao", 0)),
        }
    except Exception as e:
        return {"erro": str(e)}


@app.get("/api/historico/calibracao")
def calibracao_historico(campo: str = Query(default="resultado_1h")):
    historico = carregar_historico()
    sinais_cal = calcular_calibracao(historico, campo=campo)
    return {
        "campo":            campo,
        "total_analisados": sum(1 for h in historico if h.get(campo) is not None),
        "calibracao":       sinais_cal,
    }


@app.get("/api/analytics")
def api_analytics():
    """Profit Factor, EV por score, analise por hora, drawdown."""
    from collections import defaultdict

    historico_data = carregar_historico()
    SL = 3.0; TP1 = 3.0; TP2 = 6.0

    tg_cfg     = carregar_config()
    base_valor = float(tg_cfg.get("base_valor_trade", 20))
    capital    = float(tg_cfg.get("capital_total", 200))

    def posicao_de(h):
        s    = h.get("score", 8)
        mult = 2.0 if s >= 10 else 1.5 if s >= 9 else 1.0
        pos  = base_valor * mult
        return min(pos, capital * 0.10) if capital > 0 else pos

    def pl_simulado(h, pos):
        # TP2: 50% vendido no pico TP1 real + 50% no pico TP2 real
        if h.get("tp2_automatico"):
            tp1_pct = h.get("tp1_automatico_pct", TP1)
            tp2_pct = h.get("tp2_automatico_pct", TP2)
            return (tp1_pct / 100 * pos * 0.5) + (tp2_pct / 100 * pos * 0.5)
        # Stop: usa o pct real do momento do stop
        if h.get("stop_automatico"):
            pct = h.get("stop_automatico_pct", -SL)
            return pct / 100 * pos
        # TP1 já disparado: 50% vendido no pico real + 50% pelo último checkpoint
        if h.get("tp1_automatico"):
            tp1_pct = h.get("tp1_automatico_pct", TP1)
            vals = [h.get("resultado_1h"), h.get("resultado_2h"),
                    h.get("resultado_4h"), h.get("resultado_8h"),
                    h.get("resultado_24h")]
            vals = [v for v in vals if v is not None]
            last_v = vals[-1] if vals else tp1_pct
            return tp1_pct / 100 * pos * 0.5 + last_v / 100 * pos * 0.5
        # Posição aberta: usa checkpoints disponíveis
        vals = [h.get("resultado_1h"), h.get("resultado_2h"),
                h.get("resultado_4h"), h.get("resultado_8h"),
                h.get("resultado_24h")]
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        max_v  = max(vals)
        last_v = vals[-1]
        if any(v <= -SL for v in vals):
            return -SL / 100 * pos
        if max_v >= TP2:
            return TP2 / 100 * pos
        if max_v >= TP1:
            return TP1 / 100 * pos * 0.5 + last_v / 100 * pos * 0.5
        return last_v / 100 * pos

    fechados = []
    for h in historico_data:
        if not h.get("setup_completo"):
            continue
        pos = posicao_de(h)
        pl  = pl_simulado(h, pos)
        if pl is not None:
            fechados.append({"h": h, "pl": pl, "pos": pos})

    # Profit Factor
    ganhos = sum(f["pl"] for f in fechados if f["pl"] > 0)
    perdas = abs(sum(f["pl"] for f in fechados if f["pl"] < 0))
    profit_factor = round(ganhos / perdas, 2) if perdas > 0 else None


    # EV por score
    ev_score = defaultdict(lambda: {"wins": 0, "losses": 0, "sum_win": 0.0, "sum_loss": 0.0})
    for f in fechados:
        s = f["h"].get("score", 0)
        if f["pl"] > 0:
            ev_score[s]["wins"]    += 1
            ev_score[s]["sum_win"] += f["pl"]
        else:
            ev_score[s]["losses"]    += 1
            ev_score[s]["sum_loss"]  += abs(f["pl"])

    ev_por_score = {}
    for s, d in sorted(ev_score.items()):
        total = d["wins"] + d["losses"]
        if total == 0:
            continue
        wr       = d["wins"] / total
        avg_win  = d["sum_win"]  / d["wins"]   if d["wins"]   else 0
        avg_loss = d["sum_loss"] / d["losses"] if d["losses"] else 0
        ev_por_score[str(s)] = {
            "total":    total,
            "win_rate": round(wr * 100, 1),
            "avg_win":  round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "ev":       round(wr * avg_win - (1 - wr) * avg_loss, 2),
        }

    # Analise por hora
    hora_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for f in fechados:
        try:
            hora = int(f["h"]["timestamp"][11:13])
            hora_stats[hora]["total"] += 1
            if f["pl"] > 0:
                hora_stats[hora]["wins"] += 1
        except Exception:
            pass

    por_hora = {
        str(h): {
            "total":    d["total"],
            "win_rate": round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0,
        }
        for h, d in sorted(hora_stats.items())
    }

    # P&L acumulado + drawdown
    pl_acumulado = []
    peak = 0.0; max_dd = 0.0; acum = 0.0
    for f in sorted(fechados, key=lambda x: x["h"]["timestamp"]):
        acum += f["pl"]
        pl_acumulado.append({"ts": f["h"]["timestamp"][:16], "acum": round(acum, 2)})
        if acum > peak:
            peak = acum
        dd = peak - acum
        if dd > max_dd:
            max_dd = dd

    # Portfolio circuit breaker status
    abertas = [h for h in historico_data if h.get("setup_completo") and h.get("resultado_8h") is None]
    risco_aberto = sum(posicao_de(h) * SL / 100 for h in abertas)
    cb_ativo = (capital > 0) and (acum < 0) and (abs(acum) / capital * 100 > 5.0)

    return {
        "total_fechados":        len(fechados),
        "ganhos_total":          round(ganhos, 2),
        "perdas_total":          round(perdas, 2),
        "pl_total":              round(acum, 2),
        "profit_factor":         profit_factor,
        "max_drawdown_usd":      round(max_dd, 2),
        "circuit_breaker_ativo": cb_ativo,
        "risco_aberto_usd":      round(risco_aberto, 2),
        "ev_por_score":          ev_por_score,
        "por_hora":              por_hora,
        "pl_acumulado":          pl_acumulado,
    }


@app.get("/api/risco-posicoes")
def risco_posicoes():
    """Análise de risco bearish das posições abertas."""
    hist = carregar_historico()
    abertas = [
        h for h in hist
        if h.get("setup_completo")
        and h.get("resultado_8h") is None
        and not h.get("stop_automatico")
        and h.get("tipo") != "manual"
    ]
    resultados = []
    for pos in abertas:
        try:
            r = analisar_risco_posicao(pos["symbol"], float(pos["preco_entrada"]))
            r["timestamp_entrada"] = pos["timestamp"]
            r["score_entrada"]     = pos.get("score", 0)
            resultados.append(r)
        except Exception:
            pass
    resultados.sort(key=lambda x: x["score_risco"], reverse=True)
    return {"posicoes": resultados, "total": len(resultados)}


_TELA_PATH = Path(os.environ.get("DATA_DIR", Path(__file__).parent)) / "tela_msg.json"


@app.get("/api/tela")
def get_tela():
    """Retorna a mensagem atual para exibir no dashboard (polling pelo frontend)."""
    try:
        data = json.loads(_TELA_PATH.read_text(encoding="utf-8"))
        return data
    except Exception:
        return {"texto": None}


@app.post("/api/tela/clear")
def clear_tela():
    """Limpa a mensagem da tela após exibição automática."""
    try:
        _TELA_PATH.write_text(json.dumps({"texto": None}), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/tela/test")
def test_tela(texto: str = Query(default="TESTE")):
    """Exibe mensagem de teste no dashboard sem passar pelo Telegram."""
    try:
        _TELA_PATH.write_text(
            json.dumps({"texto": texto, "ts": __import__('datetime').datetime.now().isoformat(timespec='seconds')}),
            encoding="utf-8"
        )
        return {"ok": True, "path": str(_TELA_PATH), "texto": texto}
    except Exception as e:
        return {"ok": False, "erro": str(e), "path": str(_TELA_PATH)}


@app.post("/api/internal/enviar-telegram")
def internal_enviar_telegram(mensagem: str = Query(...)):
    """Envia mensagem Telegram — usado pelos scheduled tasks de automação."""
    from telegram_alert import enviar_mensagem as _enviar
    config = carregar_config()
    token   = config.get("bot_token", "")
    chat_id = str(config.get("chat_id", ""))
    if not token or not chat_id:
        return {"ok": False, "erro": "Telegram não configurado"}
    ok, err = _enviar(token, chat_id, mensagem)
    return {"ok": ok, "erro": err}


@app.get("/api/internal/dados-noite")
def internal_dados_noite():
    """Retorna pacote completo de dados para análise noturna autônoma."""
    from datetime import datetime as _dt, timedelta as _td
    historico_data  = carregar_historico()
    config_screener = _ler_config_json()
    tg_cfg          = carregar_config()

    # Últimas 48h de sinais
    cutoff = (_dt.now() - _td(hours=48)).isoformat(timespec="seconds")
    recentes = [h for h in historico_data if h.get("timestamp", "") >= cutoff]

    # Win rate geral
    fechados = [h for h in historico_data if h.get("setup_completo")]
    wins = sum(1 for h in fechados
               if any(h.get(c, None) is not None and h.get(c, 0) > 0
                      for c in ["resultado_1h","resultado_2h","resultado_4h","resultado_8h"]))
    total_f = len(fechados)
    win_rate = round(wins / total_f * 100, 1) if total_f else 0

    # Por score
    score_stats: dict = {}
    for h in fechados:
        s = str(h.get("score", 0))
        if s not in score_stats:
            score_stats[s] = {"total": 0, "wins": 0}
        score_stats[s]["total"] += 1
        vals = [h.get(c) for c in ["resultado_1h","resultado_2h","resultado_4h","resultado_8h"] if h.get(c) is not None]
        if vals and max(vals) > 0:
            score_stats[s]["wins"] += 1

    # Por setor
    setor_stats: dict = {}
    for h in fechados:
        s = h.get("setor", "Other")
        if s not in setor_stats:
            setor_stats[s] = {"total": 0, "wins": 0}
        setor_stats[s]["total"] += 1
        vals = [h.get(c) for c in ["resultado_1h","resultado_2h","resultado_4h","resultado_8h"] if h.get(c) is not None]
        if vals and max(vals) > 0:
            setor_stats[s]["wins"] += 1

    return {
        "timestamp":         _dt.now().isoformat(timespec="seconds"),
        "total_sinais":      len(historico_data),
        "total_fechados":    total_f,
        "win_rate_geral":    win_rate,
        "score_stats":       score_stats,
        "setor_stats":       setor_stats,
        "sinais_recentes":   recentes[-20:],
        "config_screener":   config_screener,
        "capital_total":     tg_cfg.get("capital_total", 200),
        "base_valor_trade":  tg_cfg.get("base_valor_trade", 20),
        "score_minimo":      tg_cfg.get("score_minimo", 8),
    }


@app.get("/api/debug/bot")
def debug_bot():
    """Diagnóstico: mostra chat_id configurado + últimas atualizações do Telegram."""
    import requests as req
    config = carregar_config()
    token   = config.get("bot_token", "")
    chat_id = str(config.get("chat_id", ""))
    ativo   = config.get("ativo", False)

    if not token or not ativo:
        return {"ativo": ativo, "chat_id_config": chat_id, "erro": "bot desativado ou sem token"}

    try:
        r = req.get(
            "https://api.telegram.org/bot{}/getUpdates".format(token),
            params={"limit": 5, "allowed_updates": ["message"]},
            timeout=8,
        )
        updates = r.json()
        # Mostra chat_ids recebidos sem expor conteúdo completo
        msgs = []
        for upd in updates.get("result", []):
            m = upd.get("message", {})
            msgs.append({
                "update_id": upd.get("update_id"),
                "chat_id":   str(m.get("chat", {}).get("id", "")),
                "texto":     (m.get("text") or "")[:40],
            })
        return {
            "ativo":          ativo,
            "chat_id_config": chat_id,
            "status_telegram": r.status_code,
            "ultimas_msgs":   msgs,
            "tela_path":      str(_TELA_PATH),
            "tela_existe":    _TELA_PATH.exists(),
        }
    except Exception as e:
        return {"erro": str(e)}


@app.get("/api/taxa-brl")
def taxa_brl():
    try:
        ticker = get_ticker_24h("USDTBRL")
        taxa = float(ticker.get("lastPrice", 0))
        if taxa > 0:
            return {"taxa": round(taxa, 4), "par": "USDTBRL", "fonte": "Binance"}
    except Exception:
        pass
    return {"taxa": None, "erro": "Nao foi possivel obter a cotacao."}


@app.get("/api/health")
def health_check():
    """Painel de saúde — consolida contexto, filtros ativos, performance e calibração."""
    from datetime import date as _date, datetime as _dt

    # ── Contexto de mercado ──────────────────────────────────────────
    fg      = get_fear_greed()
    btc_dom = get_btc_dominance()
    btc_4h  = get_btc_4h_tendencia()
    fg_value = (fg or {}).get("value")

    # ── Score mínimo efetivo ─────────────────────────────────────────
    config_s  = carregar_config_screener()
    score_base = int(config_s.get("score_minimo_setup", 8))
    fg_ajuste      = 2 if (fg_value is not None and fg_value <= 25) else 0
    btc_dom_ajuste = 1 if (btc_dom is not None and btc_dom > 55) else 0
    btc_4h_ajuste  = 1 if btc_4h == "abaixo_ema50" else 0
    score_min_efetivo = score_base + fg_ajuste + btc_dom_ajuste + btc_4h_ajuste

    if score_min_efetivo >= 11:
        modo = "conservador_extremo"
    elif score_min_efetivo >= 10:
        modo = "conservador"
    else:
        modo = "normal"

    # ── Histórico ────────────────────────────────────────────────────
    historico   = carregar_historico()
    hoje_str    = str(_date.today())
    tg_cfg      = carregar_config()
    base_valor  = float(tg_cfg.get("base_valor_trade", 20))
    capital     = float(tg_cfg.get("capital_total", 200))

    def _pos(h):
        s = h.get("score", 8)
        mult = 2.0 if s >= 10 else 1.5 if s >= 9 else 1.0
        return min(base_valor * mult, capital * 0.10) if capital > 0 else base_valor * mult

    def _fechado(h):
        return bool(h.get("stop_automatico")) or h.get("resultado_8h") is not None

    setups_hoje = [
        h for h in historico
        if h.get("setup_completo") and h.get("timestamp", "").startswith(hoje_str)
    ]

    # P&L hoje
    pl_hoje = 0.0
    for h in setups_hoje:
        vals = [h.get(c) for c in ["resultado_1h","resultado_2h","resultado_4h","resultado_8h"] if h.get(c) is not None]
        if vals:
            pl_hoje += vals[-1] / 100 * _pos(h)

    # Fechados — últimas 10 + geral
    fechados = [h for h in historico if h.get("setup_completo") and _fechado(h)]
    ult10    = fechados[-10:]

    def _win(lst):
        wins = sum(
            1 for h in lst
            if any(h.get(c, None) is not None and h.get(c, 0) > 0
                   for c in ["resultado_1h","resultado_2h","resultado_4h","resultado_8h"])
        )
        return round(wins / len(lst) * 100) if lst else None

    # Profit Factor simples
    ganhos = perdas = 0.0
    for h in fechados:
        vals = [h.get(c) for c in ["resultado_1h","resultado_2h","resultado_4h","resultado_8h"] if h.get(c) is not None]
        if not vals:
            continue
        pl = vals[-1] / 100 * _pos(h)
        if pl > 0:
            ganhos += pl
        else:
            perdas += abs(pl)
    pf = round(ganhos / perdas, 2) if perdas > 0 else None

    # Circuit breaker portfolio
    cb_portfolio = capital > 0 and pl_hoje < -(capital * 0.05)

    # Calibração
    raw_cfg  = _ler_config_json()
    sinais_c = raw_cfg.get("sinais_desde_calibracao", 0)

    return {
        "timestamp":           _dt.now().isoformat(timespec="seconds"),
        "modo":                modo,
        "score_minimo_efetivo": score_min_efetivo,
        "ajustes": {
            "base":    score_base,
            "fg":      fg_ajuste,
            "btc_dom": btc_dom_ajuste,
            "btc_4h":  btc_4h_ajuste,
        },
        "mercado": {
            "fg_value": fg_value,
            "fg_label": (fg or {}).get("label"),
            "btc_dom":  btc_dom,
            "btc_4h":   btc_4h,
        },
        "hoje": {
            "data":     hoje_str,
            "sinais":   len(setups_hoje),
            "pl_hoje":  round(pl_hoje, 2),
        },
        "performance": {
            "win_rate_10":    _win(ult10),
            "win_rate_geral": _win(fechados),
            "total_fechados": len(fechados),
            "profit_factor":  pf,
        },
        "circuit_breakers": {
            "portfolio": cb_portfolio,
        },
        "calibracao": {
            "sinais_desde_calibracao": sinais_c,
            "proximo_trigger_em":      max(0, _CALIBRACAO_TRIGGER - sinais_c),
            "trigger_total":           _CALIBRACAO_TRIGGER,
            "score_minimo_atual":      score_base,
            "ultima_calibracao":       raw_cfg.get("ultima_calibracao"),
        },
    }


@app.get("/api/restart")
async def restart_server():
    import subprocess, sys
    async def _kill():
        await asyncio.sleep(0.5)
        cwd = os.path.dirname(os.path.abspath(__file__))
        ppid = os.getppid()
        pid  = os.getpid()
        bat = (
            "@echo off\n"
            "taskkill /F /PID {} >nul 2>&1\n".format(ppid) +
            "taskkill /F /PID {} >nul 2>&1\n".format(pid) +
            "timeout /t 2 >nul\n"
            'start "Crypto Screener" cmd /k "{} main.py"\n'.format(sys.executable)
        )
        bat_path = os.path.join(cwd, "_restart.bat")
        with open(bat_path, "w") as f:
            f.write(bat)
        subprocess.Popen(["cmd", "/c", bat_path], cwd=cwd,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    asyncio.create_task(_kill())
    return {"status": "ok", "msg": "Reiniciando servidor..."}


# ─── LOOPS DE BACKGROUND ────────────────────────────────────────────────────

@app.on_event("startup")
async def iniciar_loops_automaticos():

    # Loop 1: verificar resultados pendentes a cada 30 min
    async def loop_verificacao():
        while True:
            await asyncio.sleep(30 * 60)
            try:
                verificar_resultados()
            except Exception:
                pass

    # Loop 2: scan automatico a cada 60 min
    async def loop_scan_automatico():
        await asyncio.sleep(60 * 60)
        while True:
            try:
                resultados = rodar_screener(interval="1h", top_n=50)
                alertas = checar_e_alertar(resultados)
                _score_min = int(carregar_config_screener().get("score_minimo_setup", 8))
                novos = registrar_sinais(resultados, score_minimo=_score_min)
                if novos > 0:
                    total = _incrementar_sinais_config(novos)
                    if total >= _CALIBRACAO_TRIGGER:
                        auto_calibrar_e_notificar()
            except Exception:
                pass
            await asyncio.sleep(60 * 60)

    # Loop 3: relatorio diario as 8h
    async def loop_relatorio_diario():
        from datetime import datetime as _dt, timedelta as _td
        while True:
            agora  = _dt.now()
            prox8h = agora.replace(hour=8, minute=0, second=0, microsecond=0)
            if agora >= prox8h:
                prox8h = prox8h + _td(days=1)
            await asyncio.sleep((prox8h - agora).total_seconds())
            try:
                hist = carregar_historico()
                enviar_relatorio_diario(hist)
            except Exception:
                pass

    # Loop 4: monitoramento de stop/TP a cada 5 min
    async def loop_stop_automatico():
        await asyncio.sleep(60)
        while True:
            try:
                tg_cfg     = carregar_config()
                sl_pct_cfg = -abs(float(tg_cfg.get("stop_pct", 3.0)))
                tp1_pct    = float(tg_cfg.get("tp1_pct", 5.0))
                disparos   = monitorar_stops_ativos(stop_pct=sl_pct_cfg, tp1_pct=tp1_pct)
                for sinal, pct, preco_atual, tipo in disparos:
                    # Alerta apenas em fechamentos (stop ou TP2)
                    if tipo in ("stop_automatico", "tp2_automatico"):
                        try:
                            enviar_fechamento(sinal, pct, preco_atual, tipo)
                        except Exception:
                            pass
            except Exception:
                pass
            await asyncio.sleep(5 * 60)

    # Loop 5: resumo de posições a cada 2 horas
    async def loop_resumo_posicoes():
        await asyncio.sleep(2 * 60 * 60)
        while True:
            try:
                hist = carregar_historico()
                enviar_resumo_posicoes(hist)
            except Exception:
                pass
            await asyncio.sleep(2 * 60 * 60)

    # Loop 6: proteção por score de risco bearish a cada 15 min
    # Se score_risco >= 8, registra stop_automatico silenciosamente (sem Telegram)
    async def loop_protecao_risco():
        await asyncio.sleep(5 * 60)
        while True:
            try:
                from datetime import datetime as _dt
                from signal_history import carregar_historico as _ch, salvar_historico as _sh, CHECKPOINTS
                from screener import get_ticker_24h as _gt

                hist   = _ch()
                agora  = _dt.now()
                salvar = False

                abertas = [
                    h for h in hist
                    if h.get("setup_completo")
                    and h.get("resultado_8h") is None
                    and not h.get("stop_automatico")
                    and h.get("tipo") != "manual"
                ]

                for pos in abertas:
                    try:
                        risco = analisar_risco_posicao(pos["symbol"], float(pos["preco_entrada"]))
                        if risco.get("score_risco", 0) < 8:
                            continue

                        ticker      = _gt(pos["symbol"])
                        preco_atual = float(ticker.get("lastPrice", 0))
                        if preco_atual == 0:
                            continue

                        entrada = float(pos["preco_entrada"])
                        pct     = round((preco_atual - entrada) / entrada * 100, 2)

                        pos["stop_automatico"]        = True
                        pos["stop_automatico_pct"]    = pct
                        pos["stop_automatico_preco"]  = preco_atual
                        pos["stop_automatico_ts"]     = agora.isoformat(timespec="seconds")
                        pos["stop_automatico_motivo"] = "risco_bearish_{}".format(risco["score_risco"])

                        ts_pos   = _dt.fromisoformat(pos["timestamp"])
                        diff_min = (agora - ts_pos).total_seconds() / 60
                        for mins, campo in CHECKPOINTS:
                            if diff_min >= mins * 0.5 and pos.get(campo) is None:
                                pos[campo] = pct
                                break

                        salvar = True
                    except Exception:
                        continue

                if salvar:
                    _sh(hist)
            except Exception:
                pass
            await asyncio.sleep(5 * 60)
    # Loop 7: bot de comandos Telegram (polling a cada 5s)
    async def loop_telegram_bot():
        await asyncio.sleep(10)
        while True:
            try:
                hist = carregar_historico()
                processar_comandos(hist)
            except Exception:
                pass
            await asyncio.sleep(5)

    asyncio.create_task(loop_verificacao())
    asyncio.create_task(loop_scan_automatico())
    asyncio.create_task(loop_relatorio_diario())
    asyncio.create_task(loop_stop_automatico())
    asyncio.create_task(loop_resumo_posicoes())
    asyncio.create_task(loop_protecao_risco())
    asyncio.create_task(loop_telegram_bot())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
