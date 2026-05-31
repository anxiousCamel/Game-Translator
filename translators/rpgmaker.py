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

        # --- Backup base data (always translate from original) ---
        backup_dir = data_dir.parent / f"{data_dir.name}-original"
        if not backup_dir.exists():
            shutil.copytree(data_dir, backup_dir)
            self.log(f"Backup criado em: {backup_dir.name}/")
        source_dir = backup_dir

        # --- Detect localization plugin ---
        locale_info = _detect_locale_plugin(self.path, self.src_lang)
        if locale_info:
            lsrc = locale_info["src"]
            rel = lsrc.relative_to(self.path) if lsrc.is_relative_to(self.path) else lsrc
            self.log(f"Plugin de localizacao detectado: {rel}")
            # Backup locale source so re-runs always translate from original
            if locale_info["type"] == "dir":
                lbackup = lsrc.parent / f"{lsrc.name}-original"
                if not lbackup.exists():
                    shutil.copytree(lsrc, lbackup)
                    self.log(f"Backup locale: {lbackup.name}/")
            else:
                lbackup = lsrc.parent / f"{lsrc.stem}-original{lsrc.suffix}"
                if not lbackup.exists():
                    shutil.copy2(lsrc, lbackup)
                    self.log(f"Backup locale: {lbackup.name}")
            locale_info["backup"] = lbackup

        # --- Cache ---
        cache_path = self.path / "traducoes_rpgmaker.json"
        cache = _load_cache(cache_path, self.log)

        # --- Collect texts (base data + locale) ---
        json_files = sorted(source_dir.glob("*.json"))
        all_texts = _collect_texts_from_files(json_files)

        if locale_info:
            extra = _collect_locale_texts(locale_info)
            seen = set(all_texts)
            added = 0
            for t in extra:
                if t not in seen:
                    all_texts.append(t)
                    seen.add(t)
                    added += 1
            self.log(f"{added} textos adicionais nos arquivos de localizacao.")

        to_do = [t for t in all_texts if not cache.get(t)]
        self.log(f"{len(all_texts)} textos unicos | {len(to_do)} para traduzir.")

        # --- Translate ---
        total = len(to_do)
        batch_size = 20
        for i in range(0, total, batch_size):
            batch = to_do[i : i + batch_size]
            translated = translate_texts(batch, self.src_lang, self.tgt_lang)
            for orig, trad in zip(batch, translated):
                cache[orig] = trad
            _save_cache(cache_path, cache)
            done = min(i + batch_size, total)
            self.set_progress(done / max(total, 1) * 0.9, f"Traduzindo... {done}/{total}")
            self.log(f"  {done}/{total} textos traduzidos")

        # --- Apply to base data ---
        self.log(f"Aplicando traducoes em: {data_dir.name}/")
        for jf in json_files:
            stem = jf.stem
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

        # --- Apply to locale files (overwrites KO/EN locale with PT translation) ---
        if locale_info:
            self.log("Aplicando traducoes nos arquivos de localizacao...")
            _apply_locale_translations(locale_info, cache, self.log)

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
# Localization plugin detection
# ---------------------------------------------------------------------------

def _detect_locale_plugin(game_path: Path, src_lang: str) -> dict | None:
    """
    Detect common RPGMaker localization plugin structures.
    Returns dict with 'type', 'src' (live path), optional 'lang_key'.
    Types: "dir" (folder of JSONs), "file" (single JSON), "master" (multi-lang JSON).
    """
    lang_variants = (src_lang, src_lang.upper())

    # Pattern 1 & 2: www/locales/<lang>/ or www/languages/<lang>/ (and game root equivalents)
    for root in (game_path / "www", game_path):
        if not root.is_dir():
            continue
        for folder in ("locales", "languages", "lang", "i18n"):
            base = root / folder
            if not base.is_dir():
                continue
            for lk in lang_variants:
                src_dir = base / lk
                if src_dir.is_dir() and list(src_dir.glob("*.json")):
                    return {"type": "dir", "src": src_dir}
                src_file = base / f"{lk}.json"
                if src_file.is_file():
                    return {"type": "file", "src": src_file}

    # Pattern 3: data/<src_lang>/ subdirectory (data/ko/, data/en/, etc.)
    for data_root in (game_path / "www" / "data", game_path / "data"):
        if not data_root.is_dir():
            continue
        for lk in lang_variants:
            data_lang = data_root / lk
            if data_lang.is_dir() and list(data_lang.glob("*.json")):
                return {"type": "dir", "src": data_lang}

        # Pattern 4: data/Languages/<lang>.json
        for lang_sub in ("Languages", "languages"):
            lang_dir = data_root / lang_sub
            if not lang_dir.is_dir():
                continue
            for lk in lang_variants:
                f = lang_dir / f"{lk}.json"
                if f.is_file():
                    return {"type": "file", "src": f}

        # Pattern 5: master file with top-level language keys
        for master_name in ("Text.json", "Languages.json", "language.json", "texts.json"):
            master = data_root / master_name
            if not master.is_file():
                continue
            try:
                data = json.loads(master.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(data, dict):
                    for lk in lang_variants:
                        if lk in data:
                            return {"type": "master", "src": master, "lang_key": lk}
            except Exception:
                pass

    return None


# ---------------------------------------------------------------------------
# Locale text collection and application
# ---------------------------------------------------------------------------

def _walk_locale_collect(obj: Any, add) -> None:
    if isinstance(obj, str):
        clean = _strip_ctrl(obj)
        if clean and len(clean) > 2:
            add(clean)
    elif isinstance(obj, list):
        for item in obj:
            _walk_locale_collect(item, add)
    elif isinstance(obj, dict):
        for val in obj.values():
            _walk_locale_collect(val, add)


def _walk_locale_apply(obj: Any, cache: dict) -> Any:
    if isinstance(obj, str):
        return _translate_with_ctrl(obj, cache) if obj.strip() else obj
    elif isinstance(obj, list):
        return [_walk_locale_apply(item, cache) for item in obj]
    elif isinstance(obj, dict):
        return {k: _walk_locale_apply(v, cache) for k, v in obj.items()}
    return obj


def _collect_locale_texts(locale_info: dict) -> list[str]:
    src = locale_info.get("backup", locale_info["src"])
    seen: set[str] = set()
    result: list[str] = []

    def add(text: str):
        if text not in seen:
            seen.add(text)
            result.append(text)

    if locale_info["type"] == "dir":
        for jf in sorted(src.glob("*.json")):
            try:
                data = json.loads(jf.read_text(encoding="utf-8", errors="replace"))
                _walk_locale_collect(data, add)
            except Exception:
                pass
    elif locale_info["type"] == "file":
        try:
            data = json.loads(src.read_text(encoding="utf-8", errors="replace"))
            _walk_locale_collect(data, add)
        except Exception:
            pass
    elif locale_info["type"] == "master":
        try:
            data = json.loads(src.read_text(encoding="utf-8", errors="replace"))
            lang_key = locale_info.get("lang_key", "")
            section = data.get(lang_key, {}) if lang_key else data
            _walk_locale_collect(section, add)
        except Exception:
            pass

    return result


def _apply_locale_translations(locale_info: dict, cache: dict, log) -> None:
    src = locale_info["src"]        # live path to write
    backup = locale_info.get("backup", src)  # original to read from

    if locale_info["type"] == "dir":
        for jf_backup in sorted(backup.glob("*.json")):
            jf_live = src / jf_backup.name
            try:
                data = json.loads(jf_backup.read_text(encoding="utf-8", errors="replace"))
                translated = _walk_locale_apply(data, cache)
                jf_live.write_text(
                    json.dumps(translated, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
            except Exception as e:
                log(f"  Aviso: {jf_backup.name} mantido ({e})")

    elif locale_info["type"] == "file":
        try:
            data = json.loads(backup.read_text(encoding="utf-8", errors="replace"))
            translated = _walk_locale_apply(data, cache)
            src.write_text(
                json.dumps(translated, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception as e:
            log(f"  Aviso: {src.name} mantido ({e})")

    elif locale_info["type"] == "master":
        try:
            data = json.loads(backup.read_text(encoding="utf-8", errors="replace"))
            lang_key = locale_info.get("lang_key", "")
            if lang_key and lang_key in data:
                data[lang_key] = _walk_locale_apply(data[lang_key], cache)
            src.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception as e:
            log(f"  Aviso: {src.name} mantido ({e})")


# ---------------------------------------------------------------------------
# Coleta de textos (base data)
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
# Aplicacao de traducoes (base data)
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
