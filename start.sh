#!/bin/bash

echo "========================================"
echo " Game Translator - Inicializacao"
echo "========================================"
echo

if ! command -v python3 &>/dev/null; then
    echo "ERRO: Python3 nao encontrado!"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "[1/3] Criando ambiente virtual..."
    python3 -m venv venv || { echo "ERRO ao criar venv!"; exit 1; }
fi

echo "[2/3] Instalando dependencias (pode demorar alguns minutos na primeira vez)..."
source venv/bin/activate

echo "  Atualizando pip..."
pip install --upgrade pip

echo "  Instalando pacotes (numpy, argostranslate, flask...)..."
pip install -r requirements.txt --progress-bar on
if [ $? -ne 0 ]; then
    echo "ERRO ao instalar dependencias!"
    deactivate; exit 1
fi
echo "  Dependencias OK."

echo "[3/3] Iniciando interface web (abre no navegador)..."
echo
python gui_web.py

deactivate
