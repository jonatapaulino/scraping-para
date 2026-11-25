# Fluxo de uma mudança no código (CI/CD)

Vale para qualquer alteração em `app/`, `tests/`, `docker-compose.yml` etc. Mudanças **só no painel** (cadastrar fonte, filtro, coleta) **não precisam disso**: são dados, não código.

```
branch → commits no padrão → PR (CI testa) → merge na main
       → CI testa de novo → versão nova (tag + Release) → imagem publicada
       → no servidor: ./deploy/atualizar.sh
```

## 1. Branch e commits no padrão (Conventional Commits)
Nunca commitar direto na `main`. **A versão do sistema sai das mensagens de commit**, então o prefixo importa:

| Prefixo | Quando | Versão |
|---|---|---|
| `fix:` | corrige um erro | 1.4.**2** → 1.4.**3** |
| `feat:` | novidade para quem usa | 1.**4**.2 → 1.**5**.0 |
| `feat!:` ou corpo com `BREAKING CHANGE:` | quebra algo que já existia (ex.: muda o formato dos arquivos) | **1**.4.2 → **2**.0.0 |
| `perf:` / `refactor:` | desempenho / melhoria interna | patch |
| `docs:` `test:` `ci:` `build:` `chore:` | documentação, testes, pipeline, dependências, faxina | não gera versão (exceto `build(deps):`, que gera patch) |

Escopo opcional, entre parênteses, e o texto em português:
```bash
git checkout -b corrige-periodo
git commit -m "fix(fila): mantém o período escolhido ao trocar de dataset"
git commit -m "feat(painel): card com o espaço livre no servidor"
git push -u origin corrige-periodo
```
> Erro `dubious ownership`? Rode uma vez: `git config --global --add safe.directory C:/scraping-para-main`

## 2. Pull request: o CI confere sozinho
```bash
gh pr create --base main --title "fix(fila): mantém o período ao trocar de dataset" --body "O que muda / como testei"
```
No PR, o GitHub Actions roda (aba *Checks*):
- **Qualidade:** ruff (erros reais de Python), sintaxe do JS, `docker-compose.yml` válido, `.bat` com CRLF;
- **Mensagens de commit:** todas no padrão da tabela acima;
- **Testes:** todas as suítes de `tests/`, cada uma com banco e pasta próprios, num Postgres de verdade;
- **Imagem Docker:** monta a imagem (sem publicar) para garantir que o build funciona.

Só faça o merge com tudo **verde**. Para rodar os testes no seu computador (com Docker):
```bash
docker run -d --rm --name pg-teste -e POSTGRES_USER=scraper -e POSTGRES_PASSWORD=scraper -p 5432:5432 postgres:16-alpine
python tests/rodar.py          # ou: python tests/rodar.py fila
```

## 3. Merge: versão e Release automáticas
```bash
gh pr merge --merge --delete-branch
git checkout main && git pull
```
Na `main`, o CI testa de novo e o **semantic-release**:
1. calcula a versão a partir dos commits desde a última tag;
2. cria a tag `vX.Y.Z` e a **Release** no GitHub, com as notas em português (Novidades, Correções…);
3. publica a imagem `ghcr.io/jonatapaulino/scraping-para` com as tags `X.Y.Z`, `X.Y`, `X` e `latest`, mais o atestado de procedência e a varredura de vulnerabilidades.

Se nenhum commit for `feat`/`fix`/`perf`/`refactor`/`build(deps)`, não sai versão nova. Isso é normal.

## 4. Atualizar o servidor
Quem administra o servidor roda, na pasta do projeto:
```bash
./deploy/atualizar.sh          # última versão
./deploy/atualizar.sh 1.5.0    # versão específica (também serve para voltar atrás)
```
O script baixa a imagem da versão, reinicia os serviços, confere o `/health` e, **se não ficar saudável em ~2 min, volta sozinho para a versão anterior**. A versão em uso aparece no topo do painel.

## 5. Se mexeu em banco ou configuração
- **Coluna/tabela nova:** adicione em `app/models.py` **e** em `_ADDITIVE_COLUMNS` de `app/database.py` (bancos já existentes não mudam sozinhos).
- **Variável nova no `.env`:** documente no `README.md` e em `.env.example`.
- **Teste novo:** crie `tests/teste_<assunto>.py` no mesmo formato dos outros (`check(condição, "mensagem")` e sair com código 1 se falhar). O `tests/rodar.py` acha sozinho.

## Checklist rápido
- [ ] Branch + commits com prefixo (`fix:`, `feat:`…)
- [ ] PR com todos os checks verdes
- [ ] Merge → conferir a Release nova em *Releases*
- [ ] Servidor: `./deploy/atualizar.sh` (ou pedir a quem administra)
