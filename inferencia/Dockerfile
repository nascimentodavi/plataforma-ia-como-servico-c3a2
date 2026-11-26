# Servico de inferencia. Contexto de build = raiz do repositorio (usa a pasta comum/).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8001
WORKDIR /app

COPY comum/requirements.txt /tmp/req-comum.txt
COPY inferencia/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req-comum.txt -r /tmp/req.txt

COPY comum/ ./comum/
COPY inferencia/ ./
# O modelo e treinado no build: a imagem ja sai pronta e funciona offline.
RUN python treinar.py

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/saude', timeout=3)"
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
