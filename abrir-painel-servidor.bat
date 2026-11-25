@echo off
title Painel do Scraper (servidor Xavier)
echo ==========================================================
echo   Painel do Scraper - rodando no SERVIDOR (Xavier)
echo ==========================================================
echo.
echo  O navegador abre sozinho em alguns segundos:
echo     Painel ............ http://localhost:8000/ui
echo     Arquivos guardados  http://localhost:8001
echo.
echo  DEIXE ESTA JANELA ABERTA enquanto usa o painel.
echo  Fechar a janela so fecha o acesso: as coletas continuam
echo  rodando no servidor, mesmo com o notebook desligado.
echo.
echo  (Se o navegador pedir login da Cloudflare, entre com o seu e-mail.)
echo.
start "" cmd /c "timeout /t 7 >nul & start http://localhost:8000/ui & start http://localhost:8001"
C:\Windows\System32\OpenSSH\ssh.exe -tt -L 8000:api:8000 -L 8001:localhost:8001 -L 5555:flower:5555 -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes xavier-jonata "python3 ~/scraper-explorar.py ~/scraper-downloads 8001"
echo.
echo [ERRO] A conexao com o servidor caiu ou nao abriu.
echo  - Confira a internet e tente de novo.
echo  - Se aparecer "Address already in use": o sistema antigo deste
echo    computador esta ligado. NAO use mais o iniciar.bat.
pause
