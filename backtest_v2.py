"""
backtest_v2.py — Backtest honesto do NÚCLEO da estratégia (fora da amostra, com custos).

Por que existe
--------------
O painel mostra resultado de PAPER TRADING sobre ~50 trades de poucos dias. Isso é
amostra pequena demais pra decidir pôr dinheiro real. Este script puxa histórico REAL
da Binance (centenas de trades, vários regimes) e mede se a estratégia tem EDGE de
verdade depois de descontar taxa + slippage.

O que ele NÃO faz (limites honestos)
------------------------------------
- Não usa order-book imbalance nem funding (não existem no histórico de candles;
  só dão pra medir ao vivo). Ele testa o núcleo baseado em PREÇO (RSI/EMA/ATR/Bollinger),
  que é o coração dos setups de dip-buy e momentum.
- Assume, quando SL e TP caem no mesmo candle, que o STOP bateu primeiro (conservador).
- Preço de execução = abertura do candle seguinte ao sinal (sem look-ahead).

Como rodar
----------
    python backtest_v2.py                 # dip-buy, 120 dias, ~30 moedas
    python backtest_v2.py --estrategia momentum --dias 180
    python backtest_v2.py --risco 2 --fee 0.30

Precisa só de Python 3 (stdlib). Nada de instalar.
"""
import argparse, json, math, time, urllib.request
from datetime import datetime, timezone

BINANCE = "https://api.binance.com"

# Lista padrão de moedas líquidas (edite à vontade). Sem stablecoins.
SYMBOLS = ["BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","LINK","DOT","LTC","TRX",
           "ATOM","UNI","NEAR","APT","FIL","ARB","OP","INJ","SUI","SEI","AAVE","RUNE",
           "TIA","LDO","IMX","GALA","SAND","FTM"]

# ───────────────────────── coleta de dados ─────────────────────────
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=20))

def klines(symbol, interval="1h", dias=120):
    """Puxa candles paginando (limite 1000/req da Binance)."""
    ms_por_candle = {"1h": 3600_000, "4h": 14400_000, "15m": 900_000}[interval]
    fim = int(time.time() * 1000)
    ini = fim - dias * 86400_000
    linhas = []
    cursor = ini
    while cursor < fim:
        url = (f"{BINANCE}/api/v3/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cursor}&limit=1000")
        try:
            batch = _get(url)
        except Exception:
            break
        if not batch:
            break
        linhas += batch
        cursor = batch[-1][0] + ms_por_candle
        if len(batch) < 1000:
            break
        time.sleep(0.15)
    o = [float(x[1]) for x in linhas]; h = [float(x[2]) for x in linhas]
    l = [float(x[3]) for x in linhas]; c = [float(x[4]) for x in linhas]
    t = [int(x[0]) for x in linhas]
    return {"t": t, "o": o, "h": h, "l": l, "c": c}

# ───────────────────────── indicadores ─────────────────────────
def rsi(cl, p=14):
    r = [None]*len(cl)
    if len(cl) <= p: return r
    g = l = 0.0
    for i in range(1, p+1):
        d = cl[i]-cl[i-1]
        g += max(d, 0); l += max(-d, 0)
    g /= p; l /= p
    r[p] = 100 - 100/(1+(100 if l == 0 else g/l))
    for i in range(p+1, len(cl)):
        d = cl[i]-cl[i-1]
        g = (g*(p-1)+max(d, 0))/p
        l = (l*(p-1)+max(-d, 0))/p
        r[i] = 100 - 100/(1+(100 if l == 0 else g/l))
    return r

def atr(h, l, c, p=14):
    tr = [0.0]*len(c); a = [None]*len(c)
    for i in range(1, len(c)):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    if len(c) <= p: return a
    s = sum(tr[1:p+1]); a[p] = s/p
    for i in range(p+1, len(c)):
        a[i] = (a[i-1]*(p-1)+tr[i])/p
    return a

def ema(c, p):
    k = 2/(p+1); e = [c[0]]
    for i in range(1, len(c)):
        e.append(c[i]*k + e[i-1]*(1-k))
    return e

def bollinger(c, p=20, k=2):
    up = [None]*len(c); lo = [None]*len(c)
    for i in range(p-1, len(c)):
        janela = c[i-p+1:i+1]
        m = sum(janela)/p
        sd = math.sqrt(sum((x-m)**2 for x in janela)/p)
        up[i] = m + k*sd; lo[i] = m - k*sd
    return up, lo

def clamp(v, lo, hi): return min(max(v, lo), hi)

# ───────────────────────── motor de saída (ATR + moonbag 50/30/20) ─────────────────────────
def sim_trade(d, i0, lv, fee_rt):
    entry = d["o"][i0+1] if i0+1 < len(d["o"]) else None
    if not entry: return None
    f1 = mb = False; pico = p1 = p2 = 0.0
    W1, W2, WM = 0.5, 0.3, 0.2
    for i in range(i0+1, min(i0+1+24, len(d["c"]))):
        hi = (d["h"][i]-entry)/entry*100
        lo = (d["l"][i]-entry)/entry*100
        cl = (d["c"][i]-entry)/entry*100
        if not mb and not f1:
            if lo <= -lv["sl"]:                      # stop cheio (conservador: antes do TP)
                return -lv["sl"] - fee_rt
            if hi >= lv["tp1"]:
                f1 = True; p1 = lv["tp1"]; pico = cl  # vende 50% no TP1
        elif f1 and not mb:
            pico = max(pico, hi)
            if lo <= 0:
                return W1*p1 - fee_rt                 # runner volta pra entrada: breakeven
            if hi >= lv["tp2"]:
                mb = True; p2 = lv["tp2"]; pico = hi   # vende 30% no TP2, forma moonbag
            elif pico - cl >= lv["trail"]:
                return W1*p1 + (W2+WM)*max(cl, 0) - fee_rt
        else:  # moonbag (20%) correndo
            pico = max(pico, hi)
            if lo <= 0:
                return W1*p1 + W2*p2 - fee_rt
            if pico - cl >= lv["trail"]*1.5:
                return W1*p1 + W2*p2 + WM*max(cl, 0) - fee_rt
    last = (d["c"][min(i0+24, len(d["c"])-1)]-entry)/entry*100  # timeout 24h
    if mb:  return W1*p1 + W2*p2 + WM*last - fee_rt
    if f1:  return W1*p1 + (W2+WM)*max(last, 0) - fee_rt
    return last - fee_rt

# ───────────────────────── regras de entrada ─────────────────────────
def entradas(d, estrategia):
    R = rsi(d["c"]); A = atr(d["h"], d["l"], d["c"])
    up, lo = bollinger(d["c"]); E50 = ema(d["c"], 50)
    sinais = []; cd = 0
    for i in range(50, len(d["c"])-25):
        if cd > 0: cd -= 1; continue
        if R[i] is None or A[i] is None: continue
        atrp = A[i]/d["c"][i]*100
        if estrategia == "dip":
            ret3 = (d["c"][i]-d["c"][i-3])/d["c"][i-3]*100
            cond = (lo[i] is not None and R[i] < 40 and R[i] > R[i-1]
                    and d["c"][i] <= lo[i]*1.005 and ret3 > -8)
        else:  # momentum / breakout+tendência
            hh = max(d["h"][i-20:i])
            cond = (d["c"][i] > hh and 52 < R[i] < 72
                    and d["c"][i] > E50[i] and E50[i] > E50[i-10])
        if not cond: continue
        lv = {"sl": clamp(1.5*atrp, 2.5, 6), "tp1": clamp(1.5*atrp, 2.5, 6),
              "tp2": clamp(3*atrp, 5, 15), "trail": clamp(1.5*atrp, 2, 5)}
        sinais.append((i, lv, d["t"][i]))
        cd = 6
    return sinais

# ───────────────────────── métricas ─────────────────────────
def metricas(trades, capital, risco_pct):
    if not trades: return {"n": 0}
    pls = [t["pl"] for t in trades]
    wins = [p for p in pls if p > 0]; losses = [p for p in pls if p <= 0]
    sW = sum(wins); sL = abs(sum(losses))
    eq = capital; peak = capital; maxdd = 0.0; curva = []
    for p in pls:
        pos = min(capital*(risco_pct/100)/0.03, capital*0.2)  # risco fixo, teto 20%
        eq += p/100*pos; peak = max(peak, eq)
        maxdd = max(maxdd, (peak-eq)/peak*100); curva.append(eq)
    media = sum(pls)/len(pls)
    var = sum((p-media)**2 for p in pls)/len(pls)
    sd = math.sqrt(var) if var > 0 else 0
    sharpe_trade = (media/sd) if sd else 0
    ret = (eq-capital)/capital*100
    return {"n": len(trades), "win_rate": round(len(wins)/len(trades)*100, 1),
            "avg_win": round(sW/len(wins), 2) if wins else 0,
            "avg_loss": round(sL/len(losses), 2) if losses else 0,
            "expectancy_pct": round(media, 3),
            "profit_factor": round(sW/sL, 2) if sL > 0 else None,
            "retorno_%": round(ret, 1), "max_drawdown_%": round(maxdd, 1),
            "sharpe_por_trade": round(sharpe_trade, 3),
            "calmar": round(ret/maxdd, 2) if maxdd > 0 else None,
            "equity_final": round(eq, 0)}

def tabela(nome, m):
    if m.get("n", 0) == 0:
        print(f"  {nome}: sem trades"); return
    print(f"  {nome:<16} n={m['n']:<4} win={m['win_rate']}%  "
          f"PF={m['profit_factor']}  exp={m['expectancy_pct']}%/trade  "
          f"ret={m['retorno_%']}%  maxDD={m['max_drawdown_%']}%  "
          f"avgW={m['avg_win']} avgL={m['avg_loss']}  Sharpe={m['sharpe_por_trade']}")

# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--estrategia", choices=["dip", "momentum"], default="dip")
    ap.add_argument("--dias", type=int, default=120)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--risco", type=float, default=3.0, help="%% de risco por trade")
    ap.add_argument("--fee", type=float, default=0.30, help="taxa+slippage round-trip %%")
    args = ap.parse_args()

    print(f"\n=== BACKTEST v2 | estrategia={args.estrategia} | {args.dias} dias | "
          f"{args.interval} | risco={args.risco}% | fee_rt={args.fee}% ===\n")
    todos = []; usados = 0
    for s in SYMBOLS:
        sym = s + "USDT"
        d = klines(sym, args.interval, args.dias)
        if len(d["c"]) < 250:
            continue
        usados += 1
        for i, lv, ts in entradas(d, args.estrategia):
            pl = sim_trade(d, i, lv, args.fee)
            if pl is not None:
                todos.append({"sym": sym, "t": ts, "pl": pl})
        print(f"  ...{sym} ok ({len(d['c'])} candles)")

    todos.sort(key=lambda x: x["t"])
    if not todos:
        print("\nNenhum trade gerado. Ajuste os parametros.\n"); return
    corte = todos[int(len(todos)*0.7)]["t"]  # 70% treino / 30% teste no tempo
    treino = [t for t in todos if t["t"] < corte]
    teste  = [t for t in todos if t["t"] >= corte]
    dur = (todos[-1]["t"]-todos[0]["t"])/86400_000

    print(f"\n--- RESULTADO ({usados} moedas, ~{dur:.0f} dias, {len(todos)} trades) ---")
    tabela("GERAL", metricas(todos, args.capital, args.risco))
    tabela("Treino (70%)", metricas(treino, args.capital, args.risco))
    tabela("Teste OOS (30%)", metricas(teste, args.capital, args.risco))
    print("\nLeitura: PF>1 e expectancy>0 no TESTE (fora da amostra) = candidato a edge.")
    print("PF<1 ou expectancy<0 = NAO pôr dinheiro real. Custos incluidos.\n")

if __name__ == "__main__":
    main()
