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

```bash
git clone https://github.com/jonatapaulino/scraping-para.git
cd scraping-para
```

**Windows:** duplo clique em `iniciar.bat` (cria `.env` do exemplo se faltar, sobe tudo e abre o painel). `parar.bat` desliga.

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
| `POST /api/v1/jobs/start` | `{"secretarias": ["SEFA"], "force_full_scrape": false, "max_depth": 2}` → 202 + job_ids (`max_depth` nulo usa o da secretaria) |
| `GET /api/v1/jobs/{id}/status` | Status e métricas do job |
| `GET /api/v1/jobs` | Lista de jobs recentes |
| `GET /api/v1/documents` | Paginado; filtros `secretaria_id`, `status_download` |
| `GET /api/v1/documents/{id}/file` | Abre o PDF (proxy do MinIO) |
| `GET/POST /api/v1/secretarias` | Lista / cadastra secretarias |
| `PATCH /api/v1/secretarias/{id}` | Ativa/desativa, edita `url_base` e `max_depth` |
| `POST /api/v1/dead-letters/requeue` | Republica a `q.dead` na fila original |
| `GET /api/v1/transparencia/opcoes?dataset=` | Períodos publicados + órgãos do dataset |
| `POST /api/v1/transparencia/start` | `{"dataset": "despesas", "ano_ini": 2025, "mes_ini": 1, "ano_fim": 2025, "mes_fim": 3, "orgaos": ["SESPA"]}` → 202 (orgaos nulo = todos; siglas, não ids) |
| `GET /api/v1/overview` | Filas + agregados (cards do painel) |

## Configuração (.env)

| Variável | Padrão | Descrição |
|---|---|---|
| `CELERY_BEAT_CRON` | `0 6 * * *` | Agendamento dos jobs automáticos |
| `DISCOVERY_MAX_DEPTH` | `2` | Profundidade padrão (jobs sem `max_depth`) |
| `DISCOVERY_DELAY_SECONDS` / `DOWNLOAD_DELAY_SECONDS` | `2` | Rate limit por worker |
| `MAX_RETRIES` / `RETRY_BASE_SECONDS` | `5` / `120` | Backoff exponencial → DLX |
| `CRAWL_DEDUP_HOURS` | `20` | Janela de dedup de páginas — após ela, re-raspa nas próximas rodadas |
| `DOC_ORPHAN_HOURS` / `JOB_STALL_HOURS` | `8` / `24` | Reconciliador: PENDING/RUNNING além do limite → `FAILED` |
| `RECONCILE_CRON` | `*/30 * * * *` | Frequência do reconciliador no Beat |
| `LOCAL_EXPORT_DIR` | `/downloads` | Espelho local (mapeado p/ `./downloads` no host) |
| `SEED_SECRETARIAS` | — | `SIGLA=url;SIGLA2=url2` p/ cadastrar no boot |
| `PROXY_POOL` | — | Proxies separados por `;` p/ discovery |
| `FOLHA_API_BASE` / `DESPESAS_API_BASE` | api-servidores… / api-despesas… | Endpoints das APIs da transparência |
| `FOLHA_PAGE_SIZE` / `FOLHA_PAGE_DELAY_SECONDS` | `1000` / `1` | Tamanho de página e pausa entre páginas |
| `GDRIVE_ENABLED` / `GDRIVE_ROOT_FOLDER_ID` | `false` / — | Espelho no Drive (ver seção) |
| `GDRIVE_CREDENTIALS_PATH` | `/secrets/gdrive.json` | JSON da service account |

## Espelho no Google Drive (opcional)

Cada arquivo concluído também pode subir ao Google Drive, organizado por sigla:

```
<pasta-raiz>/seduc/2024/decreto.pdf
<pasta-raiz>/folha/2025/01/folha-2025-01-todos.csv
```

Setup (uma vez):

1. No Google Cloud Console: crie uma **service account** e baixe a chave JSON para `./secrets/gdrive.json`
2. No Drive: compartilhe a pasta-raiz com o **email da service account** (editor)
3. Copie o ID da pasta-raiz (na URL: `drive.google.com/drive/folders/<ID>`) para `GDRIVE_ROOT_FOLDER_ID` e ative `GDRIVE_ENABLED=true`
4. `docker compose up -d --build` (recreate do worker-download)

Comportamento: pastas por sigla são achadas/criadas automaticamente dentro da raiz; re-upload sobrescreve o arquivo de mesmo nome; falha no Drive **não** marca o documento como falho (MinIO + disco já garantem — o upload é best-effort e logado).

## Dados abertos do Portal da Transparência

Além do crawl de PDFs, o sistema coleta datasets das **APIs de dados abertos** do Portal da Transparência — sem navegação, sem captcha: os endpoints públicos devolvem JSON e o sistema gera o CSV.

Datasets disponíveis (`app/transparencia.py` — adicionar um novo = funções cliente em `app/datasets.py` + 1 linha no registro):

| Dataset | Unidade | Conteúdo |
|---|---|---|
| `folha` | mês | folha de pagamento por servidor (paginado por órgão) |
| `despesas` | mês | execução orçamentária por órgão (orçado → empenhado → liquidado → pago) |
| `empenhos` | mês | notas de empenho (número, credor, valores) — mês inteiro num GET |
| `receitas` | mês | arrecadação por órgão (previsto inicial/atualizado → realizado) |
| `transf_recursos` | mês | transferências a municípios (paginação por ano; mês filtrado no cliente) |
| `bens_moveis` | mês | patrimônio por órgão: quantitativo, valor origem/atual, depreciação |
| `diarias` | intervalo | diárias de viagem — **um CSV por job** (API não filtra por período; 1,4 mi linhas varridas com filtro local por `dt_diaria`) |
| `suprimento` | ano | ordens bancárias de suprimento de fundos |
| `obras` | ano | obras públicas (situação, valores, município) |
| `areas` | ano | convênios por área temática (SAÚDE/SANEAMENTO/EDUCAÇÃO) |
| `bens_imoveis` | snapshot | inventário de imóveis ativos+inativos (sem período) |
| `transf_voluntarias` | snapshot | instrumentos/convênios vigentes (sem período) |

Indisponíveis (sem API alcançável — endpoints não respondem): `compras`, `planejamento`.

- **Painel**: seção *Dados abertos* — escolha o dataset, o intervalo e os órgãos (ou todos) e clique *Coletar*
- **Unidade = um CSV por órgão**: cada órgão selecionado gera um arquivo próprio; "todos" expande para **um CSV por órgão do período**
- **Saída**: `downloads/<SIGLA>/<dataset>/<recorte>/<dataset>-<recorte>-<SIGLA>.csv` (UTF-8 com BOM) + MinIO — pasta por sigla, igual ao crawl de PDFs
  - datasets sem órgão ou varredura única (`diarias` com "todos", `transf_voluntarias`) → `downloads/<dataset>/<recorte>/`
- **Dedup**: cada (dataset × mês × seleção) é um documento — recoletar exige marcar *recoletar*
- A fonte aparece como `TRANSPARENCIA` (tipo `api`) — não entra nas rodadas de crawl do Beat
- Tuning: `FOLHA_PAGE_SIZE` (registros/página), `FOLHA_PAGE_DELAY_SECONDS` (pausa entre páginas)

## Resiliência

- **Magic bytes**: só grava arquivos que começam com `%PDF-`
- **Dedup**: URL de origem e SHA-256 únicos — jobs repetidos não baixam de novo
- **Re-crawl**: páginas deduplicadas expiram após `CRAWL_DEDUP_HOURS` — rodadas agendadas descobrem documentos novos em páginas já conhecidas
- **Retry**: 5 tentativas com backoff `120s × 2ⁿ` (máx. 1h); 4xx é falha terminal; falhas de MinIO/rede também são retriáveis
- **DLX**: tarefas esgotadas vão para `q.dead` (visível no painel, reprocessável pelo botão *reprocessar*)
- **Reconciliador**: task periódica do Beat marca documentos `PENDING` órfãos e jobs `RUNNING` zumbis como `FAILED`
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
