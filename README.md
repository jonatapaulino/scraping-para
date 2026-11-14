# Scraper Gov-PA

Malha de web scraping distribuída, orientada a eventos, para extrair documentos (PDFs) de portais governamentais do Estado do Pará.

Você cadastra secretarias, dispara raspagens (manual ou agendada) e os PDFs caem automaticamente em `downloads/<sigla>/<ano>/` — além de ficarem versionados no MinIO com deduplicação por SHA-256.

## Arquitetura

```
┌──────────┐   ┌────────────┐   ┌──────────────────┐   ┌────────────┐
│ FastAPI  │──▶│  RabbitMQ  │──▶│ worker-discovery │──▶│  RabbitMQ  │
│ (API+UI) │   │q.discovery │   │ (httpx/BS4 +     │   │ q.download │
└──────────┘   └────────────┘   │  Browserless)    │   └─────┬──────┘
      │                        └──────────────────┘         │
      │                                                     ▼
      │                        ┌────────────┐   ┌──────────────────┐
      │                        │    DLX     │◀──│ worker-download  │
      │                        │  (q.dead)  │   │ (magic bytes,    │
      │                        └────────────┘   │  SHA-256, retry) │
      ▼                                         └───────┬──────────┘
┌────────────┐   ┌──────────┐                           │
│ PostgreSQL │   │   Beat   │                           ▼
│ (metadados)│   │ (agenda) │              ┌─────────────────────────┐
└────────────┘   └──────────┘              │ MinIO + downloads/ (PC) │
                                           └─────────────────────────┘
```

| Serviço | Papel | Porta |
|---|---|---|
| `api` | FastAPI — REST + dashboard em `/ui` | 8000 |
| `worker-discovery` | Navega páginas, extrai links PDF e links internos | — |
| `worker-download` | Baixa PDFs, valida `%PDF-`, deduplica, grava MinIO + disco | — |
| `beat` | Dispara jobs para secretarias ativas (cron) | — |
| `rabbitmq` | Filas `q.discovery`, `q.download` + DLX `q.dead` | 5672 / 15672 |
| `postgres` | Metadados: secretarias, jobs, documentos, páginas | 5432 |
| `minio` | Storage S3 dos binários | 9000 / 9001 |
| `browserless` | Chrome headless p/ páginas com JS | 3000 |
| `flower` | Monitor dos workers Celery | 5555 |

## Subindo o sistema

Pré-requisito: Docker Desktop rodando.

**Windows:** duplo clique em `iniciar.bat` (sobe tudo e abre o painel). `parar.bat` desliga.

**Manual:**

```bash
cp .env.example .env   # ajuste se quiser
docker compose up -d --build
```

| Acesso | URL | Login |
|---|---|---|
| **Painel** | http://localhost:8000/ui | — |
| API docs | http://localhost:8000/docs | — |
| MinIO console | http://localhost:9001 | minioadmin / minioadmin |
| Flower | http://localhost:5555 | — |
| RabbitMQ | http://localhost:15672 | scraper / scraper |

## Como usar

1. **Cadastrar** — seção *Secretarias*: sigla + URL base do portal (pode ser a home; o crawler navega links do mesmo domínio).
2. **Disparar** — seção *Iniciar raspagem*: marque a secretaria, ajuste a **profundidade** e clique **Disparar job** (uma vez — o job baixa todos os PDFs que encontrar).
3. **Acompanhar** — *Jobs recentes*: `RUNNING` → `COMPLETED`. Arquivos caem em `downloads/<sigla>/<ano>/`.
4. **Automático** — secretarias ativas entram na rodada do Beat (`CELERY_BEAT_CRON`, padrão 6h da manhã). Deduplicação por URL/SHA-256 garante só documentos novos.

### Profundidade (depth)

Distância em cliques da página cadastrada até a página que contém o link do PDF:

- `0` — só a página cadastrada
- `1` — ela + links diretos
- `2` — + segundo nível (padrão)
- `3-4` — varredura funda (portais onde docs ficam dentro de posts)

Dica: abra o portal e clique manualmente até achar a página com os PDFs — o número de cliques é a profundidade. Cada nível multiplica páginas visitadas (2 s/página por worker).

## Escalando

```bash
docker compose up -d --scale worker-discovery=4 --scale worker-download=8
```

## API (contratos)

| Endpoint | Descrição |
|---|---|
| `POST /api/v1/jobs/start` | `{"secretarias": ["SEFA"], "force_full_scrape": false, "max_depth": 2}` → 202 + job_ids |
| `GET /api/v1/jobs/{id}/status` | Status e métricas do job |
| `GET /api/v1/jobs` | Lista de jobs recentes |
| `GET /api/v1/documents` | Paginado; filtros `secretaria_id`, `status_download` |
| `GET/POST /api/v1/secretarias` | CRUD de secretarias |
| `GET /api/v1/overview` | Filas + agregados (cards do painel) |

## Configuração (.env)

| Variável | Padrão | Descrição |
|---|---|---|
| `CELERY_BEAT_CRON` | `0 6 * * *` | Agendamento dos jobs automáticos |
| `DISCOVERY_MAX_DEPTH` | `2` | Profundidade padrão (jobs sem `max_depth`) |
| `DISCOVERY_DELAY_SECONDS` / `DOWNLOAD_DELAY_SECONDS` | `2` | Rate limit por worker |
| `MAX_RETRIES` / `RETRY_BASE_SECONDS` | `5` / `120` | Backoff exponencial → DLX |
| `LOCAL_EXPORT_DIR` | `/downloads` | Espelho local (mapeado p/ `./downloads` no host) |
| `SEED_SECRETARIAS` | — | `SIGLA=url;SIGLA2=url2` p/ cadastrar no boot |
| `PROXY_POOL` | — | Proxies separados por `;` p/ discovery |

## Resiliência

- **Magic bytes**: só grava arquivos que começam com `%PDF-`
- **Dedup**: URL de origem e SHA-256 únicos — jobs repetidos não baixam de novo
- **Retry**: 5 tentativas com backoff `120s × 2ⁿ` (máx. 1h); 4xx é falha terminal
- **DLX**: tarefas esgotadas vão para `q.dead` (visível no painel)
- **JS**: páginas sem links no HTML bruto são rerenderizadas via Browserless

## Estrutura

```
app/
├── main.py          # FastAPI: app, /ui estático
├── routers.py       # contratos /api/v1
├── celery_app.py    # Celery + beat schedule
├── tasks.py         # discover_page / download_document
├── models.py        # secretaria, scraping_job, documento (SQLAlchemy)
├── broker.py        # declaração das filas + DLX
├── storage.py       # MinIO
├── http_client.py   # httpx + proxies + User-Agent
└── static/          # dashboard (HTML/CSS/JS vanilla)
docker-compose.yml   # 9 serviços
iniciar.bat          # sobe a stack + abre o painel
parar.bat            # desliga (dados mantidos)
testsite/            # fixture p/ teste E2E local
```

## Teste E2E local

```bash
cd testsite && python -m http.server 8899   # terminal 1
# painel: cadastrar TESTE → http://host.docker.internal:8899/index.html, disparar
# esperado: 2 URLs → 1 objeto físico (dedup por hash)
```

Baseado nos blueprints `arquitetura-scraping.md` (event-driven) e `arquitetura-scraping1.md` (SDD/Celery).
