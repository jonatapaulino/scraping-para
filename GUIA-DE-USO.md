# Guia de uso

Tudo é feito pelo painel: **http://localhost:8000/ui** (abre só neste computador — veja *Abrir em outro aparelho* no fim)

## Abrir o painel (o sistema roda no servidor)
O sistema roda no **servidor Xavier**, 24 horas por dia, mesmo com o notebook desligado. No notebook você só abre o painel:
1. Na pasta `C:\scraping-para-main`, dê dois cliques em **`abrir-painel-servidor.bat`**.
2. Abre uma janela preta: **deixe-a aberta**. Em alguns segundos o navegador abre:
   - **Painel**: http://localhost:8000/ui
   - **Arquivos guardados** no servidor (só leitura: tamanho das pastas, espaço livre, abrir/baixar): http://localhost:8001
3. Para sair, feche a janela preta. As coletas continuam no servidor.

Se uma aba mostrar "recusou a se conectar", espere uns segundos e aperte F5. **Não use mais o `iniciar.bat`**: ele liga o sistema antigo no notebook. Mudanças no código do servidor: peça ao administrador um `git pull` e o rebuild.

## Ligar e desligar (sistema local, antigo)
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

### Fila automática (várias coletas, uma de cada vez, sem você)
Em vez de **Coletar**, clique em **➕ Pôr na fila (uma por vez)**. A coleta entra numa lista que o sistema roda **sozinho, uma de cada vez, na ordem**: quando uma termina, a próxima começa (confere a cada minuto, de dia ou de noite).
- **Tudo de 2019 a 2025:** dataset *— todos os datasets —*, de `2019-01` até `2025-12`, *todos os órgãos* → **Pôr na fila**. Vira um item por dataset (14 itens).
- O Diário Oficial não entra em "todos os datasets": escolha *Diário Oficial (IOEPA)* e ponha na fila separado, se quiser.
- A lista aparece logo abaixo: **Na fila → Rodando → Concluída/Falhou**. A que está rodando mostra a barra de progresso no topo.
- **×** tira da fila o que ainda não começou. Para parar a que está rodando, use **cancelar** no progresso — a fila segue para a próxima.
- Uma coleta que falhou **não trava** a fila. Para tentar de novo o que falhou, ponha o mesmo dataset na fila outra vez (sem *recoletar*): o que já deu certo é pulado e só o que falhou é refeito.
- O computador e o Docker precisam ficar ligados. Se desligar no meio, ao voltar a fila continua de onde parou.

Órgão que não publicou nada no período aparece como **Sem dados** (cinza) — não é erro e não entra na contagem de *Falhas*.

## Enviar os arquivos para o Google Drive
Seção **☁ Enviar para o Google Drive**, no painel:
1. **Uma vez:** abra *Conectar conta Google* e siga os 4 passos (baixar o rclone no computador, rodar `.clone.exe authorize "drive"`, entrar com a conta, colar o código). O espaço usado é o **da conta conectada**.
2. Cole o **link da pasta** do Drive (pode ser uma pasta diferente a cada envio), escolha *todos os arquivos* ou *só a pasta* de um órgão e clique em **☁ Enviar para o Drive**.
3. O andamento aparece em **Coleta agora**. Roda no servidor, então dá para fechar o painel. Repetir o envio manda **só o que ainda não está lá**.

Se aparecer "a autorização do Google expirou", conecte a conta de novo (passo 1).

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
| Falhas na lista | card **Falhas → ver e resolver**: mostra o motivo de cada uma. **↻ Tentar de novo o que dá** refaz o que pode dar certo (vira uma tarefa com barra em "Coleta agora"); **🗑 Descartar links quebrados** tira da lista o que não tem jeito. Itens **Sem dados** não são falha: o órgão não publicou nada |
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
