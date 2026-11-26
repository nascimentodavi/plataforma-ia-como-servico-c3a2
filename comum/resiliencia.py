"""Padroes de resiliencia: tempo-limite, retentativa com espera progressiva e circuit breaker.

Implementados a mao (sem biblioteca) para deixar explicito o comportamento:

    retentativa( circuit_breaker( chamada_http_com_tempo_limite ) )

- Tempo-limite: toda chamada HTTP tem timeout de conexao e de leitura.
- Retentativa: so para falhas TRANSITORIAS (timeout, conexao recusada, HTTP 5xx/429),
  com espera exponencial + jitter ("full jitter") para nao sincronizar clientes.
- Circuit breaker: apos N falhas seguidas o circuito ABRE e as chamadas falham
  imediatamente (fail fast) durante `tempo_reset` segundos; depois passa a
  MEIO-ABERTO e deixa uma unica chamada de teste passar. Sucesso -> FECHADO;
  falha -> ABERTO de novo.
"""
import logging
import random
import threading
import time
from typing import Any, Callable, Optional

import requests

log = logging.getLogger("resiliencia")


class ErroTransitorio(Exception):
    """Falha que pode se resolver sozinha (vale retentar)."""


class ErroPermanente(Exception):
    """Falha de negocio/requisicao (HTTP 4xx): retentar nao adianta."""

    def __init__(self, status: int, detalhe: Any = None):
        super().__init__(f"HTTP {status}: {detalhe}")
        self.status = status
        self.detalhe = detalhe


class CircuitoAberto(Exception):
    """Chamada rejeitada sem tentar, porque o circuito esta aberto."""

    def __init__(self, nome: str, restante: float):
        super().__init__(f"circuito '{nome}' aberto (tenta de novo em {restante:.0f}s)")
        self.nome = nome
        self.restante = restante


class CircuitBreaker:
    FECHADO = "fechado"
    ABERTO = "aberto"
    MEIO_ABERTO = "meio-aberto"

    def __init__(self, nome: str, limite_falhas: int = 5, tempo_reset: float = 30.0,
                 relogio: Callable[[], float] = time.monotonic):
        self.nome = nome
        self.limite_falhas = limite_falhas
        self.tempo_reset = tempo_reset
        self._relogio = relogio
        self._lock = threading.Lock()
        self._estado = self.FECHADO
        self._falhas = 0
        self._aberto_em = 0.0
        self._teste_em_andamento = False

    # --- estado -----------------------------------------------------------
    def _estado_atual(self) -> str:
        if self._estado == self.ABERTO and self._relogio() - self._aberto_em >= self.tempo_reset:
            self._estado = self.MEIO_ABERTO
            self._teste_em_andamento = False
            log.warning("circuit breaker '%s': ABERTO -> MEIO-ABERTO", self.nome)
        return self._estado

    @property
    def estado(self) -> str:
        with self._lock:
            return self._estado_atual()

    def resumo(self) -> dict:
        with self._lock:
            return {"nome": self.nome, "estado": self._estado_atual(),
                    "falhas_seguidas": self._falhas, "limite_falhas": self.limite_falhas}

    # --- chamada protegida ------------------------------------------------
    def chamar(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        with self._lock:
            estado = self._estado_atual()
            if estado == self.ABERTO:
                restante = self.tempo_reset - (self._relogio() - self._aberto_em)
                raise CircuitoAberto(self.nome, max(restante, 0.0))
            if estado == self.MEIO_ABERTO:
                if self._teste_em_andamento:
                    raise CircuitoAberto(self.nome, self.tempo_reset)
                self._teste_em_andamento = True
        try:
            resultado = fn(*args, **kwargs)
        except ErroPermanente:
            # O servico respondeu (ex.: 404/422): ele esta saudavel.
            self._registrar_sucesso()
            raise
        except Exception:
            self._registrar_falha()
            raise
        self._registrar_sucesso()
        return resultado

    def _registrar_sucesso(self) -> None:
        with self._lock:
            if self._estado != self.FECHADO:
                log.warning("circuit breaker '%s': %s -> FECHADO", self.nome, self._estado.upper())
            self._estado = self.FECHADO
            self._falhas = 0
            self._teste_em_andamento = False

    def _registrar_falha(self) -> None:
        with self._lock:
            self._falhas += 1
            if self._estado == self.MEIO_ABERTO or self._falhas >= self.limite_falhas:
                if self._estado != self.ABERTO:
                    log.error("circuit breaker '%s': %s -> ABERTO (%d falhas)",
                              self.nome, self._estado.upper(), self._falhas)
                self._estado = self.ABERTO
                self._aberto_em = self._relogio()
                self._teste_em_andamento = False


def com_retentativa(fn: Callable[[], Any], *, tentativas: int = 3, espera_base: float = 0.2,
                    espera_maxima: float = 3.0, retentar_em: tuple = (ErroTransitorio,),
                    nao_retentar: tuple = (ErroPermanente, CircuitoAberto),
                    dormir: Callable[[float], None] = time.sleep) -> Any:
    """Executa `fn` com retentativas e espera exponencial com jitter."""
    for tentativa in range(1, tentativas + 1):
        try:
            return fn()
        except nao_retentar:
            raise
        except retentar_em as erro:
            if tentativa >= tentativas:
                raise
            teto = min(espera_maxima, espera_base * (2 ** (tentativa - 1)))
            espera = random.uniform(teto / 2, teto)
            log.warning("tentativa %d/%d falhou (%s); nova tentativa em %.2fs",
                        tentativa, tentativas, erro, espera)
            dormir(espera)
    raise RuntimeError("inalcancavel")


class ClienteResiliente:
    """Cliente HTTP que combina tempo-limite + retentativa + circuit breaker."""

    def __init__(self, nome: str, url_base: str, *, timeout: float = 5.0,
                 timeout_conexao: float = 2.0, tentativas: int = 3,
                 breaker: Optional[CircuitBreaker] = None, cabecalhos: Optional[dict] = None):
        self.nome = nome
        self.url_base = url_base.rstrip("/")
        self.timeout = timeout
        self.timeout_conexao = timeout_conexao
        self.tentativas = tentativas
        self.breaker = breaker or CircuitBreaker(nome)
        self.cabecalhos = cabecalhos or {}
        self._sessao = requests.Session()

    def _uma_chamada(self, metodo: str, caminho: str, **kwargs) -> Any:
        url = f"{self.url_base}{caminho}"
        try:
            r = self._sessao.request(metodo, url, headers=self.cabecalhos,
                                     timeout=(self.timeout_conexao, self.timeout), **kwargs)
        except requests.Timeout as e:
            raise ErroTransitorio(f"{self.nome}: tempo-limite excedido") from e
        except requests.ConnectionError as e:
            raise ErroTransitorio(f"{self.nome}: falha de conexao") from e
        if r.status_code >= 500 or r.status_code == 429:
            raise ErroTransitorio(f"{self.nome}: HTTP {r.status_code}")
        if r.status_code >= 400:
            try:
                detalhe = r.json().get("detail", r.text)
            except ValueError:
                detalhe = r.text
            raise ErroPermanente(r.status_code, detalhe)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def requisitar(self, metodo: str, caminho: str, **kwargs) -> Any:
        return com_retentativa(
            lambda: self.breaker.chamar(self._uma_chamada, metodo, caminho, **kwargs),
            tentativas=self.tentativas,
        )
