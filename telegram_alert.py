import json
import os
import requests
from datetime import datetime, timedelta
from pathlib import Path
from trading_math import calcular_posicao, pl_simulado, niveis_saida

# Arquivos de estado ficam em DATA_DIR (Railway volume) para persistir entre redeploys.
_PROJ_DIR    = Path(__file__).parent
_DATA_DIR    = Path(os.environ.get("DATA_DIR", _PROJ_DIR))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH  = _DATA_DIR / "telegram_config.json"
_OFFSET_PATH = _DATA_DIR / "telegram_offset.json"
_TELA_PATH   = _DATA_DIR / "tela_msg.json"

# Migração: copia config do projeto para o volume na primeira execução
_CONFIG_LEGADO = _PROJ_DIR / "telegram_config.json"
if not CONFIG_PATH.exists() and _CONFIG_LEGADO.exists():
    try:
        import shutil
        shutil.copy2(_CONFIG_LEGADO, CONFIG_PATH)
    except Exception:
        pass

_ultimo_setup: dict = {}


def _set_tela(texto):
    """Salva mensagem para exibir no dashboard (None = limpar). Retorna True se gravou."""
    try:
        _TELA_PATH.write_text(
            json.dumps({"texto": texto, "ts": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8"
        )
        return True
    except Exception as e:
        print("[TELA] Erro ao gravar {}: {}".format(_TELA_PATH, e))
        return False


def carregar_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ativo": False}


def salvar_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def enviar_mensagem(token, chat_id, texto):
    try:
        r = requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(token),
            json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200, r.json().get("description", "") if r.status_code != 200 else ""
    except Exception as e:
        return False, str(e)


def _calcular_posicao(score, base_valor, capital):
    # Delega para a fonte única (trading_math). Mantido por compatibilidade.
    return calcular_posicao(score, base_valor, capital)


def _taxa_brl():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "USDTBRL"}, timeout=5
        )
        return float(r.json().get("price", 0)) or None
    except Exception:
        return None


def _pl_simulado(h, pos, SL=3.0, TP1=3.0, TP2=6.0):
    # Delega para a fonte única (trading_math). Mantido por compatibilidade.
    return pl_simulado(h, pos, sl=SL, tp1=TP1, tp2=TP2)


# ─── ALERTA DE ENTRADA ───────────────────────────────────────────────────────

def checar_e_alertar(resultados):
    config = carregar_config()
    if not config.get("ativo"):
        return []

    token   = config.get("bot_token", "")
    chat_id = config.get("chat_id", "")
    if not token or not chat_id or "SEU_TOKEN" in token:
        return []

    base_valor = float(config.get("base_valor_trade", 20))
    capital    = float(config.get("capital_total", 200))
    agora      = datetime.now()
    enviados   = []

    try:
        from screener import get_ticker_24h, carregar_config_screener
        btc = get_ticker_24h("BTCUSDT")
        btc_24h = float(btc.get("priceChangePercent", 0))
        limiar = carregar_config_screener().get("circuit_breaker_pct", -7.0)
        if btc_24h <= limiar:
            return []
    except Exception:
        pass

    for r in resultados:
        if not r.get("setup_completo"):
            continue

        symbol = r["symbol"]
        nome   = symbol.replace("USDT", "")

        ultimo = _ultimo_setup.get(symbol)
        if ultimo and (agora - ultimo) < timedelta(hours=2):
            continue

        preco     = float(r["preco"])
        # Níveis ajustados à volatilidade da moeda (ATR) — mesmos que o servidor
        # usará para monitorar stop/alvos/trailing. Fallback 3/6 se sem ATR.
        _niv      = niveis_saida(r.get("atr_pct"))
        sl_pct    = _niv["exit_sl"]; tp1_pct = _niv["exit_tp1"]
        tp2_pct   = _niv["exit_tp2"]; trail_pct = _niv["exit_trail"]
        sl_preco  = round(preco * (1 - sl_pct  / 100), 6)
        tp1_preco = round(preco * (1 + tp1_pct / 100), 6)
        tp2_preco = round(preco * (1 + tp2_pct / 100), 6)
        posicao   = _calcular_posicao(r["score"], base_valor, capital)
        mult_txt  = "2x" if r["score"] >= 10 else "1.5x" if r["score"] >= 9 else "1x"
        perda_max = round(posicao * sl_pct  / 100, 2)
        lucro_tp1 = round(posicao * tp1_pct / 100 * 0.5, 2)
        lucro_tp2 = round(posicao * tp2_pct / 100 * 0.5, 2)

        setor_txt  = " | {}".format(r.get("setor")) if r.get("setor") else ""
        wr         = r.get("win_rate_historico")
        wr_txt     = "\nHistorico: {}% de acerto".format(wr) if wr is not None else ""
        sinais_txt = "\n".join("  - {}".format(s) for s in r.get("sinais", []))

        texto = (
            "NOVA ENTRADA - {}/USDT{}\n"
            "Score: {}  |  RSI: {}  |  Criterios: {}/10{}\n"
            "------------------------------\n"
            "Entrada:  ${}\n"
            "Posicao:  ${} ({} = score {})\n\n"
            "Stop Loss:          ${} (-{}%)  -> -${}\n"
            "Alvo 1 (vende 50%): ${} (+{}%) -> +${}\n"
            "Alvo 2 (vende 50%): ${} (+{}%) -> +${}\n"
            "Trailing stop: {}% abaixo do pico\n\n"
            "Sinais:\n{}\n\n{}"
        ).format(
            nome, setor_txt,
            r["score"], r.get("rsi", "—"), r.get("criterios_ativos", 0), wr_txt,
            preco, round(posicao, 2), mult_txt, r["score"],
            sl_preco, sl_pct, perda_max,
            tp1_preco, tp1_pct, lucro_tp1,
            tp2_preco, tp2_pct, lucro_tp1 + lucro_tp2,
            trail_pct, sinais_txt,
            agora.strftime("%d/%m/%Y %H:%M"),
        )

        ok, _ = enviar_mensagem(token, chat_id, texto)
        if ok:
            _ultimo_setup[symbol] = agora
            enviados.append(nome)

    return enviados


# ─── ALERTA DE FECHAMENTO (lucro ou prejuizo) ────────────────────────────────

def enviar_fechamento(sinal, pct, preco_atual, tipo):
    """Dispara quando uma posição fecha — com lucro (TP2) ou prejuizo (stop)."""
    config = carregar_config()
    if not config.get("ativo"):
        return False

    token   = config.get("bot_token", "")
    chat_id = config.get("chat_id", "")
    if not token or not chat_id or "SEU_TOKEN" in token:
        return False

    base_valor = float(config.get("base_valor_trade", 20))
    capital    = float(config.get("capital_total", 200))
    nome       = sinal["symbol"].replace("USDT", "")
    entrada    = float(sinal["preco_entrada"])
    posicao    = _calcular_posicao(sinal.get("score", 8), base_valor, capital)
    pl         = _pl_simulado(sinal, posicao)
    pl_val     = abs(pl) if pl is not None else abs(pct) / 100 * posicao
    taxa       = _taxa_brl()
    brl_txt    = "  (R$ {:.2f})".format(pl_val * taxa) if taxa else ""
    sinal_brl  = "+" if (pl is not None and pl >= 0) else "-"

    if tipo in ("tp2_automatico", "moonbag_automatico"):
        tp1_pct = sinal.get("tp1_automatico_pct", 3.0)
        if tipo == "moonbag_automatico":
            tp2_pct = sinal.get("tp2_automatico_pct", 6.0)
            emoji   = "FECHADO (MOONBAG) - {}/USDT\n".format(nome)
            detalhe = (
                "Alvo 1: +{:.2f}% (50%)\n"
                "Alvo 2: +{:.2f}% (30%)\n"
                "Moonbag: {:+.2f}% (20%)\n"
                "Resultado total: {}${:.2f}{}"
            ).format(tp1_pct, tp2_pct, pct, sinal_brl, pl_val, brl_txt)
        else:
            emoji   = "FECHADO COM LUCRO - {}/USDT\n".format(nome)
            detalhe = (
                "Alvo 1: +{:.2f}% (50% vendido)\n"
                "Alvo 2: +{:.2f}% (50% vendido)\n"
                "Lucro total: +${:.2f}{}"
            ).format(tp1_pct, pct, pl_val, brl_txt)
    else:
        motivo  = sinal.get("stop_automatico_motivo", "")
        origem  = " (risco bearish)" if "risco" in motivo else ""
        emoji   = "FECHADO COM PREJUIZO - {}/USDT{}\n".format(nome, origem)
        detalhe = (
            "Entrada: ${}  ->  Saida: ${}\n"
            "Resultado: {:.2f}%\n"
            "Prejuizo: -${:.2f}{}"
        ).format(entrada, preco_atual, pct, pl_val, brl_txt)

    texto = (
        "{}"
        "==============================\n"
        "Entrada: ${}  |  Posicao: ${:.0f}\n\n"
        "{}\n\n{}"
    ).format(
        emoji, entrada, posicao,
        detalhe,
        datetime.now().strftime("%d/%m/%Y %H:%M"),
    )

    ok, _ = enviar_mensagem(token, chat_id, texto)
    return ok


# ─── RESUMO A CADA 2 HORAS ───────────────────────────────────────────────────

def enviar_resumo_posicoes(historico):
    config = carregar_config()
    if not config.get("ativo"):
        return False

    token   = config.get("bot_token", "")
    chat_id = config.get("chat_id", "")
    if not token or not chat_id or "SEU_TOKEN" in token:
        return False

    base_valor = float(config.get("base_valor_trade", 20))
    capital    = float(config.get("capital_total", 200))

    def posicao_de(h):
        return calcular_posicao(h.get("score", 8), base_valor, capital)

    abertas = []; lucro_ops = []; prej_ops = []

    for h in historico:
        if not h.get("setup_completo"):
            continue
        pos = posicao_de(h)
        if h.get("stop_automatico") or h.get("resultado_8h") is not None:
            pl = _pl_simulado(h, pos)
            if pl is None:
                continue
            (lucro_ops if pl >= 0 else prej_ops).append({"h": h, "pl": pl})
        else:
            abertas.append(h)

    todas_fechadas = lucro_ops + prej_ops
    total_pl   = sum(f["pl"] for f in todas_fechadas)
    n_fechadas = len(todas_fechadas)
    win_rate   = round(len(lucro_ops) / n_fechadas * 100) if n_fechadas else 0
    ev         = round(total_pl / n_fechadas, 2) if n_fechadas else 0
    ganhos_v   = sum(f["pl"] for f in lucro_ops)
    perdas_v   = abs(sum(f["pl"] for f in prej_ops))
    pf         = round(ganhos_v / perdas_v, 2) if perdas_v > 0 else None

    taxa    = _taxa_brl()
    pl_brl  = round(total_pl * taxa, 2) if taxa else None
    brl_str = "  (R$ {}{:.2f})".format("+" if pl_brl and pl_brl >= 0 else "", pl_brl) if pl_brl else ""

    ab_linhas = ""
    for h in abertas[:5]:
        nome = h["symbol"].replace("USDT", "")
        ts   = h["timestamp"][11:16]
        ab_linhas += "\n  {} (entrada {}  ${})".format(nome, ts, h["preco_entrada"])
    if len(abertas) > 5:
        ab_linhas += "\n  ... e mais {}".format(len(abertas) - 5)

    pf_txt = "{}x".format(pf) if pf else "sem perdas"
    ev_txt = "{}${:.2f}/op".format("+" if ev >= 0 else "", ev)

    agora = datetime.now()
    texto = (
        "RESUMO - {}\n"
        "==============================\n\n"
        "Em aberto: {}{}\n\n"
        "Lucro: {} | Prej: {} | Acerto: {}%\n"
        "EV medio:      {}\n"
        "Profit Factor: {}\n\n"
        "Resultado total: {}{:.2f}{}\n\n"
        "Proximo resumo em 2h"
    ).format(
        agora.strftime("%d/%m %H:%M"),
        len(abertas), ab_linhas if abertas else " nenhuma",
        len(lucro_ops), len(prej_ops), win_rate,
        ev_txt, pf_txt,
        "+" if total_pl >= 0 else "", abs(total_pl), brl_str,
    )

    ok, _ = enviar_mensagem(token, chat_id, texto)
    return ok


# ─── RELATÓRIO DIÁRIO (8h) ───────────────────────────────────────────────────

def enviar_relatorio_diario(historico):
    config = carregar_config()
    if not config.get("ativo"):
        return False

    token   = config.get("bot_token", "")
    chat_id = config.get("chat_id", "")
    if not token or not chat_id or "SEU_TOKEN" in token:
        return False

    base_valor = float(config.get("base_valor_trade", 20))
    capital    = float(config.get("capital_total", 200))

    def posicao_de(h):
        return calcular_posicao(h.get("score", 8), base_valor, capital)

    abertas = []; fechadas = []
    for h in historico:
        if not h.get("setup_completo"):
            continue
        pos = posicao_de(h)
        if h.get("stop_automatico") or h.get("resultado_8h") is not None:
            pl = _pl_simulado(h, pos)
            if pl is not None:
                fechadas.append({"h": h, "pl": pl})
        else:
            abertas.append(h)

    n_lucro  = sum(1 for f in fechadas if f["pl"] > 0)
    n_prej   = sum(1 for f in fechadas if f["pl"] <= 0)
    total_pl = sum(f["pl"] for f in fechadas)
    win_rate = round(n_lucro / len(fechadas) * 100) if fechadas else 0
    ganhos   = sum(f["pl"] for f in fechadas if f["pl"] > 0)
    perdas   = abs(sum(f["pl"] for f in fechadas if f["pl"] < 0))
    pf       = round(ganhos / perdas, 2) if perdas > 0 else None
    ev       = round(total_pl / len(fechadas), 2) if fechadas else 0

    taxa    = _taxa_brl()
    pl_brl  = round(total_pl * taxa, 2) if taxa else None
    brl_str = "  (R$ {}{:.2f})".format("+" if pl_brl and pl_brl >= 0 else "", pl_brl) if pl_brl else ""

    melhor = max(fechadas, key=lambda f: f["pl"]) if fechadas else None
    pior   = min(fechadas, key=lambda f: f["pl"]) if fechadas else None

    ab_txt = ""
    for h in abertas[:5]:
        nome = h["symbol"].replace("USDT", "")
        ab_txt += "\n  {} (${}  score {})".format(nome, h["preco_entrada"], h.get("score", "—"))
    if len(abertas) > 5:
        ab_txt += "\n  ... e mais {}".format(len(abertas) - 5)

    melhor_txt = "\nMelhor: {} +${:.2f}".format(melhor["h"]["symbol"].replace("USDT",""), melhor["pl"]) if melhor else ""
    pior_txt   = "\nPior:   {} -${:.2f}".format(pior["h"]["symbol"].replace("USDT",""), abs(pior["pl"])) if pior else ""

    pf_txt = "{}x".format(pf) if pf else "sem perdas"
    ev_txt = "{}${:.2f}/op".format("+" if ev >= 0 else "", ev)

    agora = datetime.now()
    texto = (
        "BOM DIA! Relatorio {}\n"
        "==============================\n\n"
        "EM ABERTO: {}{}\n\n"
        "HISTORICO FECHADO: {} operacoes\n"
        "  Lucro:         {}\n"
        "  Prejuizo:      {}\n"
        "  Acerto:        {}%\n"
        "  EV medio:      {}\n"
        "  Profit Factor: {}\n\n"
        "RESULTADO TOTAL: {}{:.2f}{}\n"
        "{}\n\n{}"
    ).format(
        agora.strftime("%d/%m/%Y"),
        len(abertas), ab_txt if abertas else " nenhuma",
        len(fechadas), n_lucro, n_prej, win_rate, ev_txt, pf_txt,
        "+" if total_pl >= 0 else "", abs(total_pl), brl_str,
        melhor_txt + pior_txt,
        agora.strftime("%H:%M"),
    )

    ok, _ = enviar_mensagem(token, chat_id, texto)
    return ok


# ─── BOT DE COMANDOS ─────────────────────────────────────────────────────────

def _carregar_offset():
    try:
        return json.loads(_OFFSET_PATH.read_text())["offset"]
    except Exception:
        return 0


def _salvar_offset(offset):
    try:
        _OFFSET_PATH.write_text(json.dumps({"offset": offset}))
    except Exception:
        pass


def processar_comandos(historico):
    """
    Faz polling do Telegram a cada 5s. Responde comandos:
      /posicoes  — posições abertas com P&L atual
      /resultado — P&L total consolidado
      /ajuda     — lista de comandos
    """
    config = carregar_config()
    if not config.get("ativo"):
        return

    token   = config.get("bot_token", "")
    chat_id = str(config.get("chat_id", ""))
    if not token or not chat_id or "SEU_TOKEN" in token:
        return

    base_valor = float(config.get("base_valor_trade", 20))
    capital    = float(config.get("capital_total", 200))

    offset = _carregar_offset()

    try:
        r = requests.get(
            "https://api.telegram.org/bot{}/getUpdates".format(token),
            params={"offset": offset, "timeout": 3, "allowed_updates": ["message"]},
            timeout=6,
        )
        if r.status_code != 200:
            return
        updates = r.json().get("result", [])
    except Exception:
        return

    for upd in updates:
        offset = upd["update_id"] + 1
        msg = upd.get("message", {})

        # Só responde ao chat configurado
        if str(msg.get("chat", {}).get("id", "")) != chat_id:
            continue

        texto_recv  = msg.get("text", "").strip() if msg.get("text") else ""
        texto_lower = texto_recv.lower()

        # Aceita tanto /comando quanto palavra solta, e frases contendo as palavras-chave
        def contem(*palavras):
            return any(p in texto_lower for p in palavras)

        # /tela <mensagem> — exibe qualquer texto fullscreen no dashboard
        if texto_lower.startswith("/tela"):
            resto = texto_recv[5:].strip()  # preserva maiúsculas/minúsculas
            if not resto or resto.lower() in ("off", "limpar", "clear", "fechar"):
                _set_tela(None)
                resposta = "✅ Tela limpa!"
            else:
                ok = _set_tela(resto)
                if ok:
                    resposta = "✅ Exibindo no dashboard: \"{}\"".format(resto)
                else:
                    resposta = "❌ Erro ao gravar tela_msg.json — verifique DATA_DIR"
        elif contem("/posicoes", "posicoes", "/aberto", "aberto", "/status", "status",
                  "abertas", "em aberto", "open"):
            resposta = _cmd_posicoes(historico, base_valor, capital)
        elif contem("/prejuizo", "prejuizo", "perdas", "perdi", "perdeu", "negativos",
                    "quais perderam", "deram prejuizo"):
            resposta = _cmd_prejuizo(historico, base_valor, capital)
        elif contem("/resultado", "resultado", "/pl", "/lucro", "lucro", "geral",
                    "quanto ganhei", "quanto lucrei", "como estou", "como esta"):
            resposta = _cmd_resultado(historico, base_valor, capital)
        elif contem("/ajuda", "ajuda", "/help", "help", "/comandos", "comandos", "o que voce faz"):
            resposta = _cmd_ajuda()
        else:
            resposta = (
                "Nao entendi. Exemplos do que voce pode perguntar:\n\n"
                "   como esta meu lucro geral?\n"
                "   quais moedas deram prejuizo?\n"
                "   quais posicoes estao abertas?\n"
                "   /tela Tubarao  (exibe na tela do dashboard)\n\n"
                "Ou use /ajuda para ver todos os comandos."
            )

        enviar_mensagem(token, chat_id, resposta)

    _salvar_offset(offset)


def _cmd_posicoes(historico, base_valor, capital):
    def posicao_de(h):
        return calcular_posicao(h.get("score", 8), base_valor, capital)

    abertas = [
        h for h in historico
        if h.get("setup_completo")
        and h.get("resultado_8h") is None
        and not h.get("stop_automatico")
    ]

    if not abertas:
        return "Nenhuma posicao aberta no momento."

    try:
        from screener import get_ticker_24h
    except Exception:
        return "Erro ao buscar precos."

    linhas = ["POSICOES ABERTAS  ({})".format(datetime.now().strftime("%H:%M")), ""]
    for h in abertas:
        nome    = h["symbol"].replace("USDT", "")
        entrada = float(h["preco_entrada"])
        pos     = posicao_de(h)
        try:
            t   = get_ticker_24h(h["symbol"])
            cur = float(t.get("lastPrice", 0))
            pct = round((cur - entrada) / entrada * 100, 2)
            pl  = round(pct / 100 * pos, 2)
            pl_str = "{}{:.2f}% | {}{:.2f}$".format(
                "+" if pct >= 0 else "", pct,
                "+" if pl >= 0 else "", pl
            )
        except Exception:
            pl_str = "sem preco"
        linhas.append("{}: {} (score {})".format(nome, pl_str, h.get("score","—")))

    return "\n".join(linhas)


def _cmd_resultado(historico, base_valor, capital):
    def posicao_de(h):
        return calcular_posicao(h.get("score", 8), base_valor, capital)

    fechadas = []
    for h in historico:
        if not h.get("setup_completo"):
            continue
        pos = posicao_de(h)
        if h.get("stop_automatico") or h.get("resultado_8h") is not None:
            pl = _pl_simulado(h, pos)
            if pl is not None:
                fechadas.append(pl)

    if not fechadas:
        return "Nenhuma operacao fechada ainda."

    total    = sum(fechadas)
    n_lucro  = sum(1 for p in fechadas if p > 0)
    n_prej   = sum(1 for p in fechadas if p <= 0)
    win_rate = round(n_lucro / len(fechadas) * 100)
    taxa     = _taxa_brl()
    brl_txt  = "  (R$ {:.2f})".format(total * taxa) if taxa else ""

    ev       = round(total / len(fechadas), 2)
    ganhos   = sum(p for p in fechadas if p > 0)
    perdas_v = abs(sum(p for p in fechadas if p < 0))
    pf       = round(ganhos / perdas_v, 2) if perdas_v > 0 else None
    pf_txt   = "{}x".format(pf) if pf else "sem perdas"
    ev_sinal = "+" if ev >= 0 else ""

    return (
        "RESULTADO TOTAL  ({})\n"
        "==============================\n"
        "Operacoes: {} | Lucro: {} | Prej: {}\n"
        "Acerto: {}%\n"
        "EV medio: {}${:.2f}/op\n"
        "Profit Factor: {}\n"
        "P&L: {}{:.2f}${}"
    ).format(
        datetime.now().strftime("%d/%m %H:%M"),
        len(fechadas), n_lucro, n_prej, win_rate,
        ev_sinal, abs(ev), pf_txt,
        "+" if total >= 0 else "", total, brl_txt,
    )


def _cmd_prejuizo(historico, base_valor, capital):
    def posicao_de(h):
        return calcular_posicao(h.get("score", 8), base_valor, capital)

    perdas = []
    for h in historico:
        if not h.get("setup_completo"):
            continue
        pos = posicao_de(h)
        if h.get("stop_automatico") or h.get("resultado_8h") is not None:
            pl = _pl_simulado(h, pos)
            if pl is not None and pl < 0:
                perdas.append({"h": h, "pl": pl})

    if not perdas:
        return "Nenhuma operacao fechada com prejuizo ate agora!"

    perdas.sort(key=lambda x: x["pl"])
    taxa = _taxa_brl()

    linhas = ["OPERACOES COM PREJUIZO ({})".format(datetime.now().strftime("%d/%m %H:%M")), ""]
    for f in perdas:
        nome   = f["h"]["symbol"].replace("USDT", "")
        pl     = f["pl"]
        brl    = "  (R$ -{:.2f})".format(abs(pl) * taxa) if taxa else ""
        motivo = f["h"].get("stop_automatico_motivo", "")
        origem = " [risco]" if "risco" in motivo else " [preco]" if motivo else ""
        linhas.append("{}: -${:.2f}{}{}".format(nome, abs(pl), brl, origem))

    total_prej = sum(f["pl"] for f in perdas)
    brl_total  = "  (R$ -{:.2f})".format(abs(total_prej) * taxa) if taxa else ""
    linhas.append("")
    linhas.append("Total: {} operacoes | -${:.2f}{}".format(len(perdas), abs(total_prej), brl_total))

    return "\n".join(linhas)


def _cmd_ajuda():
    return (
        "COMANDOS DISPONIVEIS\n"
        "==============================\n\n"
        "posicoes   - posicoes abertas com P&L atual\n"
        "resultado  - P&L total consolidado com EV\n"
        "prejuizo   - lista de operacoes negativas\n"
        "/tela MSG  - exibe MSG fullscreen no dashboard\n"
        "/tela off  - limpa o dashboard\n\n"
        "Voce pode escrever normalmente, ex:\n"
        "  'como esta meu lucro?'\n"
        "  'quais posicoes abertas?'"
    )


def testar_conexao(bot_token: str = "", chat_id: str = ""):
    config  = carregar_config()
    token   = bot_token or config.get("bot_token", "")
    chat_id = chat_id  or config.get("chat_id", "")
    if not token or not chat_id or "SEU_TOKEN" in token:
        return False, "Token ou chat_id nao configurado"
    try:
        r = requests.get(
            "https://api.telegram.org/bot{}/sendMessage".format(token),
            params={"chat_id": chat_id, "text": "Conexao com o bot OK! Sistema operacional."},
            timeout=8,
        )
        if r.status_code == 200:
            return True, "Mensagem enviada com sucesso"
        return False, "Erro HTTP {}".format(r.status_code)
    except Exception as e:
        return False, str(e)
