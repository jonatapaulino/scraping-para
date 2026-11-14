Atue como um Arquiteto de Software Sênior e Engenheiro Python. Seu objetivo é gerar o código-fonte e o arquivo `docker-compose.yml` para uma malha de web scraping distribuída, orientada a eventos e altamente resiliente, focada em extrair documentos (PDFs) de portais governamentais do Estado do Pará.

A adoção do Specification-Driven Development (SDD) é obrigatória. Siga estritamente as definições arquiteturais, o schema do banco de dados e os contratos da API detalhados abaixo.

### 1. Blueprint da Arquitetura Distribuída

A infraestrutura roda via Docker Compose e utiliza o ecossistema Python (FastAPI, Celery, SQLAlchemy).

*   **Camada de Orquestração (Control Plane):** 
    *   Agendador `Celery Beat` para disparos automatizados.
    *   API REST em `FastAPI` para gestão, monitoramento e disparos sob demanda.
*   **Camada de Mensageria (Espinha Dorsal):** 
    *   `RabbitMQ` com filas duráveis. 
    *   Fila `q.discovery` (recebe URLs sementes).
    *   Fila `q.download` (recebe URLs finais de PDFs).
    *   Dead Letter Exchange (DLX) para tarefas que falham excessivamente.
*   **Camada de Processamento (Workers Celery):** 
    *   `Worker Discovery`: Navega nas páginas, extrai links e enfileira PDFs. Implementado com `httpx` (para HTML estático) roteado através de um pool de proxies rotativos.
    *   `Worker Render`: Um serviço isolado do Browserless (Headless Chrome) consumido pelos workers Python para lidar exclusivamente com páginas que exigem renderização de JavaScript pesado.
    *   `Worker Download`: Puxa os binários da internet. Aplica *backoff* exponencial em caso de falhas de rede.
*   **Camada de Armazenamento (Dual-Storage):** 
    *   `MinIO` (S3-compatible) para salvar os arquivos PDFs brutos.
    *   `PostgreSQL` para controle de estado, metadados e deduplicação via hash.
*   **Camadas de Resiliência e Segurança:**
    *   **Validação Anti-Lixo (Magic Bytes):** Antes de salvar no MinIO, o worker deve inspecionar os primeiros bytes do arquivo na memória. Só salva se possuir a assinatura `%PDF-`.
    *   **Observabilidade:** O painel `Flower` deve estar configurado no `docker-compose` para monitoramento das filas do RabbitMQ/Celery. Logs devem ser estruturados em JSON.

---

### 2. Especificação SDD: Schema do PostgreSQL

Utilize o SQLAlchemy (v2.0+) com tipagem estrita (Type Hints e Pydantic) para mapear as seguintes tabelas:

**Tabela: `secretaria`**
*   `id`: UUID (Primary Key)
*   `sigla`: VARCHAR(20) (Ex: 'SEFA', 'SEDUC') - Unique
*   `url_base`: VARCHAR(255)
*   `is_ativa`: BOOLEAN (Default True)

**Tabela: `scraping_job`** (Rastreia uma execução em lote)
*   `id`: UUID (Primary Key)
*   `secretaria_id`: UUID (Foreign Key -> secretaria.id)
*   `status`: ENUM ('RUNNING', 'COMPLETED', 'FAILED')
*   `total_pdfs_encontrados`: INTEGER
*   `started_at`: TIMESTAMP
*   `completed_at`: TIMESTAMP (Nullable)

**Tabela: `documento`** (O arquivo em si)
*   `id`: UUID (Primary Key)
*   `secretaria_id`: UUID (Foreign Key -> secretaria.id)
*   `job_id`: UUID (Foreign Key -> scraping_job.id)
*   `url_origem`: TEXT (Unique - previne enfileirar a mesma URL duas vezes)
*   `hash_sha256`: VARCHAR(64) (Unique - deduplicação física do binário)
*   `minio_path`: VARCHAR(255) (Ex: 'raw-docs/sefa/2026/arquivo.pdf')
*   `tamanho_bytes`: INTEGER
*   `magic_byte_valido`: BOOLEAN
*   `status_download`: ENUM ('PENDING', 'SUCCESS', 'FAILED')
*   `created_at`: TIMESTAMP

---

### 3. Especificação SDD: Contratos OpenAPI (FastAPI)

A API deve ser construída utilizando Pydantic v2 para validação rigorosa dos *payloads*.

**POST /api/v1/jobs/start**
*   **Descrição:** Inicia uma raspagem manual, injetando as URLs sementes no RabbitMQ (via Celery `delay`).
*   **Payload (JSON):** 
    ```json
    {
      "secretarias": ["SEFA", "PRODEPA"],
      "force_full_scrape": false
    }
    ```
*   **Response (202 Accepted):** Retorna o `job_id` gerado para acompanhamento.

**GET /api/v1/jobs/{job_id}/status**
*   **Descrição:** Retorna as métricas daquele job específico do PostgreSQL.
*   **Response (200 OK):**
    ```json
    {
      "job_id": "uuid",
      "status": "RUNNING",
      "pdfs_descobertos": 1540,
      "pdfs_baixados_sucesso": 1200,
      "pdfs_falhas": 3
    }
    ```

**GET /api/v1/documents**
*   **Descrição:** Lista os metadados dos documentos com paginação (FastAPI Pagination). Permite filtros por `secretaria_id` e `status_download`.

---
**Entregáveis solicitados à IA:**
1.  O arquivo `docker-compose.yml` completo com Postgres, RabbitMQ, MinIO, Browserless, Flower, API e Workers.
2.  Os modelos SQLAlchemy (models.py).
3.  As rotas da API em FastAPI (routers.py).
4.  O código dos workers do Celery demonstrando a injeção na fila e a lógica de *Magic Bytes* (tasks.py).