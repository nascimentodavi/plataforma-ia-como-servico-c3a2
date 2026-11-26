"""Servico de inferencia: sentimento, embeddings e busca semantica.

E o UNICO servico que fala com o armazenamento vetorial (Qdrant): gateway e
worker so conhecem esta API. Nao e exposto publicamente no docker compose; na
nuvem exige o cabecalho X-Token-Interno (comunicacao servico-a-servico).

Injecao de falhas (para demonstrar resiliencia), via variaveis de ambiente:
  FALHA_PERCENTUAL=30     -> 30% das chamadas de /predict e /indexar retornam 500
  LATENCIA_EXTRA_MS=8000  -> atraso artificial (dispara o tempo-limite do chamador)
"""
import hmac
import logging
import random
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, Field

from comum import config, telemetria
from comum.resiliencia import CircuitoAberto, ErroPermanente, ErroTransitorio
import modelo as ia
from vetores import RepositorioVetorial

tracer = telemetria.configurar("inferencia")
log = logging.getLogger("inferencia")

TOKEN_INTERNO = config.texto("TOKEN_INTERNO")
RETENCAO_VETORES_S = config.inteiro("RETENCAO_VETORES_S", 7 * 24 * 3600)
FALHA_PERCENTUAL = config.decimal("FALHA_PERCENTUAL", 0.0)
LATENCIA_EXTRA_MS = config.inteiro("LATENCIA_EXTRA_MS", 0)

app = FastAPI(title="Inferencia - Plataforma de IA")
FastAPIInstrumentor.instrument_app(app, excluded_urls="saude")
classificador = ia.carregar()
repositorio = RepositorioVetorial(ia.DIMENSAO)


# ---------------------------------------------------------------- dependencias
def autenticar_servico(x_token_interno: str = Header(default="")) -> None:
    if TOKEN_INTERNO and not hmac.compare_digest(x_token_interno, TOKEN_INTERNO):
        raise HTTPException(status_code=401, detail="token interno invalido")


def caos() -> None:
    if LATENCIA_EXTRA_MS:
        time.sleep(LATENCIA_EXTRA_MS / 1000)
    if FALHA_PERCENTUAL and random.uniform(0, 100) < FALHA_PERCENTUAL:
        trace.get_current_span().add_event("falha_injetada")
        raise HTTPException(status_code=500, detail="falha injetada (FALHA_PERCENTUAL)")


def tratar_vetorial(fn):
    try:
        return fn()
    except CircuitoAberto as e:
        raise HTTPException(503, f"armazenamento vetorial indisponivel: {e}")
    except ErroTransitorio as e:
        raise HTTPException(503, f"armazenamento vetorial indisponivel: {e}")
    except ErroPermanente as e:
        raise HTTPException(502, f"erro no armazenamento vetorial: {e}")


# ---------------------------------------------------------------- modelos
class Entrada(BaseModel):
    texto: str = Field(min_length=1, max_length=5000)


class Documento(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    texto: str = Field(min_length=1, max_length=5000)
    dono: str = Field(min_length=1, max_length=64)


class Consulta(BaseModel):
    texto: str = Field(min_length=1, max_length=1000)
    dono: str = Field(min_length=1, max_length=64)
    k: int = Field(default=3, ge=1, le=20)


# ---------------------------------------------------------------- rotas
@app.get("/saude")
def saude():
    return {"status": "ok", "modelo": ia.VERSAO,
            "qdrant_breaker": repositorio.cliente.breaker.estado}


@app.post("/predict", dependencies=[Depends(autenticar_servico), Depends(caos)])
def predict(entrada: Entrada):
    with tracer.start_as_current_span("modelo.classificar"):
        resultado = ia.classificar(classificador, entrada.texto)
    return {**resultado, "modelo": ia.VERSAO}


@app.post("/indexar", dependencies=[Depends(autenticar_servico), Depends(caos)])
def indexar(doc: Documento):
    with tracer.start_as_current_span("modelo.classificar"):
        resultado = ia.classificar(classificador, doc.texto)
    with tracer.start_as_current_span("modelo.embedding") as span:
        vetor = ia.embedding(doc.texto)
        span.set_attribute("vetor.dimensao", len(vetor))
    agora = time.time()
    dados = {"dono": doc.dono, "texto": doc.texto, **resultado,
             "criado_em": agora, "expira_em": agora + RETENCAO_VETORES_S}
    tratar_vetorial(lambda: repositorio.salvar(doc.id, vetor, dados))
    log.info("documento indexado", extra={"dados": {"doc_id": doc.id, **resultado}})
    return {**resultado, "modelo": ia.VERSAO, "expira_em": dados["expira_em"]}


@app.post("/buscar", dependencies=[Depends(autenticar_servico)])
def buscar(consulta: Consulta):
    vetor = ia.embedding(consulta.texto)
    achados = tratar_vetorial(lambda: repositorio.buscar(vetor, consulta.dono, consulta.k))
    return {"resultados": [{
        "id": str(p["id"]).replace("-", ""),
        "similaridade": round(p["score"], 4),
        "trecho": p["payload"].get("texto", "")[:200],
        "sentimento": p["payload"].get("sentimento"),
    } for p in achados]}


@app.delete("/documentos/{doc_id}", status_code=204, dependencies=[Depends(autenticar_servico)])
def apagar(doc_id: str, dono: str):
    if len(doc_id) != 32:
        raise HTTPException(422, "id invalido")
    tratar_vetorial(lambda: repositorio.apagar(doc_id, dono))
    log.info("documento apagado a pedido do titular", extra={"dados": {"doc_id": doc_id}})


@app.post("/expurgo", dependencies=[Depends(autenticar_servico)])
def expurgo():
    """Remove vetores com prazo de retencao vencido (LGPD). Chamado pelo worker."""
    tratar_vetorial(repositorio.expurgar_expirados)
    return {"status": "ok"}
