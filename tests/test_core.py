"""
Suíte de testes do Crypto Screener — stdlib unittest (sem dependências extras).

Rodar da pasta do projeto:
    python -m unittest discover -s tests -v

Cobre:
  - trading_math: posição e P&L (constantes canônicas 3/3/6)
  - screener: indicadores puros (RSI, EMA, ATR, VWAP, z-score) — sem rede
  - signal_history: geração de id (max+1) e round-trip de persistência SQLite
  - concorrência: escritas paralelas não perdem dados nem colidem ids

Os testes de persistência usam um DATA_DIR temporário e SQLite (sem DATABASE_URL),
portanto NÃO tocam no banco real nem na rede.
"""
import os
import sys
import tempfile
import threading
import unittest

# Garante que o pacote raiz do projeto esteja no path e força SQLite temporário
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.pop("DATABASE_URL", None)          # força backend SQLite
_TMP = tempfile.mkdtemp(prefix="cs_test_")
os.environ["DATA_DIR"] = _TMP                 # isola o banco de teste

import trading_math as tm                      # noqa: E402
import screener as sc                          # noqa: E402
import signal_history as sh                    # noqa: E402


class TestTradingMath(unittest.TestCase):
    def test_posicao_multiplicadores(self):
        self.assertEqual(tm.calcular_posicao(8, 20, 1000), 20)    # 1x
        self.assertEqual(tm.calcular_posicao(9, 20, 1000), 30)    # 1.5x
        self.assertEqual(tm.calcular_posicao(10, 20, 1000), 40)   # 2x

    def test_posicao_teto_10pct(self):
        # score 10 → 40, mas teto 10% de 200 = 20
        self.assertEqual(tm.calcular_posicao(10, 20, 200), 20)

    def test_posicao_score_none_usa_default(self):
        self.assertEqual(tm.calcular_posicao(None, 20, 1000), 20)

    def test_pl_stop(self):
        h = {"stop_automatico": True, "stop_automatico_pct": -3.0}
        self.assertAlmostEqual(tm.pl_simulado(h, 20, custo_rt_pct=0.0), -0.6)

    def test_pl_tp2_canonico(self):
        h = {"tp2_automatico": True, "tp1_automatico_pct": 3, "tp2_automatico_pct": 6}
        esperado = 3 / 100 * 20 * 0.5 + 6 / 100 * 20 * 0.5
        self.assertAlmostEqual(tm.pl_simulado(h, 20, custo_rt_pct=0.0), esperado)

    def test_pl_aberta_sem_checkpoint_none(self):
        self.assertIsNone(tm.pl_simulado({}, 20))

    def test_pl_aberta_com_checkpoint(self):
        self.assertAlmostEqual(tm.pl_simulado({"resultado_1h": 2.0}, 20, custo_rt_pct=0.0),
                               2.0 / 100 * 20)

    def test_pl_stop_por_checkpoint(self):
        # algum checkpoint <= -3 → considera stop
        self.assertAlmostEqual(tm.pl_simulado({"resultado_1h": -4.0}, 20, custo_rt_pct=0.0),
                               -3.0 / 100 * 20)


class TestSaidaATR(unittest.TestCase):
    def test_niveis_escalam_com_atr(self):
        n = tm.niveis_saida(2.0)   # ATR 2% → sl/tp1 = 1.5×2 = 3.0 ; tp2 = 3×2 = 6.0
        self.assertAlmostEqual(n["exit_sl"], 3.0)
        self.assertAlmostEqual(n["exit_tp1"], 3.0)
        self.assertAlmostEqual(n["exit_tp2"], 6.0)

    def test_niveis_clamp_piso(self):
        n = tm.niveis_saida(0.1)   # ATR minúsculo → aplica pisos
        self.assertEqual(n["exit_sl"], 2.5)
        self.assertEqual(n["exit_tp2"], 5.0)

    def test_niveis_clamp_teto(self):
        n = tm.niveis_saida(10.0)  # ATR enorme → aplica tetos
        self.assertEqual(n["exit_sl"], 6.0)
        self.assertEqual(n["exit_tp2"], 15.0)

    def test_breakeven_piso_mais_alto(self):
        # ATR baixo → breakeven arma no piso 1.5% (não mais 1.0%)
        self.assertEqual(tm.niveis_saida(1.0)["exit_be"], 1.5)
        # e sempre abaixo do TP1 (arma antes do alvo, não junto)
        n = tm.niveis_saida(2.0)
        self.assertLess(n["exit_be"], n["exit_tp1"])

    def test_niveis_atr_invalido_usa_default(self):
        for v in (None, 0, -1, "x"):
            n = tm.niveis_saida(v)
            self.assertIn("exit_sl", n)   # não quebra; usa a=1.5

    def test_custo_reduz_pl(self):
        h = {"stop_automatico": True, "stop_automatico_pct": -3.0}
        bruto = tm.pl_simulado(h, 20, custo_rt_pct=0.0)
        liquido = tm.pl_simulado(h, 20, custo_rt_pct=0.3)
        self.assertAlmostEqual(liquido, bruto - 20 * 0.003)

    def test_pl_usa_niveis_do_sinal(self):
        # exit_tp2=12 respeitado no rateio 50/30/20 (não o default 6)
        h = {"exit_tp1": 3, "exit_tp2": 12, "resultado_1h": 13}
        esperado = (3 * 0.5 + 12 * 0.3 + 13 * 0.2) / 100 * 20
        self.assertAlmostEqual(tm.pl_simulado(h, 20, custo_rt_pct=0.0), esperado)


class TestMoonbag(unittest.TestCase):
    def test_moonbag_fechado_50_30_20(self):
        h = {"tp1_automatico_pct": 3, "tp2_automatico_pct": 6,
             "moonbag_automatico": True, "moonbag_automatico_pct": 10}
        esperado = (3 * 0.5 + 6 * 0.3 + 10 * 0.2) / 100 * 20   # = 1.06
        self.assertAlmostEqual(tm.pl_simulado(h, 20, custo_rt_pct=0.0), esperado)

    def test_moonbag_ativo_marca_a_mercado(self):
        # TP2 batido, 20% ainda correndo → usa último checkpoint p/ o moonbag
        h = {"tp1_automatico_pct": 3, "tp2_automatico_pct": 6,
             "moonbag_ativo": True, "resultado_1h": 8}
        esperado = (3 * 0.5 + 6 * 0.3 + 8 * 0.2) / 100 * 20
        self.assertAlmostEqual(tm.pl_simulado(h, 20, custo_rt_pct=0.0), esperado)

    def test_runner_trailing_antes_do_tp2_eh_50_50(self):
        h = {"tp1_automatico_pct": 3, "tp2_automatico": True, "tp2_automatico_pct": 2.5}
        esperado = (3 * 0.5 + 2.5 * 0.5) / 100 * 20
        self.assertAlmostEqual(tm.pl_simulado(h, 20, custo_rt_pct=0.0), esperado)

    def test_stop_apos_tp1_nao_zera_o_banco(self):
        # Stop depois do TP1: 50% já foi vendido no TP1; só os outros 50% stopam
        h = {"tp1_automatico": True, "tp1_automatico_pct": 3,
             "stop_automatico": True, "stop_automatico_pct": -1}
        esperado = (3 * 0.5 + (-1) * 0.5) / 100 * 20   # = 0.2 (positivo!)
        self.assertAlmostEqual(tm.pl_simulado(h, 20, custo_rt_pct=0.0), esperado)


class TestIndicadores(unittest.TestCase):
    def test_rsi_todo_subindo_alto(self):
        closes = [float(i) for i in range(1, 40)]   # monotônico crescente
        self.assertGreater(sc.calcular_rsi(closes), 90)

    def test_rsi_todo_caindo_baixo(self):
        closes = [float(i) for i in range(40, 1, -1)]
        self.assertLess(sc.calcular_rsi(closes), 10)

    def test_rsi_curto_retorna_neutro(self):
        self.assertEqual(sc.calcular_rsi([1, 2, 3]), 50.0)

    def test_ema_mantem_tamanho(self):
        vals = [float(i) for i in range(1, 60)]
        ema = sc.calcular_ema(vals, 21)
        self.assertEqual(len(ema), len(vals))

    def test_ema_constante(self):
        vals = [10.0] * 60
        ema = sc.calcular_ema(vals, 21)
        self.assertAlmostEqual(ema[-1], 10.0, places=6)

    def test_atr_nao_negativo(self):
        highs = [10 + i * 0.1 for i in range(30)]
        lows = [9 + i * 0.1 for i in range(30)]
        closes = [9.5 + i * 0.1 for i in range(30)]
        self.assertGreaterEqual(sc.calcular_atr(highs, lows, closes), 0)

    def test_vwap_positivo(self):
        # klines: [t, o, h, l, c, v, ...]
        klines = [[0, 10, 11, 9, 10, 100]] * 30
        self.assertAlmostEqual(sc.calcular_vwap(klines), 10.0, places=4)

    def test_zscore_volume_pico(self):
        # Janela histórica com variação real (desvio > 0) + último candle em pico.
        # (z-score é indefinido se a janela tiver variância zero → função retorna 0.)
        vols = [100.0, 105.0, 95.0, 110.0, 90.0] * 4 + [500.0]   # 20 variados + pico
        self.assertGreater(sc.calcular_zscore_volume(vols), 2.0)

    def test_zscore_volume_janela_constante_retorna_zero(self):
        # Comportamento correto: sem variância histórica, z-score indefinido → 0.0
        self.assertEqual(sc.calcular_zscore_volume([100.0] * 25 + [1000.0]), 0.0)


class TestProximoId(unittest.TestCase):
    def test_incremental_simples(self):
        hist = [{"id": 1}, {"id": 2}, {"id": 5}]
        self.assertEqual(sh._proximo_id(hist), 6)   # max+1, não len+1

    def test_lista_vazia(self):
        self.assertEqual(sh._proximo_id([]), 1)

    def test_ids_ausentes_ou_none(self):
        hist = [{"id": None}, {}, {"id": 3}]
        self.assertEqual(sh._proximo_id(hist), 4)

    def test_nao_colide_apos_remocao(self):
        # Cenário do bug antigo: len+1 reusaria um id existente
        hist = [{"id": 1}, {"id": 2}, {"id": 3}]
        del hist[1]                                  # sobra id 1 e 3 (len=2)
        self.assertEqual(sh._proximo_id(hist), 4)    # correto; len+1 daria 3 (colisão)


class TestPersistenciaRoundTrip(unittest.TestCase):
    def setUp(self):
        # Limpa o banco de teste antes de cada caso
        sh.salvar_historico([])

    def test_salvar_e_carregar(self):
        entrada = {"id": 1, "timestamp": "2026-07-01T10:00:00",
                   "symbol": "BTCUSDT", "setup_completo": True}
        sh.salvar_historico([entrada])
        carregado = sh.carregar_historico()
        self.assertEqual(len(carregado), 1)
        self.assertEqual(carregado[0]["symbol"], "BTCUSDT")

    def test_registrar_manual_gera_id_unico(self):
        sh.salvar_historico([])
        e1 = sh.registrar_manual("BTC", 100.0)
        e2 = sh.registrar_manual("ETH", 50.0)
        self.assertNotEqual(e1["id"], e2["id"])
        self.assertEqual(len(sh.carregar_historico()), 2)


class TestConcorrencia(unittest.TestCase):
    def test_escritas_paralelas_sem_perda(self):
        """20 threads inserindo em paralelo: todas as entradas persistem e os
        ids são únicos (o lock evita lost update + colisão)."""
        sh.salvar_historico([])
        n = 20

        def inserir(i):
            sh.registrar_manual("SYM{}".format(i), float(i + 1))

        threads = [threading.Thread(target=inserir, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        hist = sh.carregar_historico()
        self.assertEqual(len(hist), n, "todas as inserções devem persistir")
        ids = [h["id"] for h in hist]
        self.assertEqual(len(ids), len(set(ids)), "ids devem ser únicos")


class TestTuningAgressivo(unittest.TestCase):
    def test_teto_score_minimo(self):
        self.assertEqual(sc._SCORE_MIN_TETO, 5)
        sc._config_cache = {}   # invalida cache p/ forçar releitura
        cfg = sc.carregar_config_screener()
        self.assertLessEqual(cfg["score_minimo_setup"], 5,
                             "clamp deve manter score mínimo <= teto agressivo")

    def test_regime_minimo_reduzido(self):
        self.assertEqual(sh.REGIME_SCORE_MINIMO, 5)   # antes 10, depois 7, agora 5

    @staticmethod
    def _fake_result(symbol, score, sniper=False, reversao=False):
        return {
            "symbol": symbol, "preco": 100.0, "score": score, "rsi": 45,
            "setup_completo": True, "sniper_setup": sniper, "reversao_setup": reversao,
            "divergencia": None, "funding_sinal": None, "mtf_tendencia": "alta",
            "intervalo": "1h", "sinais": [], "market_structure": None,
            "forca_relativa": 2.0, "er": 0.5, "rsi_recovery": False,
            "padrao_vela": None, "rotation_rank": None, "criterios_ativos": 3,
            "setor": "L1", "setor_cascade": None, "shadow_rsi": None,
            "ob_imbalance": 0.4,
        }

    def _forcar_regime_bearish(self):
        orig_reg = sh._regime_bearish
        orig_cb = sh._circuit_breaker_sequencial_ativo
        sh._regime_bearish = lambda: True
        sh._circuit_breaker_sequencial_ativo = lambda *a, **k: False
        self.addCleanup(setattr, sh, "_regime_bearish", orig_reg)
        self.addCleanup(setattr, sh, "_circuit_breaker_sequencial_ativo", orig_cb)

    def test_sniper_opera_durante_regime_bearish(self):
        # Sniper é qualificado por confluência → passa mesmo em regime bearish.
        self._forcar_regime_bearish()
        sh.salvar_historico([])
        n = sh.registrar_sinais([self._fake_result("SOLUSDT", 5, sniper=True)],
                                score_minimo=5)
        self.assertEqual(n, 1)

    def test_reversao_opera_com_score_baixo(self):
        # Dip-buy com score baixo (1) deve entrar — qualificado por confluência.
        self._forcar_regime_bearish()
        sh.salvar_historico([])
        n = sh.registrar_sinais([self._fake_result("AVAXUSDT", 1, reversao=True)],
                                score_minimo=5)
        self.assertEqual(n, 1)

    def test_faca_caindo_bloqueada(self):
        # Mesmo sendo dip-buy, score <= -3 ('faca caindo') é barrado pelo piso -2.
        self._forcar_regime_bearish()
        sh.salvar_historico([])
        n = sh.registrar_sinais([self._fake_result("XRPUSDT", -4, reversao=True)],
                                score_minimo=5)
        self.assertEqual(n, 0)

    def test_setup_normal_fraco_bloqueado(self):
        # Setup normal (não dip) com score 3 < piso base 5 → barrado.
        self._forcar_regime_bearish()
        sh.salvar_historico([])
        n = sh.registrar_sinais([self._fake_result("ADAUSDT", 3, sniper=False)],
                                score_minimo=5)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
