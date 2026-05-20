@echo off
chcp 65001 >nul
echo ================================================
echo   Agenda et Bons d'Intervention
echo ================================================
echo.

if not exist venv (
    echo [ERREUR] Environnement virtuel introuvable.
    echo Executez d'abord install.bat
    pause
    exit /b 1
)

echo   Demarrage du serveur...
echo   Acces : http://localhost:5000
echo   Appuyez sur Ctrl+C pour arreter.
echo.

start "" http://localhost:5000
venv\Scripts\python.exe app.py
pause
