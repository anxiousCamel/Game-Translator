from __future__ import annotations
import json
import re
from pathlib import Path


_TWINE_MARKERS = ("tw-passagedata", "tw-storydata", "SugarCube", "Twine.version")


def _is_twine_html(path: Path) -> bool:
    try:
        # Read in chunks — markers appear at various depths; 1 MB covers most cases.
        # For huge files (>1 MB) with late markers we do a targeted tail search.
        with path.open(encoding="utf-8", errors="ignore") as fh:
            head = fh.read(1_000_000)
        if any(m in head for m in _TWINE_MARKERS):
            return True
        # tw-passagedata can be near the end of very large compiled HTML exports
        size = path.stat().st_size
        if size > 1_000_000:
            with path.open("rb") as fb:
                fb.seek(max(0, size - 200_000))
                tail = fb.read().decode("utf-8", errors="ignore")
            if any(m in tail for m in _TWINE_MARKERS):
                return True
    except Exception:
        pass
    return False


def detect_game_type(path: Path) -> str | None:
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            if _is_twine_html(path):
                return "twine"
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

    # Twine / SugarCube: HTML file in root
    for f in list(path.glob("*.html")) + list(path.glob("*.htm")):
        if _is_twine_html(f):
            return "twine"

    # RPGMaker MV/MZ: data/System.json ou www/data/System.json
    if (path / "www" / "data" / "System.json").exists():
        return "rpgmaker"
    if (path / "data" / "System.json").exists():
        return "rpgmaker"
    if (path / "Game.rpgproject").exists() or (path / "game.rpgproject").exists():
        return "rpgmaker"

    # Unity: pasta *_Data com UnityPlayer.dll ao lado
    if (path / "UnityPlayer.dll").exists():
        for item in path.iterdir():
            if item.is_dir() and item.name.endswith("_Data"):
                return "unity"

    # Godot: project.godot / .pck next to exe / export_presets.cfg
    if (path / "project.godot").exists() or (path / "export_presets.cfg").exists():
        return "godot"
    if list(path.glob("*.pck")):
        return "godot"

    # Wolf RPG Editor: Data.wolf or .wolf files in Data/ subdirectory
    if (path / "Data.wolf").exists():
        return "wolf"
    data_dir = path / "Data"
    if data_dir.is_dir() and list(data_dir.glob("*.wolf")):
        return "wolf"
    if list(path.glob("*.wolf")):
        return "wolf"

    # Unreal Engine: .pak in Paks/ subdirectory or .uproject
    if list(path.glob("**/*.uproject"))[:1]:
        return "unreal"
    paks_candidates = [
        path / "Content" / "Paks",
        path / "WindowsNoEditor" / "Content" / "Paks",
        path / "Windows" / "Content" / "Paks",
    ]
    for paks_dir in paks_candidates:
        if paks_dir.is_dir() and list(paks_dir.glob("*.pak"))[:1]:
            return "unreal"

    return None


GAME_TYPE_LABELS = {
    "twine": "Twine / SugarCube",
    "renpy": "RenPy",
    "rpgmaker": "RPGMaker MV/MZ",
    "unity": "Unity",
    "godot": "Godot 3 / 4",
    "wolf": "Wolf RPG Editor",
    "unreal": "Unreal Engine",
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
        if game_type == "unity":
            return _sample_unity(path, n)
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


def _sample_unity(path: Path, n: int) -> list[str]:
    """Sample readable strings from Unity localization bundles (fast, small files)."""
    texts: list[str] = []
    for item in path.iterdir():
        if item.is_dir() and item.name.endswith("_Data"):
            aa = item / "StreamingAssets" / "aa"
            if aa.exists():
                for bundle in sorted(aa.rglob("*.bundle"))[:6]:
                    if "catalog" in bundle.name.lower():
                        continue
                    try:
                        import UnityPy as unitypy
                        env = unitypy.load(str(bundle))
                        for obj in env.objects:
                            if obj.type.name == "MonoBehaviour":
                                try:
                                    tree = obj.read_typetree()
                                    _collect_strings(tree, texts, n)
                                except Exception:
                                    pass
                            elif obj.type.name == "TextAsset":
                                try:
                                    data = obj.read()
                                    raw = getattr(data, "text", "") or ""
                                    for line in raw.splitlines()[:50]:
                                        if line.strip():
                                            texts.append(line.strip()[:200])
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    if len(texts) >= n:
                        return texts[:n]
    return texts[:n]
