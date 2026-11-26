"""API Gateway - porta de entrada unica (e unica parte publica) da plataforma.

Responsabilidades:
  * autenticacao JWT (/login emite; demais rotas exigem Bearer token)
  * rate limit por usuario (e por IP no /login), contador no Redis
  * roteamento SINCRONO (REST) para a inferencia, com tempo-limite,
    retentativa com espera progressiva e circuit breaker
  * roteamento ASSINCRONO: publica jobs no Redis Stream consumido pelo worker,
    levando o contexto de rastreamento dentro da mensagem
  * metricas e painel do sistema (/metricas, /painel)
"""
import logging
import time
import uuid
from pathlib import Path

import redis
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from comum import config, telemetria
from comum.resiliencia import (CircuitBreaker, CircuitoAberto, ClienteResiliente,
                               ErroPermanente, ErroTransitorio)
import seguranca
from seguranca import LimitadorTaxa, usuario_atual

tracer = telemetria.configurar("gateway")
log = logging.getLogger("gateway")

STREAM_JOBS = config.texto("STREAM_JOBS", "jobs")
STREAM_DLQ = f"{STREAM_JOBS}:dlq"
GRUPO_WORKERS = config.texto("GRUPO_WORKERS", "workers")
RETENCAO_JOB_S = config.inteiro("RETENCAO_JOB_S", 24 * 3600)

r = redis.Redis.from_url(config.texto("REDIS_URL", "redis://localhost:6379/0"),
                         decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
limitador = LimitadorTaxa(r)

breaker_inferencia = CircuitBreaker("inferencia", config.inteiro("CB_LIMITE_FALHAS", 5),
                                    config.decimal("CB_TEMPO_RESET_S", 30.0))
inferencia = ClienteResiliente(
    "inferencia", config.texto("URL_INFERENCIA", "http://localhost:8001"),
    timeout=config.decimal("TIMEOUT_INFERENCIA_S", 5.0),
    tentativas=config.inteiro("TENTATIVAS", 3),
    breaker=breaker_inferencia,
    cabecalhos={"X-Token-Interno": config.texto("TOKEN_INTERNO")},
)

app = FastAPI(
    title="Plataforma de IA como Servico - Gateway",
    description="Use POST /login para obter o token e clique em Authorize.",
    version="1.0.0",
)
FastAPIInstrumentor.instrument_app(app, excluded_urls="saude,metricas,painel")


# ------------------------------------------------------------------ middleware
@app.middleware("http")
async def registrar(request: Request, call_next):
    inicio = time.perf_counter()
    resposta = await call_next(request)
    ms = (time.perf_counter() - inicio) * 1000
    # So metodo, caminho, status e tempo: nunca corpo, query string, IP ou token.
    log.info("requisicao", extra={"dados": {
        "metodo": request.method, "caminho": request.url.path,
        "status": resposta.status_code, "ms": round(ms, 1)}})
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resposta


# ------------------------------------------------------------------ dependencias
def limitar_por_usuario(usuario: str = Depends(usuario_atual)) -> str:
    limitador.verificar(f"u:{seguranca.pseudonimo(usuario)}", seguranca.RATE_LIMIT_POR_MINUTO)
    return usuario


def chamar_inferencia(metodo: str, caminho: str, **kwargs):
    try:
        return inferencia.requisitar(metodo, caminho, **kwargs)
    except CircuitoAberto as e:
        raise HTTPException(503, "servico de inferencia temporariamente indisponivel "
                                 "(circuit breaker aberto)",
                            headers={"Retry-After": str(int(e.restante) + 1)})
    except ErroTransitorio as e:
        raise HTTPException(503, f"servico de inferencia indisponivel apos "
                                 f"{inferencia.tentativas} tentativas: {e}")
    except ErroPermanente as e:
        raise HTTPException(e.status if e.status < 500 else 502, e.detalhe)


# ------------------------------------------------------------------ modelos
class Login(BaseModel):
    usuario: str = Field(min_length=1, max_length=64)
    senha: str = Field(min_length=1, max_length=128)


class Entrada(BaseModel):
    texto: str = Field(min_length=1, max_length=5000, examples=["servico excelente"])


class Consulta(BaseModel):
    texto: str = Field(min_length=1, max_length=1000, examples=["produto quebrado"])
    k: int = Field(default=3, ge=1, le=20)


# ------------------------------------------------------------------ rotas publicas
@app.get("/", include_in_schema=False)
def raiz():
    return {"servico": "Plataforma de IA como Servico", "docs": "/docs", "painel": "/painel",
            "saude": "/saude"}


@app.get("/saude", tags=["infra"])
def saude():
    return {"status": "ok"}


@app.get("/prontidao", tags=["infra"])
def prontidao(response: Response):
    """Verifica dependencias (Redis e estado do circuit breaker da inferencia)."""
    detalhes = {"redis": "ok", "inferencia": breaker_inferencia.estado}
    try:
        r.ping()
    except redis.RedisError:
        detalhes["redis"] = "indisponivel"
    pronto = detalhes["redis"] == "ok" and detalhes["inferencia"] != CircuitBreaker.ABERTO
    response.status_code = 200 if pronto else 503
    return {"pronto": pronto, **detalhes}


@app.post("/login", tags=["autenticacao"])
def login(dados: Login, request: Request):
    limitador.verificar(f"ip:{seguranca.pseudonimo(seguranca.ip_cliente(request))}",
                        seguranca.RATE_LIMIT_LOGIN_POR_MINUTO)
    if not seguranca.verificar_credenciais(dados.usuario, dados.senha):
        raise HTTPException(401, "usuario ou senha invalidos")
    token, expira = seguranca.emitir_token(dados.usuario)
    return {"access_token": token, "token_type": "bearer", "expira_em": expira.isoformat()}


# ------------------------------------------------------------------ rotas protegidas
@app.post("/predict", tags=["inferencia (sincrono)"])
def predict(entrada: Entrada, usuario: str = Depends(limitar_por_usuario)):
    """Classificacao de sentimento sincrona (gateway -> inferencia via REST)."""
    return chamar_inferencia("POST", "/predict", json=entrada.model_dump())


@app.post("/documentos", status_code=202, tags=["documentos (assincrono)"])
def enviar_documento(entrada: Entrada, usuario: str = Depends(limitar_por_usuario)):
    """Enfileira um documento para analise + indexacao vetorial pelo worker."""
    dono = seguranca.pseudonimo(usuario)
    job_id = uuid.uuid4().hex
    with tracer.start_as_current_span(f"{STREAM_JOBS} publish", kind=SpanKind.PRODUCER) as span:
        span.set_attribute("messaging.system", "redis")
        span.set_attribute("messaging.destination.name", STREAM_JOBS)
        span.set_attribute("messaging.message.id", job_id)
        portador = telemetria.injetar_contexto()  # traceparent vai DENTRO da mensagem
        try:
            pipe = r.pipeline(transaction=True)
            pipe.hset(f"job:{job_id}", mapping={"status": "enfileirado", "dono": dono,
                                                "criado_em": time.time(), "tentativas": 0})
            pipe.expire(f"job:{job_id}", RETENCAO_JOB_S)
            pipe.xadd(STREAM_JOBS, {"job_id": job_id, "dono": dono, "texto": entrada.texto,
                                    "criado_em": time.time(), **portador},
                      maxlen=10000, approximate=True)
            pipe.execute()
        except redis.RedisError:
            span.set_status(trace.Status(trace.StatusCode.ERROR, "fila indisponivel"))
            raise HTTPException(503, "fila indisponivel; tente novamente")
    return {"job_id": job_id, "status": "enfileirado", "acompanhe_em": f"/jobs/{job_id}"}


@app.get("/jobs/{job_id}", tags=["documentos (assincrono)"])
def consultar_job(job_id: str, usuario: str = Depends(limitar_por_usuario)):
    try:
        job = r.hgetall(f"job:{job_id}")
    except redis.RedisError:
        raise HTTPException(503, "armazenamento de jobs indisponivel")
    if not job or job.get("dono") != seguranca.pseudonimo(usuario):
        raise HTTPException(404, "job nao encontrado (ou expirado)")
    job.pop("dono", None)
    return {"job_id": job_id, **job}


@app.post("/busca", tags=["documentos (assincrono)"])
def buscar(consulta: Consulta, usuario: str = Depends(limitar_por_usuario)):
    """Busca semantica (armazenamento vetorial) apenas nos documentos do proprio usuario.

    E POST (e nao GET ?q=) de proposito: query string acaba em logs e traces
    (http.url); no corpo, o texto da consulta nao e registrado em lugar nenhum.
    """
    return chamar_inferencia("POST", "/buscar", json={
        "texto": consulta.texto, "dono": seguranca.pseudonimo(usuario), "k": consulta.k})


@app.delete("/documentos/{job_id}", status_code=204, tags=["documentos (assincrono)"])
def apagar_documento(job_id: str, usuario: str = Depends(limitar_por_usuario)):
    """Direito de eliminacao (LGPD art. 18): remove o vetor, o texto e o job."""
    if len(job_id) != 32:
        raise HTTPException(404, "documento nao encontrado")
    dono = seguranca.pseudonimo(usuario)
    chamar_inferencia("DELETE", f"/documentos/{job_id}", params={"dono": dono})
    try:
        if r.hget(f"job:{job_id}", "dono") == dono:
            r.delete(f"job:{job_id}")
    except redis.RedisError:
        raise HTTPException(503, "armazenamento de jobs indisponivel")
    return Response(status_code=204)


# ------------------------------------------------------------------ metricas / painel
@app.get("/metricas", tags=["infra"])
def metricas():
    """Metricas agregadas (sem nenhum dado pessoal)."""
    dados = {"circuit_breaker_inferencia": breaker_inferencia.resumo()}
    try:
        pipe = r.pipeline()
        pipe.xlen(STREAM_JOBS)
        pipe.xlen(STREAM_DLQ)
        pipe.mget("metricas:processados", "metricas:falhas", "metricas:retentativas")
        tamanho, dlq, (processados, falhas, retentativas) = pipe.execute()
        try:
            pendentes = r.xpending(STREAM_JOBS, GRUPO_WORKERS)["pending"]
            consumidores = len(r.xinfo_consumers(STREAM_JOBS, GRUPO_WORKERS))
        except redis.ResponseError:
            pendentes, consumidores = 0, 0
        dados["fila"] = {"mensagens_no_stream": tamanho, "pendentes_sem_ack": pendentes,
                         "dead_letter": dlq, "workers_registrados": consumidores}
        dados["jobs"] = {"processados": int(processados or 0), "falhas": int(falhas or 0),
                         "retentativas": int(retentativas or 0)}
        dados["redis"] = "ok"
    except redis.RedisError:
        dados["redis"] = "indisponivel"
    return dados


PAINEL_HTML = (Path(__file__).parent / "painel.html").read_text(encoding="utf-8")


@app.get("/painel", response_class=HTMLResponse, include_in_schema=False)
def painel():
    return PAINEL_HTML
