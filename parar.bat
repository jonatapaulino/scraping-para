@echo off
cd /d %~dp0
echo Parando o Coletor de Dados Publicos (dados sao mantidos)...
docker compose down
echo.
echo Sistema parado. Para voltar: duplo clique em iniciar.bat
pause
