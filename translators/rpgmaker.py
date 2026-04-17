from __future__ import annotations
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .base import BaseTranslator
from .engine import ensure_model, translate_texts

# Codigos de comando com texto traduzivel
_TEXT_CODES = {401, 405}   # Show Text / Scroll Text
_CHOICE_CODE = 102          # Show Choices

# Codigos que nao devem ser tocados (scripts, plugin calls, etc.)
_SKIP_CODES = {
    355, 655,   # Script / Script (cont.)
    357,        # Plugin Command (MZ)
    356,        # Plugin Command (MV)
    111,        # Conditional Branch (pode ter scripts)
    108, 408,   # Comment
}

# Campos de texto em objetos de dados
# 'name' é excluido aqui pois pode ser referenciado por scripts/plugins —
# apenas campos de exibição pura são traduzidos
_DISPLAY_FIELDS = {"description", "displayName", "message1", "message2", "message3", "message4"}
# Nomes de atores/classes/tropas sao seguros (nao sao usados como chaves de script)
_SAFE_NAME_FILES = {"Actors", "Classes", "Troops", "MapInfos"}

# Codigos de controle do RPGMaker: \n[1], \c[3], \v[1], etc.
_CTRL_RE = re.compile(r"\\[a-zA-Z]\[\d+\]|\\[!>.<^{}\\|]|\\n")


class RPGMakerTranslator(BaseTranslator):
    def translate(self) -> Path:
        data_dir = self._find_data_dir()
        ensure_model(self.src_lang, self.tgt_lang, self.log)

        # Cria backup da pasta original (uma unica vez)
        backup_dir = data_dir.parent / f"{data_dir.name}-original"
        if not backup_dir.exists():
            shutil.copytree(data_dir, backup_dir)
            self.log(f"Backup criado em: {backup_dir.name}/")

        # Sempre traduz a partir do backup (original), evitando retraduzir texto ja traduzido
        source_dir = backup_dir
        json_files = sorted(source_dir.glob("*.json"))
        self.log(f"{len(json_files)} arquivos JSON em '{source_dir.name}/' (original).")

        cache_path = self.path / "traducoes_rpgmaker.json"
        cache = _load_cache(cache_path, self.log)

        all_texts = _collect_texts_from_files(json_files)
        to_do = [t for t in all_texts if not cache.get(t)]
        self.log(f"{len(all_texts)} textos unicos | {len(to_do)} para traduzir.")

        total = len(to_do)
        batch_size = 20
        for i in range(0, total, batch_size):
            batch = to_do[i : i + batch_size]
            translated = translate_texts(batch, self.src_lang, self.tgt_lang)
            for orig, trad in zip(batch, translated):
                cache[orig] = trad
            _save_cache(cache_path, cache)
            done = min(i + batch_size, total)
            self.set_progress(done / max(total, 1) * 0.95, f"Traduzindo... {done}/{total}")
            self.log(f"  {done}/{total} textos traduzidos")

        # Aplica traducoes diretamente em data_dir (in-place)
        self.log(f"Aplicando traducoes em: {data_dir.name}/")
        for jf in json_files:
            stem = jf.stem  # ex: "Actors", "Items"
            translate_names = stem in _SAFE_NAME_FILES
            out = data_dir / jf.name
            try:
                data = json.loads(jf.read_text(encoding="utf-8", errors="replace"))
                translated_data = _apply_to_json(data, cache, translate_names)
                out.write_text(
                    json.dumps(translated_data, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
            except Exception as e:
                self.log(f"  Aviso: {jf.name} mantido original ({e})")
                shutil.copy2(jf, out)

        self.set_progress(1.0, "Concluido!")
        self.log("Jogo pronto. Inicie normalmente.")
        return data_dir

    def _find_data_dir(self) -> Path:
        for candidate in [
            self.path / "www" / "data",
            self.path / "data",
        ]:
            if candidate.is_dir() and (candidate / "System.json").exists():
                return candidate
        raise FileNotFoundError(f"Pasta 'data/' do RPGMaker nao encontrada em: {self.path}")


# ---------------------------------------------------------------------------
# Coleta de textos
# ---------------------------------------------------------------------------

def _collect_texts_from_files(files: list[Path]) -> list[str]:
    seen: set[str] = set()
    result = []

    def add(text: str):
        text = text.strip()
        if text and len(text) > 2 and text not in seen:
            seen.add(text)
            result.append(text)

    for jf in files:
        try:
            stem = jf.stem
            translate_names = stem in _SAFE_NAME_FILES
            data = json.loads(jf.read_text(encoding="utf-8", errors="replace"))
            _walk_collect(data, add, translate_names)
        except Exception:
            continue
    return result


def _walk_collect(obj: Any, add, translate_names: bool = False):
    if isinstance(obj, list):
        for item in obj:
            _walk_collect(item, add, translate_names)
    elif isinstance(obj, dict):
        if "code" in obj and "parameters" in obj:
            _collect_command(obj, add)
            return
        for key, val in obj.items():
            if isinstance(val, str):
                if key in _DISPLAY_FIELDS:
                    clean = _strip_ctrl(val)
                    if clean:
                        add(clean)
                elif key == "name" and translate_names:
                    clean = _strip_ctrl(val)
                    if clean:
                        add(clean)
            elif isinstance(val, (dict, list)) and key not in {"id", "note", "meta"}:
                _walk_collect(val, add, translate_names)


def _collect_command(cmd: dict, add):
    code = cmd.get("code")
    if code in _SKIP_CODES:
        return
    params = cmd.get("parameters", [])
    if code in _TEXT_CODES and params and isinstance(params[0], str):
        clean = _strip_ctrl(params[0])
        if clean:
            add(clean)
    elif code == _CHOICE_CODE and params and isinstance(params[0], list):
        for choice in params[0]:
            if isinstance(choice, str):
                clean = _strip_ctrl(choice)
                if clean:
                    add(clean)


# ---------------------------------------------------------------------------
# Aplicacao de traducoes
# ---------------------------------------------------------------------------

def _apply_to_json(obj: Any, cache: dict, translate_names: bool = False) -> Any:
    if isinstance(obj, list):
        return [_apply_to_json(item, cache, translate_names) for item in obj]
    elif isinstance(obj, dict):
        if "code" in obj and "parameters" in obj:
            return _apply_command(obj, cache)
        result = {}
        for key, val in obj.items():
            if isinstance(val, str):
                if key in _DISPLAY_FIELDS:
                    result[key] = _translate_with_ctrl(val, cache)
                elif key == "name" and translate_names:
                    result[key] = _translate_with_ctrl(val, cache)
                else:
                    result[key] = val
            elif isinstance(val, (dict, list)) and key not in {"id", "note", "meta"}:
                result[key] = _apply_to_json(val, cache, translate_names)
            else:
                result[key] = val
        return result
    return obj


def _apply_command(cmd: dict, cache: dict) -> dict:
    code = cmd.get("code")
    if code in _SKIP_CODES:
        return cmd
    params = list(cmd.get("parameters", []))
    if code in _TEXT_CODES and params and isinstance(params[0], str):
        params[0] = _translate_with_ctrl(params[0], cache)
    elif code == _CHOICE_CODE and params and isinstance(params[0], list):
        params[0] = [
            _translate_with_ctrl(c, cache) if isinstance(c, str) else c
            for c in params[0]
        ]
    return {**cmd, "parameters": params}


# ---------------------------------------------------------------------------
# Utilitarios
# ---------------------------------------------------------------------------

def _strip_ctrl(text: str) -> str:
    return _CTRL_RE.sub("", text).strip()


def _translate_with_ctrl(text: str, cache: dict) -> str:
    clean = _strip_ctrl(text)
    if not clean or not cache.get(clean):
        return text
    return text.replace(clean, cache[clean], 1)


def _load_cache(path: Path, log) -> dict:
    if not path.exists():
        return {}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        log(f"Cache carregado: {sum(1 for v in cache.values() if v)} traducoes.")
        return cache
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict):
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
