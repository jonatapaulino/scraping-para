# Guia de uso

Tudo é feito pelo painel: **http://localhost:8000/ui** (abre só neste computador — veja *Abrir em outro aparelho* no fim)

## Ligar e desligar
1. Abra o **Docker Desktop** (precisa estar aberto).
2. Duplo clique em `iniciar.bat` — o painel abre sozinho.
3. Para desligar: `parar.bat` (os dados ficam guardados).

> Se o computador reiniciar, o sistema volta sozinho assim que o Docker Desktop abrir (a não ser que você tenha usado `parar.bat`). Para as agendas diárias funcionarem, o computador e o Docker precisam estar ligados.

## Qual caminho usar?
| O site... | Use |
|---|---|
| tem links de **PDFs** | **1. Secretarias** |
| é uma **API (JSON)** ou uma **tabela** na página | **2. Fonte de dados** |
| é o **Portal da Transparência** (folha, despesas, diárias…) | **3. Dados abertos** |

## 1. Site com lista de PDFs
Seção **Secretarias** (formulário embaixo da tabela):

| Campo | O que colocar |
|---|---|
| SIGLA | nome curto da fonte (vira a pasta em `downloads/`) |
| URL | a página onde estão os links dos PDFs |
| profundidade | quantos cliques o robô anda a partir da URL até achar os PDFs. `0` = só aquela página, `1` = ela e as páginas que ela linka, `2` = mais um nível (quanto maior, mais lento) |
| PDFs | **vazio** = só financeiros (edital, contrato…) · `*` = **todos** · ou uma regex, ex.: `remunera\|inativ` |

Marque **raspar agora** e clique em **+ Adicionar**.

> **Terminou com 0 PDFs?** Quase sempre é o filtro: coloque `*` no campo *PDFs* (✎ na tabela) e rode de novo.

## 2. Site com API (JSON) ou tabela na página
Seção **⚙ Fonte de dados — qualquer site**:
1. Escolha *API / JSON* ou *Página com tabela*.
2. Cole a URL. Se tiver várias páginas, use `{page}`: `https://site/api?pagina={page}` e defina *páginas*.
3. Clique em **🔍 Testar** — mostra as colunas e uma amostra, sem gravar nada.
4. **Salvar fonte** (com *coletar agora* se quiser rodar já). Sai um CSV.

Se a tabela só aparece com JavaScript, marque *página com JavaScript*. Se o site exige token, use *avançado → cabeçalhos* (`Authorization: Bearer …`).
**Não funciona** com login, formulário com campos ou captcha.

## 3. Dados abertos da Transparência
Seção **Dados abertos**: escolha o dataset (folha, despesas, diárias…), o período e os órgãos (ou *todos*) → **Coletar**. Gera um CSV por mês/órgão. Esta seção **não tem agenda automática**: é sempre manual.

Órgão que não publicou nada no período aparece como **Sem dados** (cinza) — não é erro e não entra na contagem de *Falhas*.

## A tabela Secretarias
| Coluna | O que é |
|---|---|
| **Tipo** | **Portal** (baixa PDFs) · **Coleta** (API/tabela → CSV) · **Diário** (IOEPA) · **API** (Transparência) |
| **Profundidade** / **PDFs** | só valem para Portal; ✎ edita |
| **⏰ Agenda diária** | caixa + horário (veja abaixo) |
| **Situação** | **Ativa** ou **Inativa** (fonte inativa não roda nem na agenda) |
| **Ações** | *desativar/reativar* · *excluir* remove a fonte e o histórico (os arquivos baixados **não** são apagados). Fontes de Coleta: ✎ config abre o formulário para editar |

## Agendar raspagem diária
Marque a caixa da fonte e escolha o horário (Brasília). **Nada roda sozinho sem essa caixa.**
- Roda todo dia nesse horário; só vem o que for novo.
- Se o computador/Docker estiver desligado na hora, roda assim que voltar (uma vez por dia).
- Se você marcar depois do horário de hoje, começa só amanhã.
- Desmarque para parar. Vale para Portal, Coleta e **IOEPA** (todo dia baixa só as edições novas do ano corrente).

## Acompanhar e controlar
- **Progresso das tarefas** (topo): barra de cada tarefa. **cancelar** interrompe (o que já baixou fica); **×** tira da lista.
- **Tarefas recentes**: **⬇ zip** baixa tudo da tarefa.
- **Documentos**: cada arquivo tem ⬇ para baixar.
- **Fila de erros**: botão *reprocessar* tenta de novo.

## Onde ficam os arquivos
`downloads/<sigla>/<ano>/…` no computador (e cópia no MinIO, console em http://localhost:9001). A pasta `downloads/` não vai para o GitHub: **faça backup dela** se os arquivos forem importantes.

## Problemas comuns
| Sintoma | O que fazer |
|---|---|
| Painel não abre | Docker Desktop aberto? Rode `iniciar.bat` de novo |
| Tarefa com 0 PDFs | filtro de PDFs → `*` (veja seção 1) |
| **Testar** deu erro | leia a mensagem: costuma ser URL errada, "não é JSON" (use *tabela*) ou caminho inexistente |
| Tarefa travada | **cancelar** e rodar de novo |
| Falhas na lista | *Fila de erros → reprocessar* (itens **Sem dados** não são falha: o órgão não publicou nada) |
| Coleta: "Endereço interno bloqueado" | a URL aponta para o próprio computador/sistema — por segurança isso não é permitido |
| Mudou o site e parou de achar PDFs | aumente a **profundidade** ou ajuste o **filtro** |

## Quando ainda precisa de ajuda técnica
Só nestes casos (não dá para resolver pelo painel):
- site com **login, formulário ou captcha**;
- API que exige um fluxo próprio (vários passos) — vira código novo;
- ajustes globais no `.env` (proxy, Google Drive, limites) — exige reiniciar;
- **mudar o código** do sistema — siga o `FLUXO-DE-MUDANCA.md`.

## Abrir em outro aparelho (celular, outro PC)
Por segurança o painel só abre neste computador. Para liberar na rede de casa/escritório: no `.env`, troque `PAINEL_BIND=127.0.0.1` por `PAINEL_BIND=0.0.0.0` e rode `iniciar.bat` de novo. O painel **não tem senha** — não faça isso em rede pública (café, hotel). O banco e o armazenamento continuam fechados.
