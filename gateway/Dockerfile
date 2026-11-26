# API Gateway. Contexto de build = raiz do repositorio (usa a pasta comum/).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app

COPY comum/requirements.txt /tmp/req-comum.txt
COPY gateway/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req-comum.txt -r /tmp/req.txt

COPY comum/ ./comum/
COPY gateway/ ./

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/saude', timeout=3)"
# --proxy-headers: confia no X-Forwarded-Proto/For do proxy TLS (Apache/Caddy)
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
