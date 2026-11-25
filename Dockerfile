FROM python:3.12-slim

# Versao gravada pelo CI (semantic-release) — aparece no painel e no /health
ARG APP_VERSION=dev
ARG VCS_REF=desconhecido
LABEL org.opencontainers.image.title="scraper-gov-pa"       org.opencontainers.image.description="Coleta de dados publicos do Para (Transparencia, Diario Oficial, PNCP, portais)"       org.opencontainers.image.source="https://github.com/jonatapaulino/scraping-para"       org.opencontainers.image.licenses="NOASSERTION"       org.opencontainers.image.version="${APP_VERSION}"       org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1     APP_VERSION=${APP_VERSION}

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.main"]
