"""Acesso ao armazenamento vetorial (Qdrant) pela API REST.

Toda chamada ao Qdrant passa pelo ClienteResiliente (tempo-limite, retentativa
e circuit breaker). A colecao e criada sob demanda (idempotente), o que evita
depender da ordem de inicializacao dos containers.
"""
import threading
import time
import uuid

from comum import config
from comum.resiliencia import CircuitBreaker, ClienteResiliente, ErroPermanente

COLECAO = config.texto("QDRANT_COLECAO", "documentos")


class RepositorioVetorial:
    def __init__(self, dimensao: int):
        cabecalhos = {}
        if config.texto("QDRANT_API_KEY"):
            cabecalhos["api-key"] = config.texto("QDRANT_API_KEY")
        self.dimensao = dimensao
        self.cliente = ClienteResiliente(
            "qdrant", config.texto("QDRANT_URL", "http://localhost:6333"),
            timeout=config.decimal("TIMEOUT_QDRANT_S", 5.0),
            tentativas=config.inteiro("TENTATIVAS", 3),
            breaker=CircuitBreaker("qdrant", config.inteiro("CB_LIMITE_FALHAS", 5),
                                   config.decimal("CB_TEMPO_RESET_S", 30.0)),
            cabecalhos=cabecalhos,
        )
        self._pronta = False
        self._lock = threading.Lock()

    @staticmethod
    def _id_ponto(doc_id: str) -> str:
        # Qdrant aceita UUID ou inteiro como id; o job_id (hex de 32) vira UUID.
        return str(uuid.UUID(hex=doc_id))

    def garantir_colecao(self) -> None:
        if self._pronta:
            return
        with self._lock:
            if self._pronta:
                return
            try:
                self.cliente.requisitar("GET", f"/collections/{COLECAO}")
            except ErroPermanente as e:
                if e.status != 404:
                    raise
                self.cliente.requisitar("PUT", f"/collections/{COLECAO}", json={
                    "vectors": {"size": self.dimensao, "distance": "Cosine"}})
                for campo, tipo in (("dono", "keyword"), ("expira_em", "float")):
                    self.cliente.requisitar("PUT", f"/collections/{COLECAO}/index",
                                            params={"wait": "true"},
                                            json={"field_name": campo, "field_schema": tipo})
            self._pronta = True

    def salvar(self, doc_id: str, vetor: list, dados: dict) -> None:
        self.garantir_colecao()
        self.cliente.requisitar("PUT", f"/collections/{COLECAO}/points", params={"wait": "true"},
                                json={"points": [{"id": self._id_ponto(doc_id), "vector": vetor,
                                                  "payload": dados}]})

    def buscar(self, vetor: list, dono: str, k: int) -> list:
        self.garantir_colecao()
        resposta = self.cliente.requisitar("POST", f"/collections/{COLECAO}/points/search", json={
            "vector": vetor, "limit": k, "with_payload": True,
            "filter": {"must": [{"key": "dono", "match": {"value": dono}},
                                {"key": "expira_em", "range": {"gt": time.time()}}]},
        })
        return resposta.get("result", [])

    def apagar(self, doc_id: str, dono: str) -> None:
        self.garantir_colecao()
        self.cliente.requisitar("POST", f"/collections/{COLECAO}/points/delete",
                                params={"wait": "true"}, json={"filter": {"must": [
                                    {"has_id": [self._id_ponto(doc_id)]},
                                    {"key": "dono", "match": {"value": dono}}]}})

    def expurgar_expirados(self) -> None:
        self.garantir_colecao()
        self.cliente.requisitar("POST", f"/collections/{COLECAO}/points/delete",
                                params={"wait": "true"}, json={"filter": {"must": [
                                    {"key": "expira_em", "range": {"lt": time.time()}}]}})
