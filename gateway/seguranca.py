"""Autenticacao (JWT), pseudonimizacao e rate limit do gateway."""
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone

import jwt
import redis
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from comum import config

log = logging.getLogger("gateway.seguranca")

SEGREDO_PADRAO = "troque-este-segredo"
JWT_SEGREDO = config.texto("JWT_SEGREDO", SEGREDO_PADRAO)
JWT_ALGORITMO = "HS256"
JWT_EXPIRA_MIN = config.inteiro("JWT_EXPIRA_MIN", 30)
JWT_EMISSOR = "plataforma-ia-gateway"
RATE_LIMIT_POR_MINUTO = config.inteiro("RATE_LIMIT_POR_MINUTO", 30)
RATE_LIMIT_LOGIN_POR_MINUTO = config.inteiro("RATE_LIMIT_LOGIN_POR_MINUTO", 10)

if JWT_SEGREDO == SEGREDO_PADRAO:
    log.warning("JWT_SEGREDO com valor padrao: defina um segredo forte em producao")


def carregar_usuarios(bruto: str) -> dict:
    """USUARIOS='ana:senha1,bruno:senha2' (contas de demonstracao)."""
    usuarios = {}
    for par in filter(None, (p.strip() for p in bruto.split(","))):
        nome, _, senha = par.partition(":")
        if nome and senha:
            usuarios[nome] = senha
    return usuarios


USUARIOS = carregar_usuarios(config.texto("USUARIOS", "demo:demo123"))


def pseudonimo(valor: str) -> str:
    """HMAC do identificador: o restante do sistema nunca ve o nome do usuario/IP."""
    return hmac.new(JWT_SEGREDO.encode(), valor.encode(), hashlib.sha256).hexdigest()[:24]


def verificar_credenciais(usuario: str, senha: str) -> bool:
    esperado = USUARIOS.get(usuario, "")
    # compare_digest roda mesmo quando o usuario nao existe (evita ataque de tempo)
    return hmac.compare_digest(senha.encode(), esperado.encode()) and bool(esperado)


def emitir_token(usuario: str) -> tuple[str, datetime]:
    expira = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRA_MIN)
    carga = {"sub": usuario, "iss": JWT_EMISSOR, "iat": datetime.now(timezone.utc), "exp": expira}
    return jwt.encode(carga, JWT_SEGREDO, algorithm=JWT_ALGORITMO), expira


_portador = HTTPBearer(auto_error=False)


def usuario_atual(credenciais: HTTPAuthorizationCredentials | None = Depends(_portador)) -> str:
    if credenciais is None:
        raise HTTPException(401, "token ausente", headers={"WWW-Authenticate": "Bearer"})
    try:
        carga = jwt.decode(credenciais.credentials, JWT_SEGREDO, algorithms=[JWT_ALGORITMO],
                           issuer=JWT_EMISSOR, options={"require": ["exp", "sub", "iss"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token expirado", headers={"WWW-Authenticate": "Bearer"})
    except jwt.InvalidTokenError:
        raise HTTPException(401, "token invalido", headers={"WWW-Authenticate": "Bearer"})
    return carga["sub"]


def ip_cliente(request: Request) -> str:
    # Atras do proxy do Render/Caddy o IP real vem em X-Forwarded-For.
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


class LimitadorTaxa:
    """Janela fixa de 1 minuto no Redis (compartilhada entre replicas do gateway).

    Se o Redis cair, o limitador FALHA ABERTO (deixa passar e registra aviso):
    preferimos disponibilidade a bloquear todo mundo por falta do contador.
    """

    def __init__(self, cliente_redis: redis.Redis):
        self.redis = cliente_redis

    def verificar(self, chave: str, limite: int) -> None:
        agora = time.time()
        janela = int(agora // 60)
        k = f"rl:{chave}:{janela}"
        try:
            pipe = self.redis.pipeline()
            pipe.incr(k)
            pipe.expire(k, 61)
            contagem = pipe.execute()[0]
        except redis.RedisError:
            log.warning("rate limit indisponivel (redis fora): falhando aberto")
            return
        if contagem > limite:
            raise HTTPException(429, f"limite de {limite} requisicoes/minuto excedido",
                                headers={"Retry-After": str(60 - int(agora) % 60)})
