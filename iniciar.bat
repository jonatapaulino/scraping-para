@echo off
cd /d %~dp0
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
