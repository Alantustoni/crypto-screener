"""
trading_math.py — Fonte ÚNICA da verdade para cálculo de posição e P&L.

Antes da auditoria, essas fórmulas estavam duplicadas em ~10 lugares
(main.py, telegram_alert.py, signal_history.py) com CONSTANTES DIVERGENTES
(signal_history usava TP1=5/TP2=10 enquanto o resto usava TP1=3/TP2=6),
o que fazia o mesmo trade ser reportado com P&L diferente em cada tela.

Todos os módulos agora importam daqui. Para mudar a estratégia,
altere SOMENTE as constantes abaixo.
"""

# ── Constantes canônicas (fallback quando o sinal não tem níveis por-ATR)
STOP_LOSS_PCT = 3.0   # stop em -3%
TP1_PCT       = 3.0   # alvo 1: vende 50% em +3%
TP2_PCT       = 6.0   # alvo 2: vende os outros 50% em +6%

# Custo round-trip realista: taxa Binance ~0.1%/lado + slippage ~0.05%/lado.
# Aplicado ao P&L simulado para a validação (paper trading) não mentir a favor.
FEE_SLIPPAGE_RT_PCT = 0.30


def niveis_saida(atr_pct) -> dict:
    """
    Converte o ATR% da moeda (ATR / preço * 100) em níveis de saída
    ajustados à volatilidade — cada moeda ganha alvos do tamanho da própria
    'respiração', em vez de 3%/6% fixos para todas.

    Retorna magnitudes POSITIVAS (%): exit_sl, exit_tp1, exit_tp2, exit_trail, exit_be.
    Com clamps de sanidade (piso e teto) para não ficar apertado nem absurdo.
    O exit_tp2 é generoso (até 15%) para deixar o vencedor correr — a saída
    real do runner costuma ser o trailing, não o teto.
    """
    a = atr_pct if (isinstance(atr_pct, (int, float)) and atr_pct > 0) else 1.5

    def clamp(v, lo, hi):
        return round(min(max(v, lo), hi), 2)

    return {
        "exit_sl":    clamp(1.5 * a, 2.5, 6.0),
        "exit_tp1":   clamp(1.5 * a, 2.5, 6.0),
        "exit_tp2":   clamp(3.0 * a, 5.0, 15.0),
        "exit_trail": clamp(1.5 * a, 2.0, 5.0),
        # Breakeven mais tardio (piso 1.5%, antes 0.75×ATR/1.0%): os dados
        # mostraram muitos trades subindo ~+1% e sendo fechados no zero cedo
        # demais. Agora só arma perto do TP1, deixando o movimento respirar.
        "exit_be":    clamp(1.0 * a, 1.5, 3.0),
    }


def calcular_posicao(score, base_valor: float, capital: float) -> float:
    """
    Tamanho da posição em USD a partir do score.
    score >= 10 → 2x | >= 9 → 1.5x | senão 1x. Teto de 10% do capital.
    """
    try:
        s = int(score) if score is not None else 8
    except (TypeError, ValueError):
        s = 8
    mult = 2.0 if s >= 10 else 1.5 if s >= 9 else 1.0
    pos = base_valor * mult
    return min(pos, capital * 0.10) if capital > 0 else pos


# Rateio da saída em 3 partes (moonbag):
#   50% no TP1  |  30% no TP2  |  20% "moonbag" correndo até 24h com stop largo
W_TP1, W_TP2, W_MOON = 0.5, 0.3, 0.2


def pl_simulado(h: dict, pos: float,
                sl: float = STOP_LOSS_PCT,
                tp1: float = TP1_PCT,
                tp2: float = TP2_PCT,
                custo_rt_pct: float = FEE_SLIPPAGE_RT_PCT):
    """
    P&L simulado em USD de um sinal, com rateio moonbag 50/30/20.
    Retorna None quando a posição está aberta e ainda não há checkpoint.

    - Usa os níveis por-sinal (exit_sl/exit_tp1/exit_tp2) quando presentes.
    - Desconta custo round-trip (taxa + slippage). custo_rt_pct=0.0 → P&L bruto.

    Estágios (na ordem):
      1. moonbag_automatico → 50%·TP1 + 30%·TP2 + 20%·saída do moonbag  (final)
      2. moonbag_ativo (TP2 batido, 20% correndo) → 50%·TP1 + 30%·TP2 + 20%·atual
      3. tp2_automatico sem moonbag (runner saiu no trailing antes do TP2) → 50/50
      4. stop antes do TP1 → pct real do stop
      5. tp1_automatico → 50%·TP1 + 50%·(stop, se houve, ou último checkpoint)
      6. Aberta (só checkpoints) → stop / TP2(50/30/20) / TP1(50/50) / último
    """
    sl  = abs(h.get("exit_sl")  or sl)
    tp1 = h.get("exit_tp1") or tp1
    tp2 = h.get("exit_tp2") or tp2

    def _com_custo(bruto):
        if bruto is None:
            return None
        return bruto - pos * custo_rt_pct / 100.0

    def _checkpoints():
        vals = [h.get("resultado_{}".format(t)) for t in ("1h", "2h", "4h", "8h", "24h")]
        return [v for v in vals if v is not None]

    tp1_pct = h.get("tp1_automatico_pct", tp1)
    tp2_pct = h.get("tp2_automatico_pct", tp2)

    # 1 — moonbag fechado (final, 3 partes)
    if h.get("moonbag_automatico"):
        moon_pct = h.get("moonbag_automatico_pct", tp2_pct)
        return _com_custo((tp1_pct * W_TP1 + tp2_pct * W_TP2 + moon_pct * W_MOON) / 100 * pos)

    # 2 — TP2 batido, 20% moonbag ainda correndo (marca a mercado)
    if h.get("moonbag_ativo"):
        vals = _checkpoints()
        atual = vals[-1] if vals else tp2_pct
        return _com_custo((tp1_pct * W_TP1 + tp2_pct * W_TP2 + atual * W_MOON) / 100 * pos)

    # 3 — runner (50%) fechou no trailing antes do TP2 → 50/50
    if h.get("tp2_automatico"):
        return _com_custo((tp1_pct * 0.5 + tp2_pct * 0.5) / 100 * pos)

    # 4 — stop total antes de qualquer parcial
    if h.get("stop_automatico") and not h.get("tp1_automatico"):
        return _com_custo(h.get("stop_automatico_pct", -sl) / 100 * pos)

    # 5 — TP1 batido, 50% restante aberto (ou stop pós-TP1)
    if h.get("tp1_automatico"):
        if h.get("stop_automatico"):
            saida = h.get("stop_automatico_pct", -sl)
        else:
            vals = _checkpoints()
            saida = vals[-1] if vals else tp1_pct
        return _com_custo((tp1_pct * 0.5 + saida * 0.5) / 100 * pos)

    # 6 — totalmente aberta: usa só checkpoints
    vals = _checkpoints()
    if not vals:
        return None
    max_v, last_v = max(vals), vals[-1]
    if any(v <= -sl for v in vals):
        return _com_custo(-sl / 100 * pos)
    if max_v >= tp2:
        return _com_custo((tp1 * W_TP1 + tp2 * W_TP2 + last_v * W_MOON) / 100 * pos)
    if max_v >= tp1:
        return _com_custo((tp1 * 0.5 + last_v * 0.5) / 100 * pos)
    return _com_custo(last_v / 100 * pos)
