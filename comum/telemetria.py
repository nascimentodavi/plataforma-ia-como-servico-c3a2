"""Observabilidade: rastreamento distribuido (OpenTelemetry) e logs estruturados.

- Cada servico cria spans e os exporta via OTLP/HTTP para o coletor definido em
  OTEL_EXPORTER_OTLP_ENDPOINT (Jaeger local, ou Grafana Cloud/Honeycomb na nuvem).
- Chamadas HTTP (requests) e Redis sao instrumentadas automaticamente; o
  contexto W3C (`traceparent`) viaja nos cabecalhos HTTP.
- Na FILA nao existem cabecalhos HTTP: por isso o contexto e injetado nos
  campos da mensagem (injetar_contexto) e extraido pelo worker (extrair_contexto).
  Assim gateway -> fila -> worker -> inferencia -> qdrant formam UM unico trace.
- Logs saem em JSON com o trace_id, e NUNCA contem o texto enviado pelo usuario.
"""
import importlib.util
import json
import logging
import os
import sys

from opentelemetry import propagate, trace
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import SpanKind

_SERVICO = "desconhecido"


class SemRaizCliente(Sampler):
    """Descarta traces que COMECAM num span de cliente (ex.: o XREADGROUP do laco
    do worker a cada 5s, ou as leituras do /metricas). Eles nao tem contexto de
    negocio e poluiriam o Jaeger. Spans de cliente DENTRO de um trace (filhos)
    continuam sendo gravados normalmente (ParentBased)."""

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None,
                      links=None, trace_state=None):
        if kind == SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        return "SemRaizCliente"


class FormatoJSON(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        dados = {
            "ts": self.formatTime(registro, "%Y-%m-%dT%H:%M:%S"),
            "nivel": registro.levelname,
            "servico": _SERVICO,
            "logger": registro.name,
            "msg": registro.getMessage(),
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            dados["trace_id"] = format(ctx.trace_id, "032x")
        extra = getattr(registro, "dados", None)
        if isinstance(extra, dict):
            dados.update(extra)
        if registro.exc_info:
            dados["erro"] = self.formatException(registro.exc_info).splitlines()[-1]
        return json.dumps(dados, ensure_ascii=False)


def _configurar_logs() -> None:
    raiz = logging.getLogger()
    raiz.handlers.clear()
    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(FormatoJSON())
    raiz.addHandler(saida)
    raiz.setLevel(os.getenv("LOG_NIVEL", "INFO").upper())
    # access log do uvicorn mostra query string (pode conter texto do usuario): desligado.
    logging.getLogger("uvicorn.access").disabled = True


def configurar(nome_servico: str) -> trace.Tracer:
    global _SERVICO
    _SERVICO = nome_servico
    _configurar_logs()

    recurso = Resource.create({
        "service.name": nome_servico,
        "service.namespace": "plataforma-ia",
        "deployment.environment": os.getenv("AMBIENTE", "local"),
    })
    provedor = TracerProvider(resource=recurso, sampler=ParentBased(root=SemRaizCliente()))
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provedor.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    elif os.getenv("OTEL_CONSOLE") == "1":
        provedor.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provedor)

    RequestsInstrumentor().instrument()
    if importlib.util.find_spec("redis") is not None:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
    return trace.get_tracer(nome_servico)


def injetar_contexto() -> dict:
    """Serializa o contexto do span atual (W3C traceparent) num dict."""
    portador: dict = {}
    propagate.inject(portador)
    return portador


def extrair_contexto(portador: dict):
    return propagate.extract(portador)
