@echo off
chcp 65001 >nul
echo ================================================
echo   Installation - Agenda et Bons d'Intervention
echo ================================================
echo.

python3 --version >nul 2>&1
if errorlevel 1 (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo [ERREUR] Python n'est pas installe ou pas dans le PATH.
        echo Telechargez Python sur https://www.python.org/downloads/
        echo Cochez "Add Python to PATH" lors de l'installation.
        pause
        exit /b 1
    )
    set PYTHON=python
) else (
    set PYTHON=python3
)

echo [1/3] Creation de l'environnement virtuel...
%PYTHON% -m venv venv
if errorlevel 1 (
    echo [ERREUR] Impossible de creer l'environnement virtuel.
    pause
    exit /b 1
)

echo [2/3] Activation et installation des dependances...
call venv\Scripts\activate.bat
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERREUR] Echec de l'installation des dependances.
    pause
    exit /b 1
)

echo [3/3] Initialisation de la base de donnees...
%PYTHON% -c "from app import init_db; init_db()"

echo.
echo ================================================
echo   Installation terminee avec succes !
echo.
echo   Compte administrateur : admin / Admin123!
echo   CHANGEZ ce mot de passe apres la 1ere connexion
echo   dans Parametres.
echo.
echo   Pour demarrer : double-cliquez sur run.bat
echo ================================================
pause
