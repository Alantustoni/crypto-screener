# 2A Crypto Screener — sinais, backtest e auditoria de edge

[![tests](https://github.com/Alantustoni/crypto-screener/actions/workflows/tests.yml/badge.svg)](https://github.com/Alantustoni/crypto-screener/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Licença](https://img.shields.io/badge/licen%C3%A7a-MIT-green)

Screener de criptomoedas que varre os 100 pares mais líquidos da Binance, pontua cada um por um conjunto de indicadores, dispara alertas no Telegram, registra os sinais num banco e **mede o próprio desempenho** — com backtest, auto-calibração dos pesos e um relatório de auditoria.

> **O resultado mais importante deste projeto é negativo.** A auditoria com dados reais, custos descontados e validação fora da amostra concluiu que a estratégia **não tem edge comprovado** e não deve operar dinheiro real. O relatório completo está em [`AUDITORIA_E_EDGE_2A.md`](AUDITORIA_E_EDGE_2A.md). O sistema continua no ar coletando sinais para testar a única hipótese ainda aberta (filtros de order book e funding).

Rodou 24/7 no Railway entre junho e agosto de 2026, com 55 commits ao longo de seis semanas.

## O que o sistema faz

```
Binance (klines, funding, OI, order book)  ──►  screener.py  ──►  score por par
CoinGecko (dominância BTC)  ───────────────►        │
Fear & Greed  ──────────────────────────────►        ▼
                                          signal_history.py (SQLite / Postgres)
                                                     │
                     ┌───────────────────────────────┼─────────────────────────┐
                     ▼                               ▼                         ▼
             telegram_alert.py                 main.py (FastAPI)        auto_calibracao.py
             alerta 1:1 por sinal              dashboard + 30 endpoints   reajusta pesos pelo
             (sem rajada)                      login, analytics, edge     win rate observado
```

| Módulo | Linhas | Responsabilidade |
|---|---|---|
| `screener.py` | 1.3k | Indicadores (EMA, RSI, ATR, VWAP, z-score de volume, divergência RSI, market structure, efficiency ratio, CVD, OI, funding, order-book imbalance, força relativa vs BTC) e o score de cada par |
| `trading_math.py` | 170 | Dimensionamento por risco e volatilidade (teto de 20 % do capital), níveis SL/TP por ATR, breakeven, P&L líquido com custos, saída em moonbag 50/30/20 |
| `signal_history.py` | 900 | Persistência dos sinais: SQLite em dev, PostgreSQL no Railway (`DATABASE_URL`); verificação de resultado, circuit breaker, features por sinal |
| `main.py` | 1.3k | API FastAPI: scan, histórico, analytics, risco por posição, calibração, `/api/edge`, dashboard web com login por senha e cookie de sessão |
| `telegram_alert.py` | 660 | Alertas formatados, deduplicação por sinal, loop a cada 5 min |
| `auto_calibracao.py` | 500 | Recalcula pesos e thresholds a partir do win rate dos sinais decididos |
| `backtest_v2.py` | 250 | Backtest com execução no candle seguinte, 0,30 % de custo por trade, janela fora da amostra |

### Regras de proteção que estão no código

- **Pausa de regime**: se BTC está abaixo da EMA50 em 4h, o screener não opera.
- **Circuit breaker** em −7 % de drawdown.
- **Cooldown** de 4 h por moeda e penalidade para memecoins.
- **Empate (~0 %) não conta como prejuízo**; win rate só sobre sinais decididos.
- Teto de 20 % do capital por posição, independente do score.

## A auditoria (resumo)

Backtest com candles reais da Binance (25 moedas, 1h, ~35 dias; depois 30 moedas, 4h, 167 dias), custos de 0,30 % por trade e execução sem look-ahead:

| Estratégia | Trades | Acerto | Profit Factor | Retorno |
|---|---|---|---|---|
| Dip-buy | 347 | 43,8 % | 0,56 | −43 % |
| Momentum | 289 | 48,8 % | 0,65 | −26 % |

Profit factor abaixo de 1 é estratégia perdedora. Testei quatro estruturas de saída diferentes ("deixar o ganhador correr") e nenhuma inverteu o sinal. A coleta ao vivo chegou a mostrar um lead com 764 trades, que decaiu ao chegar em 839. Detalhes, tabelas e o que ainda falta testar: [`AUDITORIA_E_EDGE_2A.md`](AUDITORIA_E_EDGE_2A.md).

## Rodando localmente

```bash
pip install -r requirements.txt
cp screener_config.example.json screener_config.json
cp telegram_config.example.json telegram_config.json   # opcional; preencha o token do BotFather
export DASHBOARD_PASSWORD=uma-senha
uvicorn main:app --reload
```

Abra `http://localhost:8000`. Sem `DATABASE_URL`, o histórico vai para um SQLite local.

### Deploy

Railway com volume persistente ou Postgres: veja [`DEPLOY_RAILWAY.md`](DEPLOY_RAILWAY.md). Variáveis: `DASHBOARD_PASSWORD`, `INTERNAL_API_KEY`, `DATABASE_URL` (opcional), `DATA_DIR` (opcional).

## Testes

```bash
python -m pytest -q
```

53 testes cobrindo dimensionamento por risco, níveis por ATR, P&L com custos, moonbag, trailing, RSI/EMA/ATR/VWAP/z-score e as regras de proteção.

## Auto-calibração com LLM

[`CALIBRACAO_PROMPT.md`](CALIBRACAO_PROMPT.md) é o prompt usado para pedir a um modelo de linguagem que analise o histórico de sinais e proponha novos pesos — validação das mudanças, peso por indicador, falsos positivos e threshold de score. A proposta é aplicada por `auto_calibracao.py` e fica clampada aos tetos de segurança.

## Como foi construído

Desenvolvido com apoio de Claude Code e ChatGPT, de junho a agosto de 2026, em ciclos curtos: hipótese → código → coleta ao vivo → medição → ajuste. O commit `security: remove token exposto` (24/06) documenta um erro real: um token do Telegram foi commitado no início do projeto, revogado e removido do histórico.

## Licença

MIT — veja [LICENSE](LICENSE).
