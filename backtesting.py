from datetime import datetime
from screener import get_klines, calcular_rsi, calcular_ema


def backtest(symbol: str, interval: str = "1h", rsi_compra: float = 35,
             rsi_venda: float = 65, stop_loss_pct: float = 5.0,
             capital_inicial: float = 1000.0) -> dict:
    """
    Simula uma estratégia simples de compra/venda baseada em RSI.
    Compra quando RSI cai abaixo de rsi_compra.
    Vende quando RSI sobe acima de rsi_venda ou preço cai stop_loss_pct%.
    """
    klines = get_klines(symbol, interval=interval, limit=500)
    if not klines or len(klines) < 100:
        return {"erro": "Dados insuficientes para backtesting."}

    closes = [float(k[4]) for k in klines]
    timestamps = [k[0] for k in klines]

    trades = []
    posicao = None
    capital = capital_inicial

    for i in range(50, len(closes)):
        janela = closes[:i + 1]
        rsi = calcular_rsi(janela[-50:])
        preco = closes[i]

        if posicao is None:
            if rsi < rsi_compra:
                posicao = {
                    "preco_entrada": preco,
                    "ts_entrada": timestamps[i],
                    "quantidade": capital / preco,
                }
        else:
            variacao = (preco - posicao["preco_entrada"]) / posicao["preco_entrada"] * 100

            if rsi > rsi_venda or variacao <= -stop_loss_pct:
                capital = posicao["quantidade"] * preco
                motivo = "RSI alto — venda técnica" if rsi > rsi_venda else f"Stop Loss (-{stop_loss_pct}%)"
                trades.append({
                    "entrada": posicao["preco_entrada"],
                    "saida": preco,
                    "ganho_pct": round(variacao, 2),
                    "motivo": motivo,
                    "data_entrada": datetime.fromtimestamp(posicao["ts_entrada"] / 1000).strftime("%d/%m %H:%M"),
                    "data_saida": datetime.fromtimestamp(timestamps[i] / 1000).strftime("%d/%m %H:%M"),
                    "resultado": "✅ Lucro" if variacao > 0 else "❌ Perda",
                })
                posicao = None

    if not trades:
        return {
            "symbol": symbol,
            "interval": interval,
            "total_trades": 0,
            "win_rate": 0,
            "retorno_total": 0,
            "capital_final": round(capital_inicial, 2),
            "capital_inicial": capital_inicial,
            "media_ganho": 0,
            "media_perda": 0,
            "melhor_trade": 0,
            "pior_trade": 0,
            "trades": [],
            "aviso": "Nenhuma oportunidade encontrada com esses parâmetros nos dados históricos.",
        }

    ganhos = [t for t in trades if t["ganho_pct"] > 0]
    perdas = [t for t in trades if t["ganho_pct"] <= 0]

    return {
        "symbol": symbol,
        "interval": interval,
        "total_trades": len(trades),
        "win_rate": round(len(ganhos) / len(trades) * 100, 1),
        "retorno_total": round((capital - capital_inicial) / capital_inicial * 100, 2),
        "capital_inicial": capital_inicial,
        "capital_final": round(capital, 2),
        "media_ganho": round(sum(t["ganho_pct"] for t in ganhos) / len(ganhos), 2) if ganhos else 0,
        "media_perda": round(sum(t["ganho_pct"] for t in perdas) / len(perdas), 2) if perdas else 0,
        "melhor_trade": max(t["ganho_pct"] for t in trades),
        "pior_trade": min(t["ganho_pct"] for t in trades),
        "trades": trades[-20:],
    }
