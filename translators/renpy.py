from __future__ import annotations
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from .base import BaseTranslator
from .engine import ensure_model, translate_texts

# Linhas que nunca contêm diálogo traduzível
_SKIP_KEYWORDS = re.compile(
    r"^\s*(?:"
    r"define|default|image|transform|style|init|python|label|menu|jump|call|return"
    r"|show|hide|play|stop|pause|nvl|scene|with|voice|queue|extend|window"
    r"|centered|vbox|hbox|frame|text|imagebutton|textbutton|add|null|bar"
    # Propriedades de estilo (nunca sao dialogo)
    r"|background|foreground|color|font|size|spacing|padding|pos|anchor|align|fill"
    r"|action|child|focus_mask|activate_sound|hover_sound|keyboard_focus"
    r")\b"
    r"|^\s*\$"          # Python one-liners: $ var = ...
    r"|^\s*#"           # Comentários
    r"|^\s+\w+_\w+\s"  # propriedade composta tipo hover_background, text_color, etc.
)

# Diálogo: linha indentada, opcionalmente nome do personagem, string entre aspas.
# Lookahead negativo descarta argumentos Python (terminam com , ou )) e atribuicoes (=).
_DIALOGUE_RE = re.compile(r'^(\s+)(?:\w+\s+)?("(?:[^"\\]|\\.)*")(?!\s*[,)=])', re.MULTILINE)

# Interpolações RenPy [var] e tags {b}{/b} dentro das strings — preservar
_INTERP_RE = re.compile(r"\[.*?\]|\{[^}]*\}")

# Valores tecnico-nao-traduzíveis: cores hex, caminhos de arquivo, numeros puros
_SKIP_VALUE_RE = re.compile(
    r"^#[0-9a-fA-F]{3,8}$"                     # cor hex #rgb #rrggbb etc.
    r"|^[0-9a-fA-F]{6,8}$"                      # cor hex sem #
    r"|^\d[\d., ]*(?:px|em|%)?$"                # numero/medida
    r"|^[a-zA-Z0-9_/\\.\-]+\.[a-zA-Z]{2,4}$"   # caminho sem espaços
    r"|^.+\.(png|jpg|jpeg|gif|webp|bmp|svg"      # caminho com espaços + extensao conhecida
    r"|mp3|ogg|wav|opus|flac"
    r"|mp4|webm|avi|ogv"
    r"|ttf|otf|woff"
    r"|rpy|rpyc|rpa|json|xml|txt)$"
)


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
        batch_size = 80
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
                return extracted, game_sub

        # Caso .rpyc direto (sem .rpa): descompila in-place em pasta separada
        if game_sub.is_dir():
            rpyc = list(game_sub.rglob("*.rpyc"))
            if rpyc:
                extracted = self._extract_rpyc(game_sub)
                return extracted, game_sub

        raise FileNotFoundError(f"Nenhum arquivo .rpy, .rpa ou .rpyc encontrado em: {self.path}")

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
        # Sort by size ascending — script archives are small; stop once .rpy files are found
        # to avoid extracting large data archives (images, audio) unnecessarily.
        sorted_rpas = sorted(rpa_files, key=lambda p: p.stat().st_size)
        for rpa in sorted_rpas:
            size_mb = rpa.stat().st_size / 1_048_576
            self.log(f"Extraindo {rpa.name} ({size_mb:.0f} MB)...")
            result = subprocess.run(
                [sys.executable, "-m", "unrpa", "-mp", str(extract_dir), str(rpa)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                self.log(f"  Aviso: falha ao extrair {rpa.name}: {result.stderr[:200]}")
                continue
            self.log(f"  {rpa.name} extraido.")
            if list(extract_dir.rglob("*.rpy")) or list(extract_dir.rglob("*.rpyc")):
                break
        rpy = list(extract_dir.rglob("*.rpy"))
        if not rpy:
            rpyc = list(extract_dir.rglob("*.rpyc"))
            if rpyc:
                self.log(f"Apenas .rpyc encontrados ({len(rpyc)}). Descompilando...")
                self._decompile_rpyc(extract_dir)
                rpy = list(extract_dir.rglob("*.rpy"))
            if not rpy:
                raise FileNotFoundError(
                    "Nenhum .rpy encontrado apos extrair o .rpa e descompilar .rpyc. "
                    "Verifique se 'unrpyc' esta instalado (pip install unrpyc)."
                )
        self.log(f"{len(rpy)} arquivos .rpy prontos.")
        return extract_dir

    def _extract_rpyc(self, game_dir: Path) -> Path:
        extract_dir = self.path / "_rpy_extracted"
        if extract_dir.exists():
            rpy = list(extract_dir.rglob("*.rpy"))
            if rpy:
                self.log(f"Usando .rpy ja descompilados ({len(rpy)} arquivos).")
                return extract_dir
        extract_dir.mkdir(exist_ok=True)
        rpyc_files = list(game_dir.rglob("*.rpyc"))
        self.log(f"{len(rpyc_files)} arquivos .rpyc encontrados. Descompilando...")
        for rpyc in rpyc_files:
            rel = rpyc.relative_to(game_dir)
            dest = extract_dir / rel.with_suffix(".rpyc")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rpyc, dest)
        self._decompile_rpyc(extract_dir)
        rpy = list(extract_dir.rglob("*.rpy"))
        if not rpy:
            raise FileNotFoundError(
                "Descompilacao .rpyc falhou. Verifique se 'unrpyc' esta instalado."
            )
        self.log(f"{len(rpy)} arquivos .rpy descompilados.")
        return extract_dir

    def _get_unrpyc_dir(self) -> Path:
        tools_dir = Path(__file__).parent.parent / "_tools" / "unrpyc"
        script = tools_dir / "unrpyc.py"
        if script.exists() and (tools_dir / "decompiler").is_dir():
            return tools_dir
        self.log("Baixando unrpyc (necessario uma vez)...")
        tools_dir.mkdir(parents=True, exist_ok=True)
        url = "https://github.com/CensoredUsername/unrpyc/archive/refs/heads/master.zip"
        try:
            with urllib.request.urlopen(url) as resp:
                data = resp.read()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for member in zf.namelist():
                    # strip leading "unrpyc-master/"
                    rel = "/".join(member.split("/")[1:])
                    if not rel:
                        continue
                    dest = tools_dir / rel
                    if member.endswith("/"):
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(member))
            self.log("  unrpyc baixado.")
        except Exception as e:
            raise RuntimeError(
                f"Falha ao baixar unrpyc: {e}\n"
                "Baixe manualmente de https://github.com/CensoredUsername/unrpyc "
                f"e extraia em: {tools_dir}"
            )
        return tools_dir

    def _decompile_rpyc(self, directory: Path) -> int:
        rpyc_files = list(directory.rglob("*.rpyc"))
        if not rpyc_files:
            return 0
        unrpyc_dir = self._get_unrpyc_dir()
        script = unrpyc_dir / "unrpyc.py"
        count = 0
        for rpyc in rpyc_files:
            result = subprocess.run(
                [sys.executable, str(script), str(rpyc)],
                capture_output=True, text=True,
                cwd=str(unrpyc_dir),
            )
            if result.returncode == 0:
                count += 1
            else:
                self.log(f"  Aviso: falha ao descompilar {rpyc.name}: {result.stderr[:200]}")
        self.log(f"  {count}/{len(rpyc_files)} .rpyc descompilados.")
        return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _segment(raw: str) -> list[tuple[str, bool]]:
    """Split raw string into [(text, is_protected)] pairs.
    Protected: [var], {tag}, and escape sequences like \" \\."""
    segs: list[tuple[str, bool]] = []
    pos = 0
    for m in _INTERP_RE.finditer(raw):
        if m.start() > pos:
            segs.append((raw[pos:m.start()], False))
        segs.append((m.group(0), True))
        pos = m.end()
    if pos < len(raw):
        segs.append((raw[pos:], False))
    return segs or [(raw, False)]


def _inner_text(raw: str) -> str:
    """Return the translatable portion of raw (protected patterns stripped, whitespace stripped)."""
    return "".join(s for s, p in _segment(raw) if not p).strip()


def _translate_raw(raw: str, cache: dict) -> str:
    """Apply cached translation to raw dialogue content. Returns raw unchanged if not translatable."""
    segs = _segment(raw)
    inner = "".join(s for s, p in segs if not p).strip()
    if not inner or _SKIP_VALUE_RE.match(inner) or not cache.get(inner):
        return raw

    safe = cache[inner].replace('\\"', '"').replace('"', '\\"')

    non_p = [(i, s) for i, (s, p) in enumerate(segs) if not p]
    if not non_p:
        return raw

    if len(non_p) == 1:
        idx, orig = non_p[0]
        new_val = orig.replace(inner, safe, 1) if inner in orig else safe
        new_segs = list(segs)
        new_segs[idx] = (new_val, False)
        return "".join(s for s, _ in new_segs)

    # Protected patterns split the translatable text (e.g. "Hi {b}there{/b} friend").
    # If all protected parts are formatting tags {i}/{b}/{color}/etc. (not [var] interpolations),
    # apply the translation without the tags — losing bold/italic is better than no translation.
    # If any protected part is a variable interpolation [var], leave unchanged to avoid data loss.
    protected_parts = [s for s, p in segs if p]
    if any(s.startswith("[") for s in protected_parts):
        return raw
    return safe


def _extract_strings(content: str) -> list[str]:
    seen: set[str] = set()
    result = []
    for line in content.splitlines():
        if _SKIP_KEYWORDS.match(line):
            continue
        for m in _DIALOGUE_RE.finditer(line):
            inner = _inner_text(m.group(2)[1:-1])
            if inner and len(inner) > 2 and not _SKIP_VALUE_RE.match(inner) and inner not in seen:
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
            translated = _translate_raw(raw, cache)
            if translated != raw:
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
