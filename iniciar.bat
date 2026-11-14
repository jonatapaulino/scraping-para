@echo off
cd /d %~dp0
if not exist .env (
  echo .env nao encontrado - copiando .env.example...
  copy .env.example .env >nul
)
echo ==========================================
echo   Scraper Gov-PA - subindo o sistema
echo ==========================================
docker compose up -d
if errorlevel 1 (
  echo.
  echo [ERRO] Falha ao subir. O Docker Desktop esta aberto?
  pause
  exit /b 1
)
echo.
echo Sistema no ar! Painel: http://localhost:8000/ui
echo (os PDFs baixados caem em downloads\)
echo.
start http://localhost:8000/ui
pause
