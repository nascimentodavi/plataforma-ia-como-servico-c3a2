# Worker de fila. Contexto de build = raiz do repositorio (usa a pasta comum/).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8002
WORKDIR /app

COPY comum/requirements.txt /tmp/req-comum.txt
COPY worker/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req-comum.txt -r /tmp/req.txt

COPY comum/ ./comum/
COPY worker/ ./

RUN useradd --create-home --uid 10001 app
USER app

# A porta serve apenas o /saude (healthcheck e "despertar" no free tier).
EXPOSE 8002
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/saude', timeout=3)"
CMD ["python", "worker.py"]
