"""Leitura de configuracao a partir de variaveis de ambiente.

Toda configuracao da plataforma vem do ambiente: o mesmo container roda
local (docker compose), em Kubernetes ou no Render sem alterar o codigo.
"""
import os


def texto(nome: str, padrao: str = "") -> str:
    return os.getenv(nome, padrao)


def inteiro(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, padrao))
    except ValueError:
        return padrao


def decimal(nome: str, padrao: float) -> float:
    try:
        return float(os.getenv(nome, padrao))
    except ValueError:
        return padrao
