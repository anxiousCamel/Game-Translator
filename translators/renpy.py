from __future__ import annotations
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .base import BaseTranslator
from .engine import ensure_model, translate_texts

# Linhas que nunca contêm diálogo traduzível
_SKIP_KEYWORDS = re.compile(
    r"^\s*(?:"
    r"define|default|image|transform|style|init|python|label|menu|jump|call|return"
    r"|show|hide|play|stop|pause|nvl|scene|with|voice|queue|extend|window"
    r"|centered|vbox|hbox|frame|text|imagebutton|textbutton|add|null|bar"
    r")\b"
    r"|^\s*\$"          # Python one-liners: $ var = ...
    r"|^\s*#"           # Comentários
)

# Diálogo: linha indentada, opcionalmente nome do personagem, string entre aspas
_DIALOGUE_RE = re.compile(r'^(\s+)(?:\w+\s+)?("(?:[^"\\]|\\.)*")', re.MULTILINE)

# Interpolações RenPy [var] e tags {b}{/b} dentro das strings — preservar
_INTERP_RE = re.compile(r"\[.*?\]|\{[^}]*\}")


class RenpyTranslator(BaseTranslator):
    def translate(self) -> Path:
        source_dir, dest_dir = self._find_dirs()
        ensure_model(self.src_lang, self.tgt_lang, self.log)

        source_files = sorted(source_dir.rglob("*.rpy"))
        self.log(f"{len(source_files)} arquivos .rpy encontrados.")

        cache_path = self.path / "traducoes_renpy.json"
        cache = _load_cache(cache_path, self.log)

        texts = _collect_all_texts(source_files)
        to_do = [t for t in texts if not cache.get(t)]
        self.log(f"{len(texts)} textos unicos | {len(to_do)} para traduzir.")

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

        # Aplica traducoes e grava em dest_dir (in-place ou na pasta game/)
        self.log(f"Aplicando traducoes em: {dest_dir.relative_to(self.path)}/")
        for src in source_files:
            rel = src.relative_to(source_dir)
            out = dest_dir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            new_content = _apply_translations(
                src.read_text(encoding="utf-8", errors="replace"), cache
            )
            out.write_text(new_content, encoding="utf-8")

        self.set_progress(1.0, "Concluido!")
        self.log("Jogo pronto. Inicie normalmente.")
        return dest_dir

    # ------------------------------------------------------------------

    def _find_dirs(self) -> tuple[Path, Path]:
        """Retorna (source_dir, dest_dir).
        source_dir = onde estao os .rpy originais (backup ou extraidos do .rpa)
        dest_dir   = onde gravar os .rpy traduzidos (pasta game/ real do jogo)
        """
        if not self.path.is_dir():
            raise FileNotFoundError(f"Caminho nao encontrado: {self.path}")

        game_sub = self.path / "game"

        # Caso normal: .rpy soltos em game/
        if game_sub.is_dir() and list(game_sub.rglob("*.rpy")):
            backup = self.path / "_rpy_backup"
            if not backup.exists():
                self._make_backup(game_sub, backup)
            return backup, game_sub

        # Fallback: .rpy na raiz do diretorio selecionado
        if list(self.path.glob("*.rpy")):
            backup = self.path.parent / f"{self.path.name}_rpy_backup"
            if not backup.exists():
                self._make_backup(self.path, backup)
            return backup, self.path

        # Caso .rpa: extrai os scripts
        if game_sub.is_dir():
            rpa = list(game_sub.glob("*.rpa"))
            if rpa:
                extracted = self._extract_rpa(game_sub, rpa)
                # .rpy extraidos vao direto para game/ (loose override o .rpa)
                return extracted, game_sub

        raise FileNotFoundError(f"Nenhum arquivo .rpy ou .rpa encontrado em: {self.path}")

    def _make_backup(self, src: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for rpy in sorted(src.rglob("*.rpy")):
            rel = rpy.relative_to(src)
            bak = dest / rel
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpy, bak)
        self.log(f"Backup criado em: {dest.name}/ ({len(list(dest.rglob('*.rpy')))} arquivos)")

    def _extract_rpa(self, game_dir: Path, rpa_files: list[Path]) -> Path:
        extract_dir = self.path / "_rpy_extracted"
        extract_dir.mkdir(exist_ok=True)
        for rpa in rpa_files:
            self.log(f"Extraindo {rpa.name} (pode demorar)...")
            result = subprocess.run(
                [sys.executable, "-m", "unrpa", "-mp", str(extract_dir), str(rpa)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Falha ao extrair {rpa.name}. "
                    f"Instale com: pip install unrpa\n"
                    f"Detalhe: {result.stderr[:300]}"
                )
            self.log(f"  {rpa.name} extraido.")
        rpy = list(extract_dir.rglob("*.rpy"))
        if not rpy:
            raise FileNotFoundError(
                "Nenhum .rpy encontrado apos extrair o .rpa. "
                "O jogo pode usar apenas scripts compilados (.rpyc)."
            )
        self.log(f"{len(rpy)} arquivos .rpy extraidos.")
        return extract_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_strings(content: str) -> list[str]:
    seen: set[str] = set()
    result = []
    for line in content.splitlines():
        if _SKIP_KEYWORDS.match(line):
            continue
        for m in _DIALOGUE_RE.finditer(line):
            raw = m.group(2)[1:-1]
            inner = _INTERP_RE.sub("", raw).strip()
            if inner and len(inner) > 2 and inner not in seen:
                seen.add(inner)
                result.append(inner)
    return result


def _collect_all_texts(rpy_files: list[Path]) -> list[str]:
    seen: set[str] = set()
    result = []
    for f in rpy_files:
        for t in _extract_strings(f.read_text(encoding="utf-8", errors="replace")):
            if t not in seen:
                seen.add(t)
                result.append(t)
    return result


def _apply_translations(content: str, cache: dict) -> str:
    lines = []
    for line in content.splitlines(keepends=True):
        if _SKIP_KEYWORDS.match(line):
            lines.append(line)
            continue
        new_line = line
        for m in list(_DIALOGUE_RE.finditer(line)):
            raw = m.group(2)[1:-1]
            inner = _INTERP_RE.sub("", raw).strip()
            if inner and cache.get(inner):
                translated = raw.replace(inner, cache[inner], 1)
                new_line = new_line.replace(f'"{raw}"', f'"{translated}"', 1)
        lines.append(new_line)
    return "".join(lines)


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
