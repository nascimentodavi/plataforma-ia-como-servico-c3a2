import pytest

from comum.resiliencia import (CircuitBreaker, CircuitoAberto, ErroPermanente,
                               ErroTransitorio, com_retentativa)


class Relogio:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def falhar():
    raise ErroTransitorio("fora do ar")


def test_circuito_abre_apos_limite_e_falha_rapido():
    cb = CircuitBreaker("x", limite_falhas=3, tempo_reset=10, relogio=Relogio())
    for _ in range(3):
        with pytest.raises(ErroTransitorio):
            cb.chamar(falhar)
    assert cb.estado == CircuitBreaker.ABERTO
    chamadas = []
    with pytest.raises(CircuitoAberto):
        cb.chamar(lambda: chamadas.append(1))
    assert chamadas == []  # nem tentou chamar o servico


def test_meio_aberto_fecha_com_sucesso_e_reabre_com_falha():
    relogio = Relogio()
    cb = CircuitBreaker("x", limite_falhas=1, tempo_reset=10, relogio=relogio)
    with pytest.raises(ErroTransitorio):
        cb.chamar(falhar)
    relogio.t = 11
    assert cb.estado == CircuitBreaker.MEIO_ABERTO
    assert cb.chamar(lambda: "ok") == "ok"
    assert cb.estado == CircuitBreaker.FECHADO

    with pytest.raises(ErroTransitorio):
        cb.chamar(falhar)
    relogio.t = 22
    with pytest.raises(ErroTransitorio):
        cb.chamar(falhar)  # falha no teste meio-aberto -> abre de novo
    assert cb.estado == CircuitBreaker.ABERTO


def test_erro_permanente_nao_conta_como_falha():
    cb = CircuitBreaker("x", limite_falhas=1, relogio=Relogio())

    def erro_404():
        raise ErroPermanente(404, "nao achei")
    with pytest.raises(ErroPermanente):
        cb.chamar(erro_404)
    assert cb.estado == CircuitBreaker.FECHADO


def test_retentativa_com_espera_progressiva():
    esperas, tentativas = [], []

    def instavel():
        tentativas.append(1)
        if len(tentativas) < 3:
            raise ErroTransitorio("oscilou")
        return "ok"
    assert com_retentativa(instavel, tentativas=3, espera_base=1, dormir=esperas.append) == "ok"
    assert len(tentativas) == 3
    assert 0.5 <= esperas[0] <= 1 and 1 <= esperas[1] <= 2  # cresce exponencialmente


def test_retentativa_nao_repete_erro_permanente_nem_circuito_aberto():
    for erro in (ErroPermanente(400), CircuitoAberto("x", 5)):
        tentativas = []

        def fn():
            tentativas.append(1)
            raise erro
        with pytest.raises(type(erro)):
            com_retentativa(fn, tentativas=5, dormir=lambda s: None)
        assert len(tentativas) == 1
