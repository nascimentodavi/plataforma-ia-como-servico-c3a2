import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.join(RAIZ, "gateway"))

os.environ.setdefault("JWT_SEGREDO", "segredo-de-teste")
os.environ.setdefault("USUARIOS", "ana:senha-da-ana")
os.environ.setdefault("RATE_LIMIT_POR_MINUTO", "3")
