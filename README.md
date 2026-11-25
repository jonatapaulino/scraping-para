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
| `worker-control` | Tarefas curtas de controle (agenda diária, reconciliador) na fila `q.control` — não esperam atrás do crawl | — |
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

Todas as portas ficam **só neste computador** (`127.0.0.1`) — ninguém na mesma rede Wi-Fi alcança o banco, o MinIO ou o painel. Para abrir o painel a outros aparelhos da rede, defina `PAINEL_BIND=0.0.0.0` no `.env` (o painel não tem login: só faça isso em rede confiável).

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

### Filtro de PDFs (por fonte)

Por padrão o crawl só baixa PDFs com cara de material financeiro (`CRAWL_PDF_ALLOW`: edital, contrato, convênio…). Portais de **folha/remuneração** etc. ficam de fora — nesses casos defina o filtro da fonte no painel (campo *PDFs* ao cadastrar, ou ✎ na tabela): vazio = padrão · `*` = todos os PDFs do site · ou uma regex (ex.: `remunera|inativ`). Vale a partir da próxima tarefa.

### Fonte de dados genérica (qualquer site, sem código)

Para sites que não são só lista de PDFs — painel, seção **⚙ Fonte de dados**:

- **API / JSON**: a URL devolve JSON; a lista de registros é achada sozinha (ou informe o caminho, ex.: `dados.itens`). Objetos aninhados viram colunas (`a.b`).
- **Página com tabela (HTML)**: vira linhas/colunas (a maior tabela da página, ou o seletor CSS informado). Links dentro das células ganham uma coluna extra `<coluna> (link)`. Marque *página com JavaScript* se a tabela só aparece com JS (usa o Chrome headless).
- **Paginação**: ponha `{page}` na URL (`https://x/api?pagina={page}`); anda até vir vazia/repetida ou até *páginas*.
- **Cabeçalhos** (avançado): `Authorization: Bearer …` etc.
- Use **🔍 Testar** antes de salvar: mostra colunas e amostra sem gravar nada.
- Saída: um CSV por coleta em `downloads/<sigla>/<ano>/<sigla>-<data_hora>.csv` + MinIO. Conteúdo idêntico ao da coleta anterior não gera arquivo novo.
- Para rodar todo dia sozinha, use a **⏰ Agenda diária** da fonte (ver abaixo).

### Agenda diária por fonte

Na tabela *Secretarias*, coluna **⏰ Agenda diária**: marque a caixa da fonte (portal de PDFs ou fonte de coleta) e escolha o horário — ela é raspada todo dia nesse horário (fuso `AGENDA_UTC_OFFSET_HORAS`, padrão `-3`, Brasília/Pará). O Beat confere a cada 5 minutos; se o computador/Docker estava desligado na hora, a fonte roda assim que ele voltar (uma vez por dia). Fonte desativada não roda. O **IOEPA (Diário Oficial)** também pode ser agendado: a cada dia coleta o ano corrente e baixa só as edições novas. Fontes com agenda própria ficam fora da rodada global (`CELERY_BEAT_CRON`/`BEAT_CRAWL_ENABLED`), para não rodar duas vezes.

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
| `PATCH /api/v1/secretarias/{id}` | Ativa/desativa, edita `url_base`, `max_depth` e `pdf_filtro` |
| `DELETE /api/v1/secretarias/{id}` | Exclui a fonte e o histórico no banco (arquivos baixados ficam); recusa fontes do sistema e fontes com tarefa rodando |
| `POST /api/v1/coletas/testar` | Pré-visualiza uma fonte genérica (1ª página, sem gravar): colunas + primeiras linhas |
| `POST /api/v1/coletas` / `PUT /api/v1/coletas/{id}` | Cadastra / edita uma fonte genérica (`url`, `modo`, `caminho`, `max_paginas`, …) |
| `POST /api/v1/jobs/{id}/cancel` | Cancela job em andamento (`CANCELLED`); descarta pendentes e as tarefas ainda na fila viram no-op |
| `GET /api/v1/jobs/{id}/download` | `.zip` com todos os arquivos (PDF/CSV) baixados pelo job |
| `GET /api/v1/documents/{id}/file?download=true` | Baixa o arquivo (sem o parâmetro, abre no navegador) |
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
| `PROXY_POOL` | — | Proxies separados por `;` ou `,` p/ discovery |
| `PAINEL_BIND` | `127.0.0.1` | Endereço do painel; `0.0.0.0` abre para a rede local |
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
- **Fila automática**: *➕ Pôr na fila* em vez de *Coletar* — as coletas rodam uma por vez, em ordem, e a próxima começa sozinha quando a anterior termina (`avancar_fila` no `worker-control`, a cada minuto). Falha não trava a fila; sem *recoletar*, partes que já deram certo são puladas e só as que falharam são refeitas. API: `GET/POST /api/v1/fila`, `DELETE /api/v1/fila/{id}`, `POST /api/v1/fila/limpar`
- **Unidade = um CSV por órgão**: cada órgão selecionado gera um arquivo próprio; "todos" expande para **um CSV por órgão do período**
- **Saída**: `downloads/<SIGLA>/<dataset>/<recorte>/<dataset>-<recorte>-<SIGLA>.csv` (UTF-8 com BOM) + MinIO — pasta por sigla, igual ao crawl de PDFs
  - a pasta usa a **sigla oficial** (a da API de despesas), sem acento nem espaço: nomes por extenso, siglas de outras APIs e apelidos (`PCPA`, `Casa Civil`, `FSCMP`…) caem na mesma pasta. A tabela `APELIDOS` em `app/transparencia.py` lista as equivalências
  - datasets sem órgão ou varredura única (`diarias` com "todos") → `downloads/<dataset>/<recorte>/`
  - as colunas do CSV são a união de todas as linhas (campo que só aparece em parte dos registros não é descartado)
  - órgão sem nenhum registro no período fica como **Sem dados** (não conta como falha)
- **Documentos PDF**: `transf_voluntarias` também baixa termo + plano de trabalho de cada convênio em `<SIGLA DO CONCEDENTE>/transf_voluntarias/documentos/<nº convênio>/{termo,plano_trabalho}.pdf` (dedup por hash quando os dois ids apontam pro mesmo arquivo)
- **Dedup**: cada (dataset × mês × seleção) é um documento — recoletar exige marcar *recoletar*
- A fonte aparece como `TRANSPARENCIA` (tipo `api`) — não entra nas rodadas de crawl do Beat
- Tuning: `FOLHA_PAGE_SIZE` (registros/página), `FOLHA_PAGE_DELAY_SECONDS` (pausa entre páginas)

## Resiliência

- **Magic bytes**: só grava arquivos que começam com `%PDF-`
- **Um caminho por URL**: o nome do PDF ganha um sufixo curto da URL (`edital__3fa2c1d0.pdf`) — dois arquivos de mesmo nome no mesmo ano não se sobrescrevem. O ano também é lido de pastas `AAAA-MM` (ex.: `/files/2023-01/`)
- **Contador de progresso idempotente**: um documento só desconta a pendência do job ao sair de `PENDING` — reentrega, reprocesso da fila de erros e o reconciliador não descontam duas vezes
- **Dedup**: URL de origem e SHA-256 únicos — jobs repetidos não baixam de novo
- **Re-crawl**: páginas deduplicadas expiram após `CRAWL_DEDUP_HOURS` — rodadas agendadas descobrem documentos novos em páginas já conhecidas
- **Cancelar**: interrompe também os loops longos (PDFs anexos do Diário/PNCP/convênios); documentos que já tinham arquivo continuam como baixados
- **Sem baixar de novo**: com *recoletar*, o PDF só é baixado se o site informar tamanho/data diferentes (requisição `HEAD`); fonte excluída e recadastrada reaproveita os arquivos já guardados (mesmo tamanho) em vez de duplicar
- **Retry**: 5 tentativas com backoff `120s × 2ⁿ` (máx. 1h); 4xx é falha terminal; falhas de MinIO/rede também são retriáveis
- **DLX**: tarefas esgotadas vão para `q.dead` (visível no painel, reprocessável pelo botão *reprocessar*)
- **Reconciliador**: task periódica do Beat marca documentos `PENDING` órfãos e jobs `RUNNING` zumbis (sem nenhuma atividade há `JOB_STALL_HOURS`) como `FAILED` — coletas longas que seguem trabalhando não são interrompidas
- **Coleta genérica**: não acessa endereços internos (serviços do próprio sistema, `localhost`, `host.docker.internal`, 169.254.x) — nem por redirecionamento
- **JS**: páginas sem links no HTML bruto são rerenderizadas via Browserless

## Juntar pastas de órgão duplicadas (uma vez, após atualizar)

Versões antigas criavam pastas diferentes para o mesmo órgão (`IDEFLORBIO`, `IDEFLOR_-_BIO`, nomes com acento quebrado como `AG_NCIA_...`). O script abaixo junta tudo na pasta da sigla oficial — no MinIO, em `downloads/` e no banco. Ele **só simula** sem `--aplicar` e nunca sobrescreve um arquivo diferente (conflitos ficam onde estão e aparecem no relatório):

```bash
docker compose exec worker-download python -m app.juntar_pastas            # mostra o plano
docker compose exec worker-download python -m app.juntar_pastas --aplicar  # executa
```

## Estrutura

```
app/
├── main.py          # FastAPI: app, /ui estático
├── routers.py       # contratos /api/v1
├── celery_app.py    # Celery + beat schedule
├── tasks.py         # discover_page / download_document / datasets / coleta
├── juntar_pastas.py # manutenção: junta pastas de órgão duplicadas
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
