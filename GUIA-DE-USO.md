# Guia de uso rápido

Tudo é feito pelo painel: **http://localhost:8000/ui**

## Ligar e desligar
1. Abra o **Docker Desktop**.
2. Duplo clique em `iniciar.bat` — o painel abre sozinho.
3. Para desligar: `parar.bat` (os dados ficam guardados).

## 1. Site com lista de PDFs
Seção **Secretarias** (formulário embaixo da tabela):

| Campo | O que colocar |
|---|---|
| SIGLA | nome curto da fonte (vira a pasta em `downloads/`) |
| URL | a página onde estão os links dos PDFs |
| depth | cliques até a página dos PDFs. `0` = só aquela página |
| PDFs | **vazio** = só financeiros (edital, contrato…) · `*` = **todos** · ou uma regex, ex.: `remunera\|inativ` |

Marque **raspar agora** e clique em **+ Adicionar**.
> Se a tarefa terminar com 0 PDFs, quase sempre é o filtro: coloque `*` no campo *PDFs*.

## 2. Site com API (JSON) ou tabela na página
Seção **⚙ Fonte de dados — qualquer site**:
1. Escolha *API / JSON* ou *Página com tabela*.
2. Cole a URL. Se tiver várias páginas, use `{page}`: `https://site/api?pagina={page}` e defina *páginas*.
3. Clique em **🔍 Testar** — mostra as colunas e uma amostra, sem gravar nada.
4. **Salvar fonte** (com *coletar agora* se quiser rodar já). Sai um CSV.

Não faz login, formulário nem captcha. Se a tabela só aparece com JavaScript, marque *página com JavaScript*.

## 3. Dados abertos da Transparência (folha, despesas, diárias…)
Seção **Dados abertos**: escolha o dataset, o período e os órgãos (ou *todos*) → **Coletar**.

## Acompanhar e controlar
- **Progresso das tarefas** (topo): barra de cada tarefa em andamento. **cancelar** interrompe (o que já baixou fica).
- **Tarefas recentes**: **⬇ zip** baixa tudo da tarefa; **cancelar** se ainda estiver rodando.
- **Documentos**: cada arquivo tem ⬇ para baixar.
- **Fila de erros**: botão *reprocessar* tenta de novo.
- **Secretarias**: ✎ edita URL/profundidade/filtro/configuração · *desativar* tira da rodada diária · *excluir* remove a fonte e o histórico (os arquivos baixados **não** são apagados).

## Onde ficam os arquivos
`downloads/<sigla>/<ano>/…` no computador (e cópia no MinIO, console em http://localhost:9001).

## Agendar raspagem diária
Na tabela **Secretarias**, coluna **⏰ Agenda diária**: marque a caixa da fonte e escolha o horário (Brasília).
- Roda todo dia nesse horário; só vem o que for novo.
- Se o computador/Docker estiver desligado na hora, roda assim que voltar.
- Desmarque a caixa para parar. Fonte desativada também não roda.
- Vale para portais, fontes de coleta e para o **IOEPA (Diário Oficial)** — nele, todo dia baixa só as edições novas do ano corrente.
- O Docker precisa estar ligado para a agenda funcionar.
