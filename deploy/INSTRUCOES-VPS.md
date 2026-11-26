# Deploy na VPS (Ubuntu + Podman + Apache)

Topologia em producao:

```
Internet ──HTTPS:443──> Apache (TLS Let's Encrypt, proxy reverso)
                           ├── /        -> 127.0.0.1:8000  (gateway)
                           └── /jaeger  -> 127.0.0.1:16686 (UI do Jaeger, com senha)
                                              │
                     rede interna do Podman ──┴── inferencia, worker, redis, qdrant
                                                  (nenhum deles exposto no host)
```

O Apache ja ocupa 80/443 na VPS, entao ele faz a terminacao TLS e a stack escuta
apenas em `127.0.0.1`. Todos os comandos abaixo sao executados **como root** na VPS.
Troque `ia.seudominio.com.br` pelo seu subdominio.

---

## 0. DNS

Crie um registro **A** `ia.seudominio.com.br` apontando para o IP da VPS e confira:

```bash
dig +short ia.seudominio.com.br     # deve mostrar o IP da VPS
```

## 1. Preparar o Podman

```bash
# API compativel com Docker (usada pelo `podman compose` / docker-compose)
systemctl enable --now podman.socket
# religa os containers com restart=always depois de um reboot da VPS
systemctl enable podman-restart.service

# portas locais que a stack usa precisam estar livres:
ss -tlnp | grep -E ':(8000|16686)\s' || echo "portas livres"
free -h    # a stack completa usa ~1,2 GB de RAM
```

> Se 8000 ou 16686 estiverem ocupadas, defina `PORTA_GATEWAY=...` e/ou
> `PORTA_JAEGER=...` no `.env` e ajuste as mesmas portas no vhost do Apache.

## 2. Baixar o codigo

```bash
apt-get install -y git
cd /opt
git clone https://github.com/nascimentodavi/plataforma-ia-como-servico-c3a2.git
cd plataforma-ia-como-servico-c3a2
```

## 3. Segredos (`.env`, fica so na VPS, nunca vai para o Git)

```bash
cat > .env <<EOF
JWT_SEGREDO=$(openssl rand -hex 32)
TOKEN_INTERNO=$(openssl rand -hex 32)
QDRANT_API_KEY=$(openssl rand -hex 32)
USUARIOS=demo:demo123
EOF
chmod 600 .env
```

`USUARIOS` aceita varias contas: `demo:demo123,professor:outra-senha`.

## 4. Subir a stack (UM comando)

```bash
podman compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

O primeiro build leva alguns minutos. Conferir:

```bash
podman compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl http://127.0.0.1:8000/saude                 # {"status":"ok"}
bash scripts/demo.sh http://127.0.0.1:8000       # teste ponta a ponta
```

## 5. Apache como proxy reverso + HTTPS

O dominio fica atras do **Cloudflare** (HTTPS publico), que se conecta ao Apache
na porta 443. O vhost [apache-plataforma-ia.conf](apache-plataforma-ia.conf) reaproveita
o certificado Let's Encrypt que os outros sites da VPS ja usam
(`/etc/letsencrypt/live/nascimentodavi.dev.br/`), redireciona :80 para HTTPS e
faz proxy para o gateway. Nao e preciso mexer em DNS nem gerar certificado novo.

```bash
apt-get install -y apache2-utils
a2enmod proxy proxy_http headers ssl

cp deploy/apache-plataforma-ia.conf /etc/apache2/sites-available/plataforma-ia.conf

# usuario/senha para a UI do Jaeger (/jaeger)
htpasswd -c /etc/apache2/.htpasswd-plataforma-ia professor

a2ensite plataforma-ia
apache2ctl configtest && systemctl reload apache2
curl -k -H "Host: ia.nascimentodavi.dev.br" https://127.0.0.1/saude   # {"status":"ok"}
```

## 6. HTTPS

Feito pelo Cloudflare (borda) + certificado do Apache (origem); a renovacao do
certificado e a mesma dos demais sites da VPS. Sem Cloudflare, bastaria
`certbot --apache -d ia.nascimentodavi.dev.br`.

## 7. Validar o deploy publico

```bash
bash scripts/demo.sh https://ia.seudominio.com.br
```

No navegador:
- `https://ia.seudominio.com.br/docs` (Swagger: faca login e use **Authorize**)
- `https://ia.seudominio.com.br/painel` (painel de metricas)
- `https://ia.seudominio.com.br/jaeger` (traces, pede a senha do passo 5)

Por fim, coloque a URL publica no `README.md` (secao "URL publica").

---

## Operacao do dia a dia

| Acao | Comando (na pasta do projeto) |
|---|---|
| Atualizar para o codigo mais novo | `bash deploy/atualizar.sh` |
| Ver logs | `podman compose -f docker-compose.yml -f docker-compose.prod.yml logs -f gateway worker` |
| Escalar workers | `podman compose -f docker-compose.yml -f docker-compose.prod.yml up -d --scale worker=3` |
| Parar tudo | `podman compose -f docker-compose.yml -f docker-compose.prod.yml down` |

Dica: `alias pc='podman compose -f docker-compose.yml -f docker-compose.prod.yml'`.

## CD automatico (opcional)

O workflow `.github/workflows/ci-cd.yml` publica na VPS a cada push na `main`
depois dos testes passarem, se voce habilitar:

```bash
# na VPS: chave dedicada ao deploy
ssh-keygen -t ed25519 -N "" -f /root/.ssh/deploy_github
cat /root/.ssh/deploy_github.pub >> /root/.ssh/authorized_keys
cat /root/.ssh/deploy_github        # conteudo -> segredo VPS_CHAVE_SSH
```

No GitHub (Settings > Secrets and variables > Actions):
- **Variables:** `DEPLOY_HABILITADO = true`
- **Secrets:** `VPS_HOST` (IP/dominio), `VPS_USUARIO` (`root`), `VPS_CHAVE_SSH`,
  `VPS_DIRETORIO` (`/opt/plataforma-ia-como-servico-c3a2`)

## Problemas comuns

| Sintoma | Causa provavel / solucao |
|---|---|
| `502 Proxy Error` no navegador | stack parada: rode o `ps` do passo 4 e veja os logs |
| `required variable ... is missing` | faltou algum segredo no `.env` (passo 3) |
| `Cannot connect to the Docker daemon` | `systemctl enable --now podman.socket` |
| Abre outro site da VPS | falta o vhost `:443` do subdominio, ou o site nao foi habilitado (`a2ensite plataforma-ia`) |
| `/jaeger` sem estilos | confira se o container jaeger subiu com `QUERY_BASE_PATH=/jaeger` (vem do `docker-compose.prod.yml`) |
