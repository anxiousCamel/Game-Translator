from __future__ import annotations
import json
import re
from pathlib import Path


def detect_game_type(path: Path) -> str | None:
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            try:
                snippet = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
                if "tw-passagedata" in snippet:
                    return "twine"
            except Exception:
                pass
        if suffix == ".exe":
            return detect_game_type(path.parent)
        return None

    if not path.is_dir():
        return None

    # RenPy: pasta game/ com .rpy, .rpa, ou diretório renpy/ na raiz
    game_dir = path / "game"
    if game_dir.is_dir():
        if list(game_dir.glob("*.rpy")) or list(game_dir.rglob("*.rpy")):
            return "renpy"
        if list(game_dir.glob("*.rpa")):
            return "renpy"
        if list(game_dir.glob("*.rpyc")) or list(game_dir.rglob("*.rpyc")):
            return "renpy"
    if (path / "renpy").is_dir():
        return "renpy"
    if list(path.glob("**/*.rpy")) or list(path.glob("**/*.rpyc")):
        return "renpy"

    # Twine: arquivo HTML na pasta
    for f in list(path.glob("*.html")) + list(path.glob("*.htm")):
        try:
            snippet = f.read_text(encoding="utf-8", errors="ignore")[:200_000]
            if "tw-passagedata" in snippet:
                return "twine"
        except Exception:
            continue

    # RPGMaker MV/MZ: data/System.json ou www/data/System.json
    if (path / "www" / "data" / "System.json").exists():
        return "rpgmaker"
    if (path / "data" / "System.json").exists():
        return "rpgmaker"
    if (path / "Game.rpgproject").exists() or (path / "game.rpgproject").exists():
        return "rpgmaker"

    return None


GAME_TYPE_LABELS = {
    "twine": "Twine / SugarCube",
    "renpy": "RenPy",
    "rpgmaker": "RPGMaker MV/MZ",
}


# ---------------------------------------------------------------------------
# Text sampling for language detection
# ---------------------------------------------------------------------------

_RENPY_DIALOG_RE = re.compile(r'^\s+(?:\w+\s+)?"((?:[^"\\]|\\.)+)"', re.MULTILINE)
_TWINE_PASSAGE_RE = re.compile(r'<tw-passagedata[^>]*>(.*?)</tw-passagedata>', re.DOTALL)


def _collect_strings(obj: object, out: list[str], limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(obj, str):
        if obj.strip():
            out.append(obj)
    elif isinstance(obj, list):
        for item in obj:
            _collect_strings(item, out, limit)
            if len(out) >= limit:
                return
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out, limit)
            if len(out) >= limit:
                return


def sample_game_texts(path: Path, game_type: str, n: int = 200) -> list[str]:
    """Return up to n raw strings from game files for language detection."""
    try:
        if game_type == "rpgmaker":
            return _sample_rpgmaker(path, n)
        if game_type == "renpy":
            return _sample_renpy(path, n)
        if game_type == "twine":
            return _sample_twine(path, n)
    except Exception:
        pass
    return []


def _sample_rpgmaker(path: Path, n: int) -> list[str]:
    data_dir = next(
        (d for d in [path / "www" / "data", path / "data"] if d.is_dir()), None
    )
    if not data_dir:
        return []
    texts: list[str] = []
    for jf in sorted(data_dir.glob("*.json"))[:8]:
        try:
            obj = json.loads(jf.read_text(encoding="utf-8", errors="ignore"))
            _collect_strings(obj, texts, n)
        except Exception:
            pass
        if len(texts) >= n:
            break
    return texts[:n]


def _sample_renpy(path: Path, n: int) -> list[str]:
    rpy_files = list(path.glob("**/*.rpy"))[:12]
    texts: list[str] = []
    for rpy in rpy_files:
        try:
            content = rpy.read_text(encoding="utf-8", errors="ignore")
            for m in _RENPY_DIALOG_RE.finditer(content):
                texts.append(m.group(1))
        except Exception:
            pass
        if len(texts) >= n:
            break
    return texts[:n]


def _sample_twine(path: Path, n: int) -> list[str]:
    files = [path] if path.is_file() else (list(path.glob("*.html")) + list(path.glob("*.htm")))
    texts: list[str] = []
    for f in files[:3]:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")[:800_000]
            for m in _TWINE_PASSAGE_RE.finditer(content):
                texts.append(m.group(1)[:300])
        except Exception:
            pass
        if len(texts) >= n:
            break
    return texts[:n]
