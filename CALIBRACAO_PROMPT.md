# Prompt de Auto-Calibração do Crypto Screener

> Este arquivo é um prompt para o Claude analisar os resultados históricos e sugerir
> ajustes precisos no screener.py baseados em estatística, não em intuição.
>
> **Como usar:** Cole este texto numa conversa com Claude + o conteúdo de `signal_history.json`
> e o conteúdo de `calibracao_resultado.json` (gerado pelo `auto_calibracao.py`).

---

## Contexto do sistema

Você é um quantitative analyst especializado em crypto. Você criou um screener
de sinais de compra para Binance Spot (USDT pairs). O screener analisa 50 moedas
usando os seguintes indicadores:

**Indicadores e seus scores atuais:**
| Indicador | Condição | Score atual |
|-----------|----------|-------------|
| RSI | < 35 (oversold) | +2 |
| RSI | < 40 | +1 |
| RSI | > 60 | -1 |
| RSI | > 70 | -2 |
| EMA spread | EMA9/EMA21 > 1% | +2 |
| EMA spread | EMA9/EMA21 > 0.5% | +1 |
| Tendência | EMA21 > EMA50 | +1 |
| Preço | > EMA50 | +1 |
| Volume | Ratio > 2x média | +1 |
| Divergência RSI | Bullish | +2 |
| Padrão vela | Hammer / Engulfing | +1 |
| Multi-Timeframe | TF superior confirma (score >= 2) | +2 |
| Multi-Timeframe | TF superior contradiz (score <= -2) | -3 |
| Market Structure | HH/HL (bullish) | +1 |
| Market Structure | LL/LH (bearish) | -2 |
| Meme sector | DOGE/SHIB/PEPE/FLOKI/WIF/BONK/NOT | -2 |
| Força Relativa | > 3% vs BTC (12 candles) | +2 |
| Força Relativa | > 1.5% vs BTC | +1 |
| Força Relativa | < -1.5% vs BTC | -1 |
| Efficiency Ratio | > 0.4 (tendência limpa) | +1 |
| Efficiency Ratio | < 0.2 (lateral) | -1 |
| Volume Z-Score | > 2.0 (anômalo) | +1 |
| VWAP | Preço acima | +1 |
| VWAP | Preço abaixo | -1 |
| ATR Expansion | ATR atual > 1.3x ATR base | +1 |
| RSI Recovery | Saindo de oversold < 40, slope positivo | +2 |
| Bollinger Bands | Squeeze (banda < 6%) | +1 |
| Bollinger Bands | Breakout superior | +2 |
| Order Book | Imbalance > +0.30 (compradores) | +1 |
| Order Book | Imbalance < -0.30 (vendedores) | -1 |
| Fear & Greed | Medo Extremo (<= 25) — contrário | +1 |
| BTC Gatekeeper | Queda leve (-1% a -2% / 6 candles) | -1 |
| BTC Gatekeeper | Queda severa (< -2% / 6 candles) | -3 + bloqueia setup |
| Cascade | >= 3 moedas do mesmo setor sinalizando | +1 |

**Condições para Setup Completo (v5.0):**
- score >= 8
- criterios_ativos >= 3 (dos 10 critérios-chave)
- RSI < 58
- Preço > EMA50
- BTC não em queda severa
- MTF confirma (OBRIGATÓRIO — win rate = 0% sem ele)
- Setor != Meme

---

## Sua tarefa

Analise os dados fornecidos (`signal_history.json` e `calibracao_resultado.json`)
e responda **especificamente** às seguintes perguntas. Para cada resposta, forneça
o **número exato** baseado nos dados, não estimativas.

### 1. Validação das mudanças v5.0
Compare entradas que TERIAM passado pelos filtros v5.0 (score>=8, MTF alta, não Meme)
contra as que não passariam. Qual é a diferença na taxa de acerto em +1h e +4h?

### 2. Peso ótimo de cada indicador
Para cada indicador, calcule:
- Taxa de acerto em +1h quando o indicador está presente vs ausente
- Diferença de retorno médio (presente - ausente)
- Sugestão de score: deve subir, descer ou permanecer igual?

Use este critério:
- Se presente → win rate > 45%: manter ou aumentar score
- Se presente → win rate < 35%: diminuir score
- Se ausente → win rate maior que com presença: esse indicador está sendo mal usado

### 3. Identificar indicadores falsos-positivos
Quais sinais aparecem frequentemente nos PERDEDORES mas raramente nos vencedores?
Esses sinais estão inflando o score artificialmente. Sugestão: remover ou negativar.

### 4. Combinação ótima de 3 indicadores
Qual a combinação de 3 indicadores que, juntos, produz a maior taxa de acerto em +1h?
(Use as combinações testadas no calibracao_resultado.json como ponto de partida,
mas vá além testando outras combinações não exploradas.)

### 5. Threshold ótimo de score
Dado os dados atuais, qual threshold de score (7, 8, 9, 10) maximiza:
- (win_rate × media_retorno) — uma função que considera tanto acerto quanto magnitude

### 6. Detecção de regime de mercado
O screener usa Fear & Greed como proxy de sentimento. Mas os dados mostram que
mesmo com Fear Extremo (F&G=14-23), o mercado estava caindo.
Proponha uma regra adicional de filtro de regime baseada nos dados disponíveis
(sem adicionar novos endpoints de API) que bloquearia sinais em mercados em
tendência de baixa.

### 7. Cooldown ótimo por moeda
RENDER foi sinalizado 10x em 12h, sempre no mesmo preço. O cooldown atual é 4h.
Baseado nos dados, qual seria o cooldown ótimo para maximizar sinais únicos sem perder
oportunidades reais? (Considere que EIGEN foi sinalizado 2x com preços diferentes
e ambas tinham contexto diferente.)

### 8. Construir novo scorecards de critérios
Os 10 critérios atuais para `criterios_ativos` são:
1. Divergência RSI bullish
2. Market structure bullish
3. Forca relativa > 1.5%
4. Volume anômalo (ratio > 2 OR zscore > 2)
5. Efficiency Ratio > 0.4
6. Preço > VWAP
7. RSI Recovery
8. Order Book > +0.30
9. MTF confirma
10. Bollinger Bands (squeeze ou breakout)

Baseado no win rate individual de cada critério, reorganize-os em ordem de
relevância preditiva. Deveríamos adicionar algum critério? Remover algum?

---

## Formato de resposta esperado

Para cada pergunta, forneça:
```
### Pergunta N — [título]
Resultado: [número ou conclusão específica]
Mudança sugerida em screener.py: [linha específica a alterar]
Impacto esperado: [melhoria estimada em win rate]
```

Ao final, forneça um bloco de código Python com TODAS as mudanças sugeridas
para `screener.py`, prontas para copiar e colar.

---

## Princípios de calibração quantitativa

1. **Nunca confie em intuição quando tem dados.** Se os dados dizem que
   "Tendência Alta" não prediz bem, abaixe o score mesmo que pareça contraintuitivo.

2. **Correlação não é causalidade.** "Fear & Greed Medo Extremo" aparece em quase
   todos os registros — isso não significa que ele prediz bem, significa que o
   mercado estava consistentemente em medo durante o período de teste.

3. **Overfitting é o inimigo.** Com 48 amostras, ajustes muito específicos podem
   ser ruído. Só mude scores se a diferença de win rate for > 10 pontos percentuais
   E a amostra for >= 5 entradas.

4. **O threshold ótimo balanceia precision e recall.** Score alto = menos sinais
   mas mais precisos. Score baixo = mais sinais mas mais ruído. O ponto ótimo
   depende de quanto capital você arrisca por entrada.

5. **Regime importa mais que indicadores individuais.** Se o mercado está em
   tendência de baixa sistêmica (BTC caindo 5% em 24h), NENHUM sinal bullish
   de altcoin é confiável. O screener precisa de um "circuit breaker" de macro.

6. **Estatística Bayesiana:** Com poucos dados (< 100), use o prior de que
   nenhum indicador individual é confiável. Só confie em combinações de 2+
   indicadores que mostram consistência.

---

## Dados para análise

Cole aqui o conteúdo de `signal_history.json`:
```json
[COLAR AQUI]
```

Cole aqui o conteúdo de `calibracao_resultado.json`:
```json
[COLAR AQUI]
```
