"""
Auto-Calibracao do Screener -- analise estatistica do signal_history.json
Roda sozinho: python auto_calibracao.py

1. ANALISA  -- win rate por combinacao de filtros, por score, por campo
2. OTIMIZA  -- testa grade de (score_min x fr_min) e encontra o par que maximiza EV
3. APLICA   -- escreve o resultado otimo em screener_config.json (se melhorar)
4. NOTIFICA -- retorna mensagem formatada para o Telegram

A funcao `auto_calibrar()` e a que o main.py chama automaticamente a cada 10 setups.
"""

import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from itertools import product as itertools_product

HISTORY_PATH = Path(__file__).parent / "signal_history.json"
CONFIG_PATH  = Path(__file__).parent / "screener_config.json"
RESULT_PATH  = Path(__file__).parent / "calibracao_resultado.json"

CHECKPOINTS  = ["resultado_1h", "resultado_2h", "resultado_4h", "resultado_8h", "resultado_24h"]
AMOSTRAS_MIN = 5   # ignorar combinacoes com menos de N amostras


# ------------- CARREGAMENTO -------------

def carregar_historico():
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def carregar_config():
    defaults = {
        "score_minimo_setup": 8, "score_minimo_quase": 5,
        "criterios_minimos_setup": 3, "criterios_minimos_quase": 2,
        "rsi_maximo_setup": 58, "rsi_maximo_quase": 62,
        "fr_minimo_setup": 1.5, "fr_forte_threshold": 3.0,
        "fr_bonus_forte": 2, "fr_bonus_normal": 1, "fr_penalidade": 1, "fr_fraca_threshold": -1.5,
        "ob_threshold": 0.30, "ob_bonus": 2, "ob_penalidade": 2,
        "mtf_bonus": 2, "mtf_contradiz_penalidade": 3,
        "market_bearish_penalidade": 2, "meme_penalidade": 2,
        "cooldown_min": 240,
        "ultima_calibracao": None, "total_calibracoes": 0,
        "sinais_desde_calibracao": 0, "win_rate_atual": None, "versao": "v5.0",
    }
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return {**defaults, **{k: v for k, v in raw.items() if not k.startswith("_")}}
    except Exception:
        return defaults


def salvar_config(cfg):
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ------------- ANALISE -------------

def _stats(vals):
    if not vals:
        return None
    wins = sum(1 for v in vals if v > 0)
    return {
        "total":    len(vals),
        "win_rate": round(wins / len(vals) * 100, 1),
        "media":    round(sum(vals) / len(vals), 2),
    }


def analise_combinacoes(historico):
    combos = {
        "MTF_alta":                lambda h: h.get("mtf_tendencia") == "alta",
        "MTF_nao_alta":            lambda h: h.get("mtf_tendencia") != "alta",
        "FR_acima_1.5":            lambda h: (h.get("forca_relativa") or 0) > 1.5,
        "FR_acima_3.0":            lambda h: (h.get("forca_relativa") or 0) > 3.0,
        "MTF_alta+FR>1.5":         lambda h: h.get("mtf_tendencia") == "alta" and (h.get("forca_relativa") or 0) > 1.5,
        "MTF_alta+FR>1.5+OB>0.3":  lambda h: h.get("mtf_tendencia") == "alta" and (h.get("forca_relativa") or 0) > 1.5 and (h.get("ob_imbalance") or 0) > 0.30,
        "setor_Meme":              lambda h: h.get("setor") == "Meme",
        "nao_Meme":                lambda h: h.get("setor") != "Meme",
        "market_bullish":          lambda h: h.get("market_structure") == "bullish",
        "market_bearish":          lambda h: h.get("market_structure") == "bearish",
        "score_gte_9":             lambda h: h.get("score", 0) >= 9,
        "score_gte_8":             lambda h: h.get("score", 0) >= 8,
        "score_gte_7":             lambda h: h.get("score", 0) >= 7,
        "rsi_recovery":            lambda h: h.get("rsi_recovery") is True,
        "ob_positivo":             lambda h: (h.get("ob_imbalance") or 0) > 0.30,
        "setup_completo":          lambda h: h.get("setup_completo") is True,
    }
    resultado = {}
    for nome, filtro in combos.items():
        subset = [h for h in historico if filtro(h)]
        resultado[nome] = {"n": len(subset)}
        for cp in CHECKPOINTS:
            vals = [h[cp] for h in subset if h.get(cp) is not None]
            resultado[nome][cp] = _stats(vals)
    return resultado


def analise_por_faixa_score(historico):
    faixas = {"7": [], "8": [], "9": [], "10+": []}
    for h in historico:
        s = h.get("score", 0)
        if s == 7:    faixas["7"].append(h)
        elif s == 8:  faixas["8"].append(h)
        elif s == 9:  faixas["9"].append(h)
        elif s >= 10: faixas["10+"].append(h)
    resultado = {}
    for faixa, sinais in faixas.items():
        resultado[faixa] = {}
        for cp in CHECKPOINTS:
            vals = [h[cp] for h in sinais if h.get(cp) is not None]
            resultado[faixa][cp] = _stats(vals)
    return resultado


def analise_sl_tp(historico):
    sl = tp1 = tp2 = 0
    total = 0
    for h in historico:
        for cp in CHECKPOINTS:
            res = h.get(cp)
            if res is None:
                continue
            total += 1
            if res <= -3.0: sl  += 1
            if res >= 10.0: tp2 += 1
            elif res >= 5.0: tp1 += 1
    return {
        "total_resultados": total,
        "stop_loss_hits":   sl,
        "tp1_hits":         tp1,
        "tp2_hits":         tp2,
        "sl_pct":           round(sl / total * 100, 1) if total else 0,
    }


def analise_por_campo(historico):
    campos = ["mtf_tendencia", "market_structure", "setor", "rsi_recovery", "setup_completo", "tipo"]
    resultado = {}
    for campo in campos:
        valores = defaultdict(lambda: defaultdict(list))
        for h in historico:
            val = str(h.get(campo, "null"))
            for cp in CHECKPOINTS:
                res = h.get(cp)
                if res is not None:
                    valores[val][cp].append(res)
        resultado[campo] = {
            val: {cp: _stats(lst) for cp, lst in cps.items()}
            for val, cps in valores.items()
        }
    return resultado


# ------------- MOTOR DE OTIMIZACAO -------------

def encontrar_thresholds_otimos(historico, checkpoint="resultado_1h"):
    """
    Grade: score_min em [7,8,9] x fr_min em [0.5,1.0,1.5,2.0,2.5]
    Maximiza: win_rate * (1 + max(avg_return, 0))
    """
    score_range = [7, 8, 9]
    fr_range    = [0.5, 1.0, 1.5, 2.0, 2.5]

    melhor = None
    melhor_ev = -9999.0
    resultados_grade = []

    for score_min, fr_min in itertools_product(score_range, fr_range):
        subset = [
            h for h in historico
            if h.get("score", 0) >= score_min
            and h.get("mtf_tendencia") == "alta"
            and (h.get("forca_relativa") is None or (h.get("forca_relativa") or 0) > fr_min)
            and h.get("setor") != "Meme"
        ]
        vals = [h[checkpoint] for h in subset if h.get(checkpoint) is not None]
        if len(vals) < AMOSTRAS_MIN:
            resultados_grade.append({
                "score_min": score_min, "fr_min": fr_min,
                "n": len(vals), "ev": None, "win_rate": None, "media": None,
            })
            continue

        wins     = sum(1 for v in vals if v > 0)
        win_rate = wins / len(vals)
        media    = sum(vals) / len(vals)
        ev       = win_rate * (1 + max(media, 0))

        resultados_grade.append({
            "score_min": score_min, "fr_min": fr_min,
            "n": len(vals), "ev": round(ev, 4),
            "win_rate": round(win_rate * 100, 1),
            "media":    round(media, 2),
        })

        if ev > melhor_ev:
            melhor_ev = ev
            melhor = {
                "score_min": score_min, "fr_min": fr_min,
                "n": len(vals), "ev": round(ev, 4),
                "win_rate": round(win_rate * 100, 1),
                "media":    round(media, 2),
                "checkpoint": checkpoint,
            }

    return {
        "melhor":    melhor,
        "grade":     resultados_grade,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


# ------------- WIN RATE POR MOEDA -------------

def analise_por_simbolo(historico, checkpoint="resultado_1h"):
    """
    Calcula win rate por moeda individual.
    Retorna dict: {symbol: {"win_rate": 65, "n": 8, "media": 2.3}}
    Minimo AMOSTRAS_MIN sinais com resultado para incluir.
    """
    dados = defaultdict(list)
    for h in historico:
        val = h.get(checkpoint)
        if val is not None:
            dados[h["symbol"]].append(val)

    resultado = {}
    for sym, vals in dados.items():
        if len(vals) < AMOSTRAS_MIN:
            continue
        wins = sum(1 for v in vals if v > 0)
        resultado[sym] = {
            "win_rate": round(wins / len(vals) * 100, 1),
            "n":        len(vals),
            "media":    round(sum(vals) / len(vals), 2),
        }
    return resultado


# ------------- APLICAR CALIBRACAO -------------

def aplicar_calibracao(otimizacao, win_rate_minimo=40.0):
    """
    Aplica o melhor threshold ao screener_config.json.
    So aplica se win_rate > win_rate_minimo (evita piora).
    """
    melhor = otimizacao.get("melhor")
    if not melhor:
        return {"aplicado": False, "motivo": "Nenhum threshold otimo encontrado."}

    if melhor["win_rate"] < win_rate_minimo:
        motivo = "Win rate otimo abaixo do minimo ({:.1f}% < {:.1f}%).".format(
            melhor["win_rate"], win_rate_minimo)
        return {"aplicado": False, "motivo": motivo}

    cfg = carregar_config()
    antes = {
        "score_minimo_setup": cfg["score_minimo_setup"],
        "fr_minimo_setup":    cfg["fr_minimo_setup"],
        "win_rate_atual":     cfg.get("win_rate_atual"),
    }

    cfg["score_minimo_setup"]      = melhor["score_min"]
    cfg["fr_minimo_setup"]         = melhor["fr_min"]
    cfg["win_rate_atual"]          = melhor["win_rate"]
    cfg["ultima_calibracao"]       = datetime.now().isoformat(timespec="seconds")
    cfg["total_calibracoes"]       = cfg.get("total_calibracoes", 0) + 1
    cfg["sinais_desde_calibracao"] = 0
    cfg["versao"]                  = "v5.{}".format(cfg["total_calibracoes"])

    salvar_config(cfg)

    return {
        "aplicado": True,
        "motivo":   "OK",
        "antes":    antes,
        "depois": {
            "score_minimo_setup": cfg["score_minimo_setup"],
            "fr_minimo_setup":    cfg["fr_minimo_setup"],
            "win_rate_atual":     cfg["win_rate_atual"],
            "versao":             cfg["versao"],
        },
    }


# ------------- MENSAGEM TELEGRAM -------------

def gerar_mensagem_telegram(resultado_aplicacao, otimizacao):
    melhor = otimizacao.get("melhor") or {}
    if not resultado_aplicacao.get("aplicado"):
        return (
            "Sem mudancas: {}\n"
            "Win rate otimo: {}% (n={})".format(
                resultado_aplicacao.get("motivo", "?"),
                melhor.get("win_rate", "-"),
                melhor.get("n", "-"),
            )
        )

    antes  = resultado_aplicacao["antes"]
    depois = resultado_aplicacao["depois"]
    mudou_score = antes["score_minimo_setup"] != depois["score_minimo_setup"]
    mudou_fr    = antes["fr_minimo_setup"]    != depois["fr_minimo_setup"]

    linhas = [
        "Auto-Calibracao Concluida",
        "Versao: {}".format(depois["versao"]),
        "Win rate esperado: {}% (era {}%)".format(
            depois["win_rate_atual"],
            antes.get("win_rate_atual") or "?"),
        "Amostras analisadas: {}".format(melhor.get("n", "?")),
        "",
    ]

    if mudou_score:
        linhas.append("Score minimo: {} -> {}".format(
            antes["score_minimo_setup"], depois["score_minimo_setup"]))
    if mudou_fr:
        linhas.append("FR minima: {}% -> {}%".format(
            antes["fr_minimo_setup"], depois["fr_minimo_setup"]))
    if not mudou_score and not mudou_fr:
        linhas.append("Parametros mantidos (ja estavam otimizados)")

    return "\n".join(linhas)


# ------------- FUNCAO PRINCIPAL (chamada pelo main.py) -------------

def auto_calibrar(enviar_telegram_fn=None):
    """
    Chamada automaticamente pelo main.py a cada 10 setups.
    """
    historico = carregar_historico()
    if len(historico) < 10:
        return {"status": "poucos_sinais", "n": len(historico)}

    combos        = analise_combinacoes(historico)
    faixas        = analise_por_faixa_score(historico)
    campos        = analise_por_campo(historico)
    sl_tp         = analise_sl_tp(historico)
    wr_por_moeda  = analise_por_simbolo(historico, checkpoint="resultado_1h")
    otimizacao    = encontrar_thresholds_otimos(historico, checkpoint="resultado_1h")

    resultado_aplicacao = aplicar_calibracao(otimizacao, win_rate_minimo=40.0)

    # Salva win rate por moeda no screener_config.json
    try:
        cfg_atual = carregar_config()
        cfg_atual["win_rate_por_moeda"] = wr_por_moeda
        salvar_config(cfg_atual)
    except Exception:
        pass

    mensagem = gerar_mensagem_telegram(resultado_aplicacao, otimizacao)
    if enviar_telegram_fn and callable(enviar_telegram_fn):
        try:
            enviar_telegram_fn(mensagem)
        except Exception:
            pass

    saida = {
        "total_sinais":          len(historico),
        "timestamp":             datetime.now().isoformat(timespec="seconds"),
        "otimizacao":            otimizacao,
        "resultado_aplicacao":   resultado_aplicacao,
        "combinacoes":           combos,
        "faixas_score":          faixas,
        "campos":                campos,
        "sl_tp":                 sl_tp,
    }
    RESULT_PATH.write_text(json.dumps(saida, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "status":   "ok",
        "aplicado": resultado_aplicacao.get("aplicado", False),
        "mensagem": mensagem,
        "win_rate": melhor.get("win_rate") if (melhor := otimizacao.get("melhor")) else None,
    }


# ------------- RELATORIO TERMINAL -------------

def imprimir_relatorio(historico, combos, faixas, sl_tp, otimizacao):
    sep = "-" * 60
    print("\n" + sep)
    print("  AUTO-CALIBRACAO -- {} sinais no historico".format(len(historico)))
    print(sep)

    print("\nWIN RATE POR COMBINACAO (checkpoint +1h / +4h):")
    print("{:<35} {:>4}  {:>16}  {:>16}".format("Combinacao", "n", "1h", "4h"))
    print("-" * 75)
    for nome, dados in sorted(
        combos.items(),
        key=lambda x: (x[1].get("resultado_1h") or {}).get("win_rate", 0) or 0,
        reverse=True
    ):
        n  = dados.get("n", 0)
        r1 = dados.get("resultado_1h")
        r4 = dados.get("resultado_4h")
        w1 = "{}% (n={})".format(r1["win_rate"], r1["total"]) if r1 else "--"
        w4 = "{}% (n={})".format(r4["win_rate"], r4["total"]) if r4 else "--"
        print("  {:<33} {:>4}  {:>16}  {:>16}".format(nome, n, w1, w4))

    print("\nWIN RATE POR FAIXA DE SCORE (checkpoint +1h):")
    print("{:>8}  {:>6}  {:>10}  {:>8}".format("Score", "total", "win rate", "media %"))
    print("-" * 40)
    for faixa in ["7", "8", "9", "10+"]:
        r1 = (faixas.get(faixa) or {}).get("resultado_1h")
        if r1:
            print("  {:>6}  {:>6}  {:>9}%  {:>+7.2f}%".format(
                faixa, r1["total"], r1["win_rate"], r1["media"]))

    print("\nOTIMIZACAO DE THRESHOLDS (score_min x fr_min -> EV):")
    melhor = otimizacao.get("melhor")
    if melhor:
        print("  Melhor combo: score>={} + FR>{}%".format(melhor["score_min"], melhor["fr_min"]))
        print("  Win rate: {}% | Media: {:+.2f}% | n={}".format(
            melhor["win_rate"], melhor["media"], melhor["n"]))
    print("\n  Grade completa (top 10 com n >= {})".format(AMOSTRAS_MIN))
    print("  {:>10}  {:>8}  {:>5}  {:>7}  {:>8}  {:>8}".format(
        "score_min", "fr_min", "n", "win%", "media%", "EV"))
    for row in sorted([r for r in otimizacao["grade"] if r["ev"] is not None],
                      key=lambda x: x["ev"], reverse=True)[:10]:
        print("  {:>10}  {:>8}  {:>5}  {:>6}%  {:>+7.2f}%  {:>8.4f}".format(
            row["score_min"], row["fr_min"], row["n"],
            row["win_rate"], row["media"], row["ev"]))

    print("\nSTOP LOSS / TAKE PROFIT:")
    print("   Total de resultados: {}".format(sl_tp["total_resultados"]))
    print("   Stop Loss (<= -3%):  {} ({}%)".format(sl_tp["stop_loss_hits"], sl_tp["sl_pct"]))
    print("   Take Profit 1 (>=+5%): {}".format(sl_tp["tp1_hits"]))
    print("   Take Profit 2 (>=+10%): {}".format(sl_tp["tp2_hits"]))
    print("\n" + sep + "\n")


# ------------- ENTRY POINT -------------

def main():
    if not HISTORY_PATH.exists():
        print("signal_history.json nao encontrado.")
        return

    historico  = carregar_historico()
    print("{} registros carregados.".format(len(historico)))

    combos     = analise_combinacoes(historico)
    faixas     = analise_por_faixa_score(historico)
    campos     = analise_por_campo(historico)
    sl_tp      = analise_sl_tp(historico)
    otimizacao = encontrar_thresholds_otimos(historico, checkpoint="resultado_1h")

    imprimir_relatorio(historico, combos, faixas, sl_tp, otimizacao)

    if resultado_aplicacao["aplicado"]:
        print("screener_config.json atualizado!")
        d = resultado_aplicacao["depois"]
        print("  score_min: {} -> {}".format(
            resultado_aplicacao["antes"]["score_minimo_setup"], d["score_minimo_setup"]))
        print("  fr_min:    {} -> {}".format(
            resultado_aplicacao["antes"]["fr_minimo_setup"], d["fr_minimo_setup"]))
        print("  win_rate:  {}%\n".format(d["win_rate_atual"]))
    else:
        print("Config nao alterado: {}\n".format(resultado_aplicacao["motivo"]))

    saida = {
        "total_sinais":          len(historico),
        "timestamp":             datetime.now().isoformat(timespec="seconds"),
        "otimizacao":            otimizacao,
        "resultado_aplicacao":   resultado_aplicacao,
        "combinacoes":           combos,
        "faixas_score":          faixas,
        "campos":                campos,
        "sl_tp":                 sl_tp,
    }
    RESULT_PATH.write_text(json.dumps(saida, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Resultado completo salvo em: {}\n".format(RESULT_PATH))


if __name__ == "__main__":
    main()
