# Manifests de Kubernetes (extensao opcional)

Equivalente em Kubernetes do `docker-compose.yml`. Nao foram executados em um
cluster real (o edital permite), mas sao validos e seguem as mesmas decisoes:

| Arquivo | Conteudo |
|---|---|
| `00-base.yaml` | Namespace, ConfigMap e Secret (exemplo) |
| `10-dados.yaml` | Redis e Qdrant (StatefulSet com volume) + Jaeger |
| `20-aplicacao.yaml` | gateway, inferencia e worker (Deployments + Services + probes) |
| `30-escalonamento.yaml` | HPA do gateway (CPU) e **KEDA ScaledObject** do worker (tamanho da fila) |
| `40-ingress.yaml` | Ingress com TLS (cert-manager / Let's Encrypt) |

```bash
kubectl apply -f k8s/
```

As imagens esperadas sao `ghcr.io/nascimentodavi/plataforma-ia-<servico>:latest`
(gere com `docker build -f <servico>/Dockerfile -t ... .` a partir da raiz).

**Autoescalonamento por fila:** o `ScaledObject` do KEDA observa o numero de
mensagens pendentes do consumer group `workers` no stream `jobs` e escala o
worker de 1 a 10 replicas (1 replica a cada 20 mensagens pendentes). Isso so e
possivel porque o worker e *stateless* e usa consumer group: varias replicas
dividem a fila sem processar a mesma mensagem duas vezes ao mesmo tempo.
