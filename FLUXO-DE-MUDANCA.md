# Fluxo depois de uma mudança no código

Use sempre que alterar algo em `app/` (Python, HTML, JS, CSS). Mudanças **só no painel** (cadastrar fonte, filtro, coleta) **não precisam disso** — são dados, não código.

## 1. Aplicar no sistema (rebuild)
O código vai dentro da imagem do Docker, então só vale depois do rebuild:

```bash
docker compose up -d --build api worker-discovery worker-download beat
```
- Antes, confira no painel se **não há tarefa rodando** (o rebuild interrompe o que está em andamento).
- Só mudou HTML/JS/CSS? Basta `docker compose up -d --build api`.
- Depois, **Ctrl+F5** no painel (limpa o cache do navegador).

## 2. Conferir antes de commitar
```bash
node --check app/static/app.js        # erro de sintaxe no JS quebra o painel inteiro
python -c "import ast; ast.parse(open('app/routers.py', encoding='utf-8').read())"   # idem p/ cada .py alterado
docker compose logs api --tail 30     # sem erro ao subir?
```
Abra o painel e teste a coisa que mudou de verdade (clicar, rodar uma tarefa).

## 3. Branch, commit e push
Nunca commitar direto na `main`.

```bash
git checkout -b nome-curto-da-mudanca
git add <arquivos>                     # evite `git add .` (pode pegar lixo); downloads/ e .env já são ignorados
git commit -m "O que mudou e por quê, numa frase"
git push -u origin nome-curto-da-mudanca
```
> Erro `dubious ownership`? Rode uma vez: `git config --global --add safe.directory C:/scraping-para-main`

## 4. Pull request e merge
```bash
gh pr create --base main --title "Título claro" --body "O que muda / como testei / migração (se houver)"
gh pr view --web                       # confere no navegador
gh pr merge --merge --delete-branch    # quando estiver ok
git checkout main && git pull          # traz a main atualizada para o computador
```
O repositório não tem testes automáticos: o que vale é o teste manual do passo 2. Descreva no PR o que você testou.

## 5. Se mexeu em banco ou configuração
- **Coluna/tabela nova**: adicione em `app/models.py` **e** em `_ADDITIVE_COLUMNS` de `app/database.py` (bancos já existentes não são alterados sozinhos).
- **Variável nova no `.env`**: documente no `README.md` e em `.env.example`.
- Depois do merge em **outro computador**: `git pull` + o rebuild do passo 1.

## Checklist rápido
- [ ] Sem tarefa rodando → rebuild
- [ ] Sintaxe conferida (JS e Python)
- [ ] Testei no painel
- [ ] Branch + commit + PR
- [ ] Merge, `git checkout main && git pull`
