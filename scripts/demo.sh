#!/usr/bin/env bash
# Teste ponta a ponta da plataforma (so precisa de bash + curl).
#   ./scripts/demo.sh                          -> testa http://localhost:8000
#   ./scripts/demo.sh https://ia.nascimentodavi.dev.br -> testa o deploy publico
set -euo pipefail

URL="${1:-http://localhost:8000}"
USUARIO="${USUARIO:-demo}"
SENHA="${SENHA:-demo123}"
campo() { sed -n "s/.*\"$1\":\"\{0,1\}\([^\",}]*\).*/\1/p"; }

echo "== 1. saude (publica)"
curl -fsS "$URL/saude"; echo

echo "== 2. rota protegida SEM token (espera 401)"
codigo=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/predict" \
  -H 'Content-Type: application/json' -d '{"texto":"teste"}')
echo "HTTP $codigo"; [ "$codigo" = "401" ]

echo "== 3. login (JWT)"
TOKEN=$(curl -fsS -X POST "$URL/login" -H 'Content-Type: application/json' \
  -d "{\"usuario\":\"$USUARIO\",\"senha\":\"$SENHA\"}" | campo access_token)
[ -n "$TOKEN" ] && echo "token recebido (${#TOKEN} caracteres)"
AUTH="Authorization: Bearer $TOKEN"

echo "== 4. predicao sincrona (gateway -> inferencia)"
curl -fsS -X POST "$URL/predict" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"texto":"servico excelente, recomendo"}'; echo

echo "== 5. documento assincrono (gateway -> fila -> worker -> inferencia -> qdrant)"
JOB=$(curl -fsS -X POST "$URL/documentos" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"texto":"a entrega atrasou e o produto chegou quebrado"}' | campo job_id)
echo "job_id=$JOB"
for _ in $(seq 1 30); do
  STATUS=$(curl -fsS "$URL/jobs/$JOB" -H "$AUTH")
  echo "$STATUS" | grep -q '"status":"concluido"' && break
  sleep 1
done
echo "$STATUS"; echo "$STATUS" | grep -q '"status":"concluido"'

echo "== 6. busca semantica (armazenamento vetorial)"
curl -fsS -X POST "$URL/busca" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"texto":"produto quebrado","k":3}'; echo

echo "== 7. eliminacao a pedido do titular (LGPD) -> 204"
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' -X DELETE "$URL/documentos/$JOB" -H "$AUTH"

echo "== 8. metricas"
curl -fsS "$URL/metricas"; echo

echo
echo "OK - fluxo completo funcionando. Traces: Jaeger (http://localhost:16686 localmente)."
