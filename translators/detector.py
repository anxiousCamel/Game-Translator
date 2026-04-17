from __future__ import annotations
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
    if (path / "renpy").is_dir():
        return "renpy"
    if list(path.glob("**/*.rpy")):
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
