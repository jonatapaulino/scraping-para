FROM python:3.12-slim

# Versao gravada pelo CI (semantic-release) — aparece no painel e no /health
ARG APP_VERSION=dev
ARG VCS_REF=desconhecido
LABEL org.opencontainers.image.title="coletor-dados-publicos"       org.opencontainers.image.description="Coleta independente (nao oficial) de dados publicos do Para (Transparencia, Diario Oficial, PNCP, portais)"       org.opencontainers.image.source="https://github.com/jonatapaulino/scraping-para"       org.opencontainers.image.licenses="NOASSERTION"       org.opencontainers.image.version="${APP_VERSION}"       org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1     PYTHONDONTWRITEBYTECODE=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1     APP_VERSION=${APP_VERSION}

WORKDIR /srv

# rclone (envio ao Google Drive pelo painel): versao fixa, arquivo conferido
# contra o SHA256SUMS oficial (releases do rclone no GitHub) antes de instalar
ARG RCLONE_VERSION=1.68.2
RUN python -c "import hashlib,io,os,urllib.request,zipfile; v=os.environ['RCLONE_VERSION']; base=f'https://github.com/rclone/rclone/releases/download/v{v}/'; nome=f'rclone-v{v}-linux-amd64'; dados=urllib.request.urlopen(base+nome+'.zip', timeout=180).read(); somas=urllib.request.urlopen(base+'SHA256SUMS', timeout=60).read().decode(); assert hashlib.sha256(dados).hexdigest()+'  '+nome+'.zip' in somas, 'checksum do rclone nao confere'; open('/usr/local/bin/rclone','wb').write(zipfile.ZipFile(io.BytesIO(dados)).read(nome+'/rclone')); os.chmod('/usr/local/bin/rclone', 0o755)"  && rclone version | head -1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.main"]
