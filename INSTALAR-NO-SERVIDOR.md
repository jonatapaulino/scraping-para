# Instalar no servidor (para quem administra a máquina)

Sistema de coleta de dados públicos do Pará (Portal da Transparência, Diário Oficial, PNCP, portais de secretarias). Tudo roda em Docker Compose.

## O que sobe
| Serviço | Imagem | Para que |
|---|---|---|
| `postgres` | postgres:16-alpine | banco (histórico, fila de coletas) |
| `rabbitmq` | rabbitmq:3.13-management | filas de tarefas |
| `browserless` | browserless/chrome (digest fixo) | Chrome headless para páginas com JavaScript |
| `api` | build local | painel web + API (porta 8000) |
| `worker-discovery`, `worker-download`, `worker-control` | build local | coletores |
| `beat` | build local | agendador |
| `flower` | build local | monitor dos workers (opcional) |

**Recursos**: ~4 GB de RAM, 2–4 núcleos. Disco: o que for baixado (hoje ~18 GB, deve crescer para algumas centenas de GB).

**Rede**: só tráfego de saída (HTTPS para `*.sistemas.pa.gov.br`, `ioepa.com.br`, `pncp.gov.br` e portais `*.pa.gov.br`). Nenhuma porta precisa ficar aberta para fora: todas são publicadas só em `127.0.0.1`.

## Passo a passo
```bash
git clone https://github.com/jonatapaulino/scraping-para.git
cd scraping-para
cp .env.example .env
```

No `.env`, ajuste:
```bash
# senhas novas (instalação limpa: pode e deve trocar as padrão)
POSTGRES_PASSWORD=<senha forte>
RABBITMQ_PASS=<senha forte>
BROWSERLESS_TOKEN=<token qualquer>

# pasta onde os arquivos baixados ficam (ÚNICA cópia). Ideal: um diretório
# que o usuário jonata enxergue na home dele, para acompanhar os arquivos.
DOWNLOADS_DIR=/caminho/no/host/jonata/scraper-downloads
```

Subir:
```bash
docker compose up -d --build
curl -s localhost:8000/health    # {"status":"ok", ...}
```

O painel fica em `http://127.0.0.1:8000/ui` **na própria máquina**. Ele **não tem login**, então não deve ser exposto sem proteção. Para o usuário acessar, escolha uma das opções:
- **Cloudflare Tunnel + Access** (como já é feito com o SSH): publicar `http://localhost:8000` num hostname, por exemplo `painel-jonata.npca.tec.br`, protegido por uma política do Access só para o e-mail dele;
- ou deixar a porta 8000 acessível a partir do container SSH dele, que então acessa por túnel SSH (`ssh -L 8000:<host>:8000 ...`).

## Operação
- **Reinício**: todos os serviços usam `restart: unless-stopped` e voltam sozinhos depois de um reboot.
- **Atualizar**: `git pull && docker compose up -d --build`.
- **Logs**: `docker compose logs -f api worker-download`.
- **Parar**: `docker compose down`. Os dados ficam nos volumes `postgres-data`/`rabbitmq-data` e na pasta `DOWNLOADS_DIR`.

## Migração dos dados que já existem
Depois de subir, o banco e os arquivos atuais vêm do computador do Jonatã (um dump do Postgres e a pasta `downloads/`). Isso é feito em conjunto, num segundo momento.
