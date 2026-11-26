import fakeredis
import jwt
import pytest
from fastapi.testclient import TestClient

import app as gateway
import seguranca


@pytest.fixture()
def cliente(monkeypatch):
    falso = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(gateway, "r", falso)
    monkeypatch.setattr(gateway.limitador, "redis", falso)
    monkeypatch.setattr(gateway.inferencia, "requisitar",
                        lambda metodo, caminho, **kw: {"sentimento": "positivo", "confianca": 0.9})
    return TestClient(gateway.app)


def token(cliente):
    r = cliente.post("/login", json={"usuario": "ana", "senha": "senha-da-ana"})
    assert r.status_code == 200
    return r.json()["access_token"]


def test_saude_e_publica(cliente):
    assert cliente.get("/saude").json() == {"status": "ok"}


def test_sem_token_retorna_401(cliente):
    assert cliente.post("/predict", json={"texto": "oi"}).status_code == 401


def test_senha_errada_retorna_401(cliente):
    assert cliente.post("/login", json={"usuario": "ana", "senha": "x"}).status_code == 401


def test_token_forjado_retorna_401(cliente):
    falso = jwt.encode({"sub": "ana", "iss": seguranca.JWT_EMISSOR, "exp": 9999999999},
                       "outro-segredo", algorithm="HS256")
    r = cliente.post("/predict", json={"texto": "oi"}, headers={"Authorization": f"Bearer {falso}"})
    assert r.status_code == 401


def test_predict_com_token(cliente):
    r = cliente.post("/predict", json={"texto": "oi"},
                     headers={"Authorization": f"Bearer {token(cliente)}"})
    assert r.status_code == 200 and r.json()["sentimento"] == "positivo"


def test_rate_limit_retorna_429(cliente):
    cab = {"Authorization": f"Bearer {token(cliente)}"}
    codigos = [cliente.post("/predict", json={"texto": "oi"}, headers=cab).status_code
               for _ in range(4)]
    assert codigos == [200, 200, 200, 429]  # RATE_LIMIT_POR_MINUTO=3 no conftest


def test_documento_vai_para_fila_com_contexto_de_trace(cliente):
    cab = {"Authorization": f"Bearer {token(cliente)}"}
    r = cliente.post("/documentos", json={"texto": "produto otimo"}, headers=cab)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    [(_, campos)] = gateway.r.xrange(gateway.STREAM_JOBS)
    assert campos["job_id"] == job_id
    assert campos["dono"] != "ana"  # usuario pseudonimizado
    assert cliente.get(f"/jobs/{job_id}", headers=cab).json()["status"] == "enfileirado"


def test_job_de_outro_usuario_nao_e_visivel(cliente):
    gateway.r.hset("job:" + "a" * 32, mapping={"status": "concluido", "dono": "outra-pessoa"})
    r = cliente.get("/jobs/" + "a" * 32, headers={"Authorization": f"Bearer {token(cliente)}"})
    assert r.status_code == 404
