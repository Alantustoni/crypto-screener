# 2A Crypto — Auditoria + Veredito de Edge

*Análise técnica completa e teste de estratégia com dados reais. Julho/2026.*

---

## 1. Veredito em uma frase

**A estratégia ainda não tem edge comprovado. Num teste honesto com dados reais e custos, o núcleo baseado em preço perde dinheiro. Não coloque dinheiro real agora.** O que o painel mostra (+$47, PF 2,12, 54% de acerto) vem de ~48 trades simulados de poucos dias — amostra pequena e favorável. Quando testei centenas de trades ao longo de semanas, o resultado inverte.

Isso não é um "não" definitivo à ideia. É um "não com dinheiro real até provar edge fora da amostra". A diferença entre os dois é o seu R$1.000.

---

## 2. O teste que fiz (e por que dá pra confiar mais nele)

Puxei candles reais da Binance de 25 moedas líquidas (~35 dias, 1h) e reproduzi o **núcleo** dos seus setups, descontando **0,30% de taxa+slippage** por trade (ida e volta) e usando a mesma lógica de saída do sistema (SL/TP por ATR + moonbag 50/30/20 + trailing + timeout 24h). Execução no candle seguinte ao sinal (sem trapaça de look-ahead).

| Estratégia | Trades | Acerto | Profit Factor | Expectativa/trade | Retorno | Max Drawdown |
|---|---|---|---|---|---|---|
| **Dip-buy** (seu foco atual) | 347 | 43,8% | **0,56** | **−0,62%** | **−43%** | 44% |
| Dip-buy — janela recente (30%) | 92 | 44,6% | 0,53 | −0,60% | −11% | 11% |
| **Momentum/tendência** | 289 | 48,8% | **0,65** | **−0,44%** | −26% | 27% |
| Momentum — janela recente (30%) | 110 | 51,8% | 0,95 | −0,05% | −1,2% | 6,7% |

Leitura: **Profit Factor abaixo de 1 = estratégia perdedora.** O dip-buy perde claramente. O momentum perde menos e quase empata na janela recente, mas ainda não é lucro. Para valer a pena com dinheiro real, você quer PF **acima de 1,3–1,5** de forma consistente, fora da amostra.

### Por que perde (o problema estrutural)
Em **ambas** as estratégias: **perda média (~2,5%) é MAIOR que ganho médio (~1,7–2,0%)** e o acerto fica **abaixo de 50%**. Isso é expectativa negativa matemática. As causas:

1. **As saídas cortam o ganhador cedo demais.** O TP1 vende 50% em ~ATR e o trailing fecha o resto rápido, então o ganho médio fica pequeno — enquanto o perdedor vai até o stop cheio. Em cripto você precisa do contrário: deixar o ganhador correr MUITO (cauda gorda) pra pagar vários stops pequenos.
2. **Overtrading.** 347 trades em 35 dias × 0,30% de custo = mais de 100% do capital gasto em taxa/slippage no período. Cada trade a mais só te sangra se não houver edge. Menos trades, mais seletivos.
3. **Comprar fraqueza (dip-buy) pega faca caindo.** Comprar RSI baixo em queda é o clássico que perde líquido — confirmado nos números.

### Testei "deixar o ganhador correr" — e NÃO resolveu

Minha primeira hipótese foi que o problema era a saída cortar o ganhador cedo. Testei 4 estruturas de saída diferentes (vender menos no TP1, stop no zero, trailing largo, segurar até 72h) nas duas entradas. Resultado (dados reais, com custos):

| Saída | DIP geral | DIP fora-amostra | MOM geral | MOM fora-amostra |
|---|---|---|---|---|
| Atual (50/30/20) | PF 0,52 | PF 0,45 | PF 0,32 | PF 0,21 |
| Corre 70% (trail 2,5×) | PF 0,41 | PF 0,27 | PF 0,27 | PF 0,18 |
| Corre 75% (trail 3×) | PF 0,46 | PF 0,15 | PF 0,22 | PF 0,16 |
| 100% trailing (72h) | PF 0,58 | PF 0,15 | PF 0,23 | PF 0,14 |

**Todas perdem. Nenhuma passou nem perto de PF 1.** Deixar o ganhador correr até piorou em vários casos. A lição é dura e importante: **não dá pra consertar uma entrada ruim com uma saída boa.** Quando o sinal de entrada não prevê o movimento futuro melhor que o acaso (depois de custos), nenhuma gestão de saída cria lucro. O gargalo não é a saída — é a **entrada não ter vantagem preditiva**.

---

## 3. Por que o painel diz o contrário?

Três motivos, e nenhum deles é mentira do código:

- **Amostra minúscula e recente.** 48 trades em poucos dias, num período que calhou de ser favorável. 347 trades contam outra história. Isso é a diferença entre "tive sorte esta semana" e "tenho vantagem".
- **Filtros ao vivo que o backtest não vê.** Order-book imbalance e funding não existem no histórico — só dá pra medir ao vivo. É *possível* que eles adicionem seletividade que meu teste não captura. Mas isso é uma hipótese a **provar**, não a assumir.
- **Paper trading é otimista por natureza.** Sem competir por liquidez real, sem latência, com preenchimento ideal. O real sempre rende menos.

Conclusão justa: meu teste **não prova** que o sistema completo perde (ele omite os filtros ao vivo), mas **prova que o edge não está demonstrado** e que o núcleo de preço é perdedor. Isso já basta para a decisão: **validar antes de arriscar.**

---

## 4. Estado técnico do código (auditoria)

O que **melhorou** nesta fase (está sólido):

- Cálculo de posição/P&L unificado em `trading_math.py` (fonte única — acabou a divergência entre telas).
- Persistência com lock e id `max+1` (sem corrida/colisão).
- Segurança da API endurecida (auth fail-closed, `compare_digest`).
- Bug do "breakeven" que registrava perda real como zero — **corrigido**.
- Enxurrada de recalibração (contador que não zerava no Postgres) — **corrigido**.
- Suíte de testes stdlib cobrindo o essencial.

O que ainda **bloqueia dinheiro real** (por ordem):

1. **Sem edge comprovado** (seção 2) — é o bloqueador nº 1.
2. **Scan síncrono no event loop** (`loop_scan_automatico` roda `requests` bloqueante). Trava a API a cada ciclo. Com dinheiro real, uma API travada na hora errada é caro. Migrar pra execução em executor/async.
3. **Zero backtest embutido antes desta entrega.** Você estava calibrando parâmetros sobre ~50 trades — isso é *overfitting* garantido (ajustar ao ruído). Calibração só faz sentido sobre centenas de trades e validada fora da amostra.
4. **Token do Telegram exposto no histórico do Git.** Rotacionar no @BotFather.
5. **Sem gestão de risco de portfólio de verdade** (perda máxima diária/semanal que *para de operar*, correlação entre posições — 16 posições abertas em alts caem todas juntas num tombo do BTC).

---

## 5. Caminho realista (o que eu faria, nesta ordem)

**Passo 1 — Provar edge antes de qualquer centavo.** Use o `backtest_v2.py` que deixei. Rode com mais histórico e as duas estratégias:
```
python backtest_v2.py --estrategia momentum --dias 180
python backtest_v2.py --estrategia dip --dias 180
```
Meta objetiva: só avança quem tiver **PF > 1,3 e expectativa > 0 no TESTE (30% fora da amostra)**. Se nenhuma passar como está, o trabalho vira *encontrar* uma que passe — não colocar dinheiro.

**Passo 2 — O foco é a ENTRADA, não a saída.** Já testei reformar a saída e não resolve (seção 2). O trabalho de verdade é achar um sinal de entrada que preveja o movimento melhor que o acaso, líquido de custos. Isso é o que 95% dos que tentam não conseguem — e é honesto encarar isso de frente. Duas frentes concretas pra testar (cada uma com o `backtest_v2.py`, barra PF > 1,3 fora da amostra):
- **A única hipótese ainda não testada: os filtros ao vivo** (order-book imbalance, funding). Eles não entram no meu backtest porque não existem no histórico. Se existe edge no seu sistema, é aqui que ele estaria. Só dá pra provar coletando esses dados ao vivo por semanas e medindo se os trades filtrados por eles batem os não-filtrados. Vale montar essa coleta.
- **Timeframe/universo diferentes** (ex.: 4h em vez de 1h reduz custo relativo e ruído; ou focar só nas 3–5 moedas mais líquidas). Testar se a expectativa muda de sinal.

**Passo 3 — Menos trades, mais convicção.** Subir o piso de qualidade do sinal e medir se a expectativa por trade sobe. Frequência alta só ajuda se cada trade tiver vantagem — e a 0,30% de custo, ela sangra.

**Passo 4 — Gestão de risco de portfólio.** Perda máxima diária que trava o robô, teto de posições correlacionadas, tamanho por volatilidade (já tem sizing por risco — falta o freio de portfólio).

**Passo 5 — Só então, dinheiro real pequeno.** Começar com o mínimo, comparar o real com o backtest por semanas, e escalar só se baterem.

---

## 6. Ferramentas de mercado reais que valem adotar

Nada mágico — o que profissionais de fato usam:

- **ccxt** — biblioteca única para dados e execução em dezenas de corretoras (troca Binance por outra sem reescrever).
- **pandas-ta** ou **TA-Lib** — indicadores testados e rápidos (menos bug que reimplementar à mão).
- **vectorbt** ou **backtesting.py** — backtest vetorizado + varredura de parâmetros + walk-forward de verdade (muito além do meu script didático).
- **Métricas certas** — Sharpe, Sortino, Calmar, max drawdown, expectancy. Retorno sem drawdown engana.
- **Walk-forward / validação fora da amostra** — o único jeito honesto de não se enganar com overfitting.

---

## 7. Sobre a meta de R$80–100/dia

Com R$1.000, isso é **8–10% ao dia**. Nenhum fundo, banco ou trader do mundo sustenta isso — os melhores fazem 20–40% ao **ano**. Perseguir meta diária alta força posição grande e overtrading, que é exatamente como se zera uma conta. 

A meta saudável não é "quanto ganho por dia", é: **tenho edge comprovado fora da amostra? meu risco por trade preserva o capital nos meses ruins?** Se sim, o lucro vem da composição ao longo do tempo — devagar, com meses no vermelho no meio. Se não, nenhuma meta diária muda a matemática; só acelera a perda.

Eu topo construir isso com você do jeito certo: provar o edge primeiro, blindar o risco, e só então falar em dinheiro real. É mais lento, mas é o único caminho que não termina com você perdendo os R$1.000.

---

*Método e limites do teste estão documentados no topo de `backtest_v2.py`. Números da tabela vieram de dados reais da Binance puxados no seu navegador em julho/2026; reexecute o script para atualizar e ampliar a amostra.*
