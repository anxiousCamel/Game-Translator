@echo off
echo ========================================
echo  Game Translator - Inicializacao
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado!
    echo Instale Python 3.9+ de: https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist "venv" (
    echo [1/3] Criando ambiente virtual...
    python -m venv venv
    if errorlevel 1 (
        echo ERRO ao criar ambiente virtual!
        pause
        exit /b 1
    )
)

echo [2/3] Instalando dependencias...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERRO ao instalar dependencias!
    pause
    exit /b 1
)

echo [3/3] Iniciando interface grafica...
echo.
python gui.py

call venv\Scripts\deactivate.bat
