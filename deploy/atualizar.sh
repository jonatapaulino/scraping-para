#!/usr/bin/env bash
# Atualiza o Coletor de Dados Publicos para uma versao publicada (Releases do GitHub).
#
#   ./deploy/atualizar.sh            # ultima versao publicada
#   ./deploy/atualizar.sh 1.4.2      # versao especifica (tambem serve para voltar atras)
#
# Nao abre nada para fora: so BAIXA a imagem do ghcr.io e o compose da tag.
# Se o sistema nao ficar saudavel em ~2 min, volta sozinho para a versao anterior.
set -euo pipefail

REPO="jonatapaulino/scraping-para"
cd "$(dirname "$0")/.."

log() { printf '\033[1m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
erro() { printf '\033[31m[ERRO]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f .env ] || erro "falta o .env (copie de .env.example e ajuste)"
command -v docker >/dev/null || erro "docker nao encontrado"

ALVO="${1:-}"
if [ -z "$ALVO" ]; then
  ALVO=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["tag_name"].lstrip("v"))') \
    || erro "nao consegui consultar a ultima versao no GitHub"
fi
ALVO="${ALVO#v}"
[[ "$ALVO" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || erro "versao invalida: $ALVO (use X.Y.Z)"

ATUAL=$(grep -E '^SCRAPER_VERSAO=' .env | tail -1 | cut -d= -f2- || true)
log "versao atual: ${ATUAL:-(nao definida)}  ->  nova: $ALVO"
[ "$ATUAL" = "$ALVO" ] && { log "ja esta nessa versao — nada a fazer"; exit 0; }

define_versao() {
  if grep -qE '^SCRAPER_VERSAO=' .env; then
    sed -i "s/^SCRAPER_VERSAO=.*/SCRAPER_VERSAO=$1/" .env
  else
    printf '\n# versao do sistema (deploy/atualizar.sh)\nSCRAPER_VERSAO=%s\n' "$1" >> .env
  fi
}

aplica() {  # $1 = versao
  git fetch --quiet --tags origin
  git -c advice.detachedHead=false checkout --quiet "v$1"   # compose e scripts DAQUELA versao
  define_versao "$1"
  docker compose pull --quiet
  docker compose up -d --no-build --remove-orphans
}

saudavel() {  # espera o /health da api responder ok com a versao esperada
  for _ in $(seq 1 24); do
    if docker compose exec -T api python -c "import json,sys,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)); sys.exit(0 if d.get('status')=='ok' and d.get('versao')=='$1' else 1)" 2>/dev/null; then
      return 0
    fi
    sleep 5
  done
  return 1
}

log "baixando e aplicando v$ALVO ..."
aplica "$ALVO"
if saudavel "$ALVO"; then
  log "pronto: rodando v$ALVO"
  docker compose ps --format 'table {{.Service}}\t{{.Status}}'
  exit 0
fi

log "v$ALVO nao ficou saudavel em 2 minutos"
docker compose logs --tail 40 api || true
if [ -n "$ATUAL" ]; then
  log "voltando para v$ATUAL ..."
  aplica "$ATUAL"
  saudavel "$ATUAL" && log "de volta em v$ATUAL" || log "ATENCAO: v$ATUAL tambem nao respondeu — veja os logs"
fi
exit 1
