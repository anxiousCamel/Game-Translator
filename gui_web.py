#!/usr/bin/env python3
"""
Game Translator — Interface web local (abre no navegador automaticamente).
"""
from __future__ import annotations

import subprocess
import threading
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from translators.detector import detect_game_type, GAME_TYPE_LABELS
from translators.twine import TwineTranslator
from translators.renpy import RenpyTranslator
from translators.rpgmaker import RPGMakerTranslator

app = Flask(__name__)

TRANSLATORS = {
    "twine": TwineTranslator,
    "renpy": RenpyTranslator,
    "rpgmaker": RPGMakerTranslator,
}

# ---------------------------------------------------------------------------
# Estado compartilhado (thread-safe)
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {
    "running": False,
    "logs": [],        # lista de [msg, css_class]
    "progress": 0.0,
    "status_label": "Aguardando...",
    "error": False,
}
_lock = threading.Lock()


def _log(msg: str, cls: str = "") -> None:
    with _lock:
        _state["logs"].append([msg, cls])


def _set_progress(value: float, label: str = None) -> None:
    with _lock:
        _state["progress"] = value
        if label:
            _state["status_label"] = label


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def status():
    since = int(request.args.get("since", 0))
    with _lock:
        return jsonify(
            {
                "running": _state["running"],
                "logs": _state["logs"][since:],
                "log_count": len(_state["logs"]),
                "progress": _state["progress"],
                "status_label": _state["status_label"],
                "error": _state["error"],
            }
        )


@app.route("/browse", methods=["POST"])
def browse():
    kind = (request.json or {}).get("kind", "folder")
    path = _native_dialog(kind)
    return jsonify({"path": path})


@app.route("/detect", methods=["POST"])
def detect():
    path_str = (request.json or {}).get("path", "")
    p = Path(path_str)
    t = detect_game_type(p) if p.exists() else None
    return jsonify({"type": t, "label": GAME_TYPE_LABELS.get(t)})


@app.route("/translate", methods=["POST"])
def translate():
    with _lock:
        if _state["running"]:
            return jsonify({"error": "Traducao ja em andamento"}), 400

    data = request.json or {}
    threading.Thread(target=_run_translation, args=(data,), daemon=True).start()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Logica de traducao (roda em thread)
# ---------------------------------------------------------------------------

def _run_translation(data: dict) -> None:
    with _lock:
        _state["running"] = True
        _state["logs"] = []
        _state["progress"] = 0.0
        _state["error"] = False
        _state["status_label"] = "Iniciando..."

    path = Path(data.get("path", ""))
    game_type = data.get("game_type") or detect_game_type(path)
    src_lang = data.get("src_lang", "en")
    tgt_lang = data.get("tgt_lang", "pt")

    try:
        if not path.exists():
            raise FileNotFoundError(f"Caminho nao encontrado: {path}")

        if not game_type:
            raise ValueError(
                "Nao foi possivel detectar o tipo de jogo.\n"
                "Selecione manualmente no menu 'Tipo de jogo'."
            )

        if src_lang == tgt_lang:
            raise ValueError("Idioma de origem e destino sao iguais.")

        translator_cls = TRANSLATORS.get(game_type)
        if not translator_cls:
            raise ValueError(f"Tipo de jogo nao suportado: {game_type}")

        _log(f"Tipo: {GAME_TYPE_LABELS.get(game_type, game_type)}", "info")
        _log(f"Idiomas: {src_lang} -> {tgt_lang}", "info")
        _log("")

        translator = translator_cls(
            path=path,
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            log_fn=lambda msg: _log(msg),
            progress_fn=_set_progress,
        )
        output = translator.translate()

        _log("")
        _log(f"Concluido! Saida:", "ok")
        _log(f"  {output}", "ok")
        _set_progress(1.0, "Concluido!")

    except Exception as exc:
        _log(f"Erro: {exc}", "err")
        _set_progress(_state["progress"], "Erro!")
        with _lock:
            _state["error"] = True
    finally:
        with _lock:
            _state["running"] = False


# ---------------------------------------------------------------------------
# Dialog nativo
# ---------------------------------------------------------------------------

def _native_dialog(kind: str) -> str | None:
    home = str(Path.home())

    if kind == "folder":
        cmds = [
            ["kdialog", "--getexistingdirectory", home],
            ["zenity", "--file-selection", "--directory", f"--filename={home}/"],
        ]
    else:
        cmds = [
            ["kdialog", "--getopenfilename", home, "Jogos Twine (*.html *.htm)"],
            ["zenity", "--file-selection", f"--filename={home}/",
             "--file-filter=HTML *.html *.htm", "--file-filter=Todos *"],
        ]

    for cmd in cmds:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                path = result.stdout.strip()
                if path:
                    return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------

def main():
    port = 7321
    url = f"http://localhost:{port}"
    print(f"\n  Game Translator rodando em {url}")
    print("  Pressione Ctrl+C para fechar.\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
