"""Worker de fila: consome jobs do Redis Stream e indexa os documentos.

Semantica de entrega: PELO MENOS UMA VEZ (at-least-once).
  * O worker le com XREADGROUP (consumer group): varias replicas dividem a fila.
  * So confirma (XACK) depois de processar. Se o worker morrer no meio, a
    mensagem fica pendente e outra replica a recupera com XAUTOCLAIM depois de
    VISIBILIDADE_MS sem ACK.
  * Falha transitoria -> nao confirma; a mensagem volta apos VISIBILIDADE_MS
    (retentativa com espera). Apos MAX_TENTATIVAS vai para a dead-letter queue.
  * A indexacao e idempotente (id do ponto = job_id), entao reprocessar e seguro.
  * Depois do ACK a mensagem e apagada do stream (XDEL): o texto do usuario
    nao fica guardado na fila (minimizacao de dados - LGPD).

Rastreamento: o contexto W3C vem DENTRO da mensagem e vira o pai do span de
consumo, ligando gateway -> fila -> worker -> inferencia -> qdrant num so trace.
"""
import json
import logging
import os
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from comum import config, telemetria
from comum.resiliencia import (CircuitBreaker, CircuitoAberto, ClienteResiliente,
                               ErroPermanente, ErroTransitorio)

tracer = telemetria.configurar("worker")
log = logging.getLogger("worker")

STREAM = config.texto("STREAM_JOBS", "jobs")
DLQ = f"{STREAM}:dlq"
GRUPO = config.texto("GRUPO_WORKERS", "workers")
CONSUMIDOR = f"{socket.gethostname()}-{os.getpid()}"
MAX_TENTATIVAS = config.inteiro("MAX_TENTATIVAS", 5)
VISIBILIDADE_MS = config.inteiro("VISIBILIDADE_MS", 15000)
RETENCAO_JOB_S = config.inteiro("RETENCAO_JOB_S", 24 * 3600)
EXPURGO_INTERVALO_S = config.inteiro("EXPURGO_INTERVALO_S", 3600)
PORTA_SAUDE = config.inteiro("PORT", 8002)

r = redis.Redis.from_url(config.texto("REDIS_URL", "redis://localhost:6379/0"),
                         decode_responses=True, socket_timeout=10, socket_connect_timeout=3)
inferencia = ClienteResiliente(
    "inferencia", config.texto("URL_INFERENCIA", "http://localhost:8001"),
    timeout=config.decimal("TIMEOUT_INFERENCIA_S", 5.0),
    tentativas=config.inteiro("TENTATIVAS", 3),
    breaker=CircuitBreaker("inferencia", config.inteiro("CB_LIMITE_FALHAS", 5),
                           config.decimal("CB_TEMPO_RESET_S", 30.0)),
    cabecalhos={"X-Token-Interno": config.texto("TOKEN_INTERNO")},
)

estado = {"rodando": True, "ultimo_ciclo": time.time(), "processados": 0}


# ------------------------------------------------------------------ fila
def garantir_grupo() -> None:
    try:
        r.xgroup_create(STREAM, GRUPO, id="0", mkstream=True)
        log.info("consumer group criado", extra={"dados": {"stream": STREAM, "grupo": GRUPO}})
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def confirmar(msg_id: str) -> None:
    pipe = r.pipeline()
    pipe.xack(STREAM, GRUPO, msg_id)
    pipe.xdel(STREAM, msg_id)
    pipe.execute()


def atualizar_job(job_id: str, **campos) -> None:
    pipe = r.pipeline()
    pipe.hset(f"job:{job_id}", mapping=campos)
    pipe.expire(f"job:{job_id}", RETENCAO_JOB_S, nx=True)  # nunca deixa job sem TTL
    pipe.execute()


def mover_para_dlq(msg_id: str, job_id: str, motivo: str) -> None:
    # Na DLQ vai so o identificador e o motivo: o texto do usuario e descartado.
    r.xadd(DLQ, {"job_id": job_id, "motivo": motivo[:300], "em": time.time()},
           maxlen=1000, approximate=True)
    atualizar_job(job_id, status="falhou", erro=motivo[:300])
    r.incr("metricas:falhas")
    confirmar(msg_id)
    log.error("job enviado para a dead-letter queue", extra={"dados": {"job_id": job_id,
                                                                       "motivo": motivo}})


# ------------------------------------------------------------------ processamento
def processar(msg_id: str, campos: dict) -> None:
    job_id = campos.get("job_id", "")
    contexto = telemetria.extrair_contexto(campos)
    with tracer.start_as_current_span(f"{STREAM} process", context=contexto,
                                      kind=SpanKind.CONSUMER) as span:
        span.set_attribute("messaging.system", "redis")
        span.set_attribute("messaging.destination.name", STREAM)
        span.set_attribute("messaging.message.id", job_id)
        span.set_attribute("messaging.consumer.group.name", GRUPO)
        try:
            espera_fila = time.time() - float(campos.get("criado_em", time.time()))
            span.set_attribute("fila.tempo_espera_s", round(espera_fila, 3))
        except ValueError:
            pass

        if not job_id or not r.exists(f"job:{job_id}"):
            # Job expirou ou o titular pediu a eliminacao: descarta.
            span.add_event("job_descartado")
            confirmar(msg_id)
            return

        tentativa = r.hincrby(f"job:{job_id}", "tentativas", 1)
        span.set_attribute("job.tentativa", tentativa)
        atualizar_job(job_id, status="processando")
        try:
            resultado = inferencia.requisitar("POST", "/indexar", json={
                "id": job_id, "texto": campos.get("texto", ""), "dono": campos.get("dono", "")})
        except ErroPermanente as e:
            span.set_status(trace.Status(trace.StatusCode.ERROR, "erro permanente"))
            mover_para_dlq(msg_id, job_id, f"erro permanente: {e}")
            return
        except (ErroTransitorio, CircuitoAberto) as e:
            span.record_exception(e)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "erro transitorio"))
            if tentativa >= MAX_TENTATIVAS:
                mover_para_dlq(msg_id, job_id, f"{tentativa} tentativas esgotadas: {e}")
            else:
                # Sem ACK: a mensagem sera reentregue apos VISIBILIDADE_MS.
                atualizar_job(job_id, status="aguardando_retentativa", erro=str(e)[:300])
                r.incr("metricas:retentativas")
                log.warning("falha transitoria; job sera reprocessado",
                            extra={"dados": {"job_id": job_id, "tentativa": tentativa}})
            return

        if not r.exists(f"job:{job_id}"):
            confirmar(msg_id)  # apagado durante o processamento: nao recria
            return
        atualizar_job(job_id, status="concluido", sentimento=resultado["sentimento"],
                      confianca=resultado["confianca"], concluido_em=time.time(), erro="")
        r.incr("metricas:processados")
        confirmar(msg_id)
        estado["processados"] += 1
        log.info("job concluido", extra={"dados": {"job_id": job_id, "tentativa": tentativa}})


def recuperar_orfas() -> list:
    """Mensagens pendentes ha mais de VISIBILIDADE_MS (worker morreu ou falha transitoria)."""
    try:
        _, mensagens, *_ = r.xautoclaim(STREAM, GRUPO, CONSUMIDOR, min_idle_time=VISIBILIDADE_MS,
                                        start_id="0-0", count=10)
    except redis.ResponseError:
        return []
    return [(mid, campos) for mid, campos in mensagens if campos]


def expurgar_se_preciso(ultimo: float) -> float:
    if time.time() - ultimo < EXPURGO_INTERVALO_S:
        return ultimo
    with tracer.start_as_current_span("lgpd.expurgo_retencao"):
        try:
            inferencia.requisitar("POST", "/expurgo")
            log.info("expurgo de vetores expirados executado")
        except (ErroTransitorio, CircuitoAberto, ErroPermanente) as e:
            log.warning("expurgo adiado: %s", e)
            return ultimo
    return time.time()


def laco_principal() -> None:
    ultimo_expurgo = 0.0
    while estado["rodando"]:
        estado["ultimo_ciclo"] = time.time()
        try:
            garantir_grupo()
            for msg_id, campos in recuperar_orfas():
                processar(msg_id, campos)
            resposta = r.xreadgroup(GRUPO, CONSUMIDOR, {STREAM: ">"}, count=5, block=5000)
            for _, mensagens in resposta or []:
                for msg_id, campos in mensagens:
                    processar(msg_id, campos)
            ultimo_expurgo = expurgar_se_preciso(ultimo_expurgo)
        except redis.RedisError as e:
            log.error("redis indisponivel, tentando de novo em 3s: %s", e)
            time.sleep(3)
        except Exception:
            log.exception("erro inesperado no laco do worker")
            time.sleep(1)
    log.info("worker encerrado com seguranca")


# ------------------------------------------------------------------ saude (HTTP)
class Saude(BaseHTTPRequestHandler):
    def do_GET(self):
        vivo = time.time() - estado["ultimo_ciclo"] < 60
        corpo = json.dumps({"status": "ok" if vivo else "travado", "consumidor": CONSUMIDOR,
                            "processados": estado["processados"],
                            "breaker_inferencia": inferencia.breaker.estado}).encode()
        self.send_response(200 if vivo else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args):
        pass


def encerrar(*_):
    estado["rodando"] = False


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, encerrar)
    signal.signal(signal.SIGINT, encerrar)
    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA_SAUDE), Saude)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    log.info("worker iniciado", extra={"dados": {"consumidor": CONSUMIDOR, "stream": STREAM}})
    laco_principal()
