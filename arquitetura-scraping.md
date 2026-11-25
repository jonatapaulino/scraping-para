# Blueprint da Arquitetura: Coletor Distribuído de Dados Públicos
Padrão Arquitetural: Orientado a Eventos (Event-Driven) e Microsserviços.
Estratégia de Deploy: Containerização (Docker) em ambiente Linux.

1. Camada de Disparo e Orquestração (Control Plane)
Responsável por ditar quando as coisas acontecem, sem executar o trabalho pesado.

Agendador (Scheduler): Um serviço dedicado apenas a disparar eventos baseados em tempo (cronograma). Ele injeta a mensagem "Inicie a raspagem da SEFA" no mensageiro nos dias e horários estipulados.

API de Controle (FastAPI): Uma interface REST (OpenAPI compliant) para gestão. Permite monitorar o status das filas, pausar raspagens, e disparar execuções manuais sob demanda sem depender do agendador.

2. Camada de Mensageria (Espinha Dorsal)
Garante que nenhum documento seja perdido se a internet cair ou o portal do governo sair do ar.

Message Broker (RabbitMQ): Atua como o buffer entre a descoberta de links e o download real.

Topologia de Filas:

q.discovery: Recebe os links raízes (ex: página inicial de licitações da SEDUC).

q.download: Recebe os links diretos terminados em .pdf.

DLX (Dead Letter Exchange): Fila de "cemitério". Se um documento falhar 5 vezes seguidas (ex: link quebrado ou 404), ele vai para essa fila para análise manual, evitando que o sistema fique em loop infinito.

3. Camada de Processamento (Worker Nodes)
Totalmente desacoplada. Você pode escalar de 1 para 50 workers dependendo da capacidade da sua máquina ou rede.

Workers de Descoberta (Scout): Consumidores da q.discovery. Eles navegam pelas páginas, lidam com paginação e extraem as URLs dos PDFs. São idempotentes: não salvam nada, apenas mapeiam e jogam as URLs encontradas na q.download.

Workers de Download (Fetcher): Consumidores da q.download. A única missão deles é puxar o arquivo binário da internet para a sua infraestrutura.

4. Camada de Armazenamento (Dual-Storage)
Separação estrita entre o "arquivo físico" e a "ficha catalográfica".

Object Storage (MinIO): O cofre dos arquivos. Os workers fazem stream do PDF diretamente para o bucket (ex: raw-docs/sefa/2026/arquivo.pdf). Totalmente compatível com S3, preparando o terreno para futuros pipelines de IA (RAG).

Banco Relacional (PostgreSQL): O cérebro dos metadados. Armazena a proveniência do dado.

Deduplicação via Hash: Antes de baixar, o sistema verifica o hash SHA-256 do arquivo (ou da URL). Se já existir no banco, o download é ignorado. Isso economiza banda e impede PDFs duplicados caso a secretaria suba o mesmo arquivo em duas páginas diferentes.

5. Mecanismos de Resiliência (Anti-Caos)
Backoff Exponencial: Se o servidor da PRODEPA der Timeout, o worker espera 2 minutos. Se falhar de novo, espera 4, depois 8, até o limite de tentativas.

Rate Limiting no Consumo: O RabbitMQ é configurado com Prefetch = 1 e os workers possuem delays injetados para garantir que não farão ataque de negação de serviço (DDoS) acidental nos portais do estado.

Rotação de Headers: Para sistemas legados mais chatos, os workers alternam os User-Agents a cada requisição.