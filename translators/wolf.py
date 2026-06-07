from __future__ import annotations
import json
import os
import re
import struct
from pathlib import Path

from .base import BaseTranslator
from .engine import (
    ensure_model, translate_texts,
    load_glossary, mask_glossary, unmask_glossary,
    mask_code_vars, unmask_code_vars,
)

# ---------------------------------------------------------------------------
# Wolf RPG Editor binary format constants
# ---------------------------------------------------------------------------

# Wolf RPG Editor 2 map/common event header magic
_WOLF2_MAGIC  = b'\x00\x00\x00\x01\x01'
# Wolf RPG Editor 3 data container magic
_WOLF3_MAGIC  = b'WLF3'
# DXLib archive magic (Wolf RPG 3 uses DXLib for packaging)
_DXLIB_MAGIC  = b'DX'

# Known Wolf RPG 3 file type codes (first 4 bytes of plaintext sections)
_WOLF_TYPES = {
    0x00000000: 'map',
    0x00000001: 'database',
    0x00000002: 'common_event',
}

# File extensions to scan for text data
_DATA_EXTS = frozenset({'.wolf', '.mps', '.dat', '.project', '.json', '.csv', '.txt'})


# ---------------------------------------------------------------------------
# String scanning: UTF-16LE null-terminated strings in binary
# ---------------------------------------------------------------------------

_MIN_CHARS = 3
_MAX_CHARS = 500

# Prefilter regex for post-decode validation
_SKIP_RE = re.compile(
    r'^[A-Z_][A-Z0-9_]{2,}$'           # CONSTANT
    r'|^[a-z][A-Za-z0-9_]{2,}$'        # camelCase
    r'|^\d'                              # starts with digit
    r'|^\.'                              # dot-start (path/ext)
    r'|^[A-Za-z]:\\|^/'                 # file path
    r'|^[A-Za-z][A-Za-z0-9_]*\s*[=(]'  # code expression
)


def _translatable(s: str) -> bool:
    s = s.strip()
    if len(s) < _MIN_CHARS:
        return False
    alpha = sum(c.isalpha() for c in s)
    if alpha < 2:
        return False
    if _SKIP_RE.search(s):
        return False
    if '_' in s and ' ' not in s and '\n' not in s:
        return False
    return True


def _scan_utf16le(data: bytes, out: set, positions: dict | None = None) -> None:
    """
    Scan binary data for UTF-16LE null-terminated strings.
    positions: if provided, maps original_string → list of (byte_offset, byte_length)
               so translations can be applied back in-place.
    """
    i = 0
    dlen = len(data) - 1
    while i < dlen:
        if i + 1 < dlen and data[i + 1] == 0:
            # Potential start of UTF-16LE string
            start = i
            end   = i
            while end + 1 < len(data) and not (data[end] == 0 and data[end + 1] == 0):
                end += 2
            raw = data[start:end]
            if 2 <= len(raw) <= _MAX_CHARS * 2 and len(raw) % 2 == 0:
                try:
                    s = raw.decode('utf-16-le')
                    if _translatable(s):
                        out.add(s)
                        if positions is not None:
                            positions.setdefault(s, []).append((start, len(raw) + 2))  # +2 for null
                except Exception:
                    pass
        i += 2  # advance by 2 bytes (UTF-16LE units)


def _scan_utf8_lpstr(data: bytes, out: set, positions: dict | None = None) -> None:
    """
    Scan for length-prefixed UTF-8 strings: uint32_le(n) + n bytes.
    Used by Wolf RPG Editor 2 and some Wolf RPG 3 plaintext sections.
    """
    i = 0
    dlen = len(data)
    while i < dlen - 4:
        n = struct.unpack_from('<I', data, i)[0]
        if _MIN_CHARS <= n <= _MAX_CHARS and i + 4 + n <= dlen:
            raw = data[i + 4: i + 4 + n]
            try:
                s = raw.decode('utf-8')
                if _translatable(s):
                    out.add(s)
                    if positions is not None:
                        positions.setdefault(s, []).append((i, 4 + n))
            except Exception:
                pass
        i += 1


# ---------------------------------------------------------------------------
# Binary patching: in-place string replacement
# ---------------------------------------------------------------------------

def _patch_utf16le(data: bytes, cache: dict) -> bytes:
    """
    Replace UTF-16LE strings in a binary blob.
    Translations longer than original are truncated; shorter are padded with spaces.
    """
    result = bytearray(data)
    i = 0
    dlen = len(data) - 1

    while i < dlen:
        if i + 1 < dlen and data[i + 1] == 0:
            start = i
            end   = i
            while end + 1 < len(data) and not (data[end] == 0 and data[end + 1] == 0):
                end += 2
            raw = data[start:end]
            if 2 <= len(raw) <= _MAX_CHARS * 2 and len(raw) % 2 == 0:
                try:
                    s  = raw.decode('utf-16-le')
                    tr = cache.get(s)
                    if tr and tr != s:
                        tr_enc  = tr.encode('utf-16-le')
                        orig_sz = len(raw)  # excluding null terminator
                        if len(tr_enc) <= orig_sz:
                            # Pad shorter translation with spaces
                            padded = tr_enc + b'\x20\x00' * ((orig_sz - len(tr_enc)) // 2)
                            result[start: start + orig_sz] = padded[:orig_sz]
                        else:
                            # Truncate to fit original slot
                            result[start: start + orig_sz] = tr_enc[:orig_sz]
                except Exception:
                    pass
                i = end + 2  # skip past this string (past null terminator)
                continue
        i += 2

    return bytes(result)


def _patch_utf8_lpstr(data: bytes, cache: dict) -> bytes:
    """Rebuild binary with translated UTF-8 length-prefixed strings."""
    result = bytearray()
    i = 0
    dlen = len(data)
    last = 0

    while i < dlen - 4:
        n = struct.unpack_from('<I', data, i)[0]
        if _MIN_CHARS <= n <= _MAX_CHARS and i + 4 + n <= dlen:
            raw = data[i + 4: i + 4 + n]
            try:
                s  = raw.decode('utf-8')
                tr = cache.get(s)
                if tr and tr != s:
                    result += data[last:i]
                    tr_enc = tr.encode('utf-8')
                    result += struct.pack('<I', len(tr_enc)) + tr_enc
                    last = i + 4 + n
                    i    = last
                    continue
            except Exception:
                pass
        i += 1

    result += data[last:]
    return bytes(result)


# ---------------------------------------------------------------------------
# Cache helper
# ---------------------------------------------------------------------------

def _save_cache(path: Path, cache: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Game data file discovery
# ---------------------------------------------------------------------------

def _find_data_files(game_dir: Path) -> list[Path]:
    """
    Locate Wolf RPG data files.
    Priority: Data.wolf → Data/**.wolf → Data/**.mps → any .csv/.json/.txt in Data/.
    """
    candidates = []
    # Single packed file (Wolf RPG 3)
    for p in game_dir.rglob('Data.wolf'):
        candidates.append(p)
    # Unpacked files (Wolf RPG 2 / extracted Wolf RPG 3)
    data_dirs = list(game_dir.rglob('Data'))
    for d in data_dirs:
        if d.is_dir():
            for ext in ('.wolf', '.mps', '.dat', '.csv', '.json'):
                candidates.extend(sorted(d.rglob(f'*{ext}')))
    return candidates


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

class WolfRPGTranslator(BaseTranslator):
    """
    Wolf RPG Editor game translator.

    Supports:
    - Wolf RPG 2: individual .wolf/.mps files with UTF-8 length-prefixed strings
    - Wolf RPG 3: single Data.wolf (UTF-16LE string scanning)
    - DXLib-encrypted containers: best-effort UTF-16LE scan of accessible regions

    Limitation: DXLib-encrypted Data.wolf requires the game's decryption key;
    visible text may be partial (unencrypted header strings only).
    """

    def translate(self) -> Path:
        data_files = _find_data_files(self.path)
        if not data_files:
            raise RuntimeError(
                "Nenhum arquivo de dados Wolf RPG encontrado.\n"
                "Selecione a pasta raiz do jogo (que contem Game.ini ou Data.wolf)."
            )

        ensure_model(self.src_lang, self.tgt_lang, self.log, engine=self.engine)

        cache_file = self.path / 'traducoes_wolf.json'
        cache: dict[str, str] = {}
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text(encoding='utf-8'))
                self.log(f"Cache: {sum(1 for v in cache.values() if v)} entradas.")
            except Exception:
                pass

        glossary = load_glossary(force=True)

        self.log(f"\n[1/3] Analisando {len(data_files)} arquivos...")
        all_strings: set[str] = set()

        # Classify each file and scan for strings
        file_modes: dict[Path, str] = {}  # path → 'utf8_lp' | 'utf16le'
        for i, fp in enumerate(data_files):
            self.set_progress(0.05 + 0.20 * i / max(len(data_files), 1),
                              f"Escaneando: {fp.name}")
            try:
                raw = fp.read_bytes()
            except Exception:
                continue

            is_dxlib   = raw[:2] == _DXLIB_MAGIC
            is_wolf3   = raw[:4] == _WOLF3_MAGIC
            is_plaintext = fp.suffix.lower() in {'.csv', '.json', '.txt'}

            if is_dxlib:
                self.log(f"  {fp.name}: DXLib (criptografado) — scan parcial UTF-16LE")
                file_modes[fp] = 'utf16le'
                _scan_utf16le(raw, all_strings)
            elif is_wolf3:
                self.log(f"  {fp.name}: Wolf RPG 3 — UTF-16LE")
                file_modes[fp] = 'utf16le'
                _scan_utf16le(raw, all_strings)
            elif is_plaintext:
                # Text CSV/JSON: read as UTF-8 and extract quoted strings
                try:
                    text = raw.decode('utf-8', errors='replace')
                    for line in text.splitlines():
                        for cell in re.split(r'[,\t]', line):
                            s = cell.strip().strip('"')
                            if _translatable(s):
                                all_strings.add(s)
                    file_modes[fp] = 'text'
                except Exception:
                    pass
            else:
                # Wolf RPG 2 / unknown: try both
                _scan_utf8_lpstr(raw, all_strings)
                _scan_utf16le(raw, all_strings)
                file_modes[fp] = 'both'
                self.log(f"  {fp.name}: formato desconhecido — scan duplo")

        to_translate = [s for s in sorted(all_strings) if not cache.get(s)]
        self.log(f"\n  {len(all_strings)} strings | a traduzir: {len(to_translate)}")

        if not all_strings:
            self.log(
                "\n[Aviso] Nenhuma string extraida. O arquivo pode estar totalmente criptografado.\n"
                "Para Wolf RPG 3 criptografado, e necessario extrair os dados com WolfRPGEditor primeiro."
            )

        # ── 2: Translate ─────────────────────────────────────────────────────
        if to_translate:
            self.log(f"\n[2/3] Traduzindo {len(to_translate)} strings...")
            batch_sz = 40
            total    = len(to_translate)
            for i in range(0, total, batch_sz):
                chunk = to_translate[i: i + batch_sz]
                masked, tags_l, gloss_l = [], [], []
                for s in chunk:
                    m, tags  = mask_code_vars(s)
                    m, gloss = mask_glossary(m, glossary)
                    masked.append(m)
                    tags_l.append(tags)
                    gloss_l.append(gloss)
                translated = translate_texts(masked, self.src_lang, self.tgt_lang, engine=self.engine)
                translated = [
                    unmask_code_vars(unmask_glossary(t, g), tags)
                    for t, g, tags in zip(translated, gloss_l, tags_l)
                ]
                cache.update(zip(chunk, translated))
                done = min(i + batch_sz, total)
                if done % 500 < batch_sz or done >= total:
                    _save_cache(cache_file, cache)
                self.set_progress(0.25 + 0.55 * done / total, f"Traduzindo... {done}/{total}")
                self.log(f"  {done}/{total}")
        else:
            self.log('\n[2/3] Tudo no cache.')

        # ── 3: Apply ─────────────────────────────────────────────────────────
        self.log('\n[3/3] Aplicando traducoes...')
        changed_count = 0
        for fp, mode in file_modes.items():
            self.set_progress(0.82, f"Aplicando: {fp.name}")
            try:
                raw = fp.read_bytes()
            except Exception:
                continue

            bak = fp.with_suffix(fp.suffix + '.bak')
            if not bak.exists():
                bak.write_bytes(raw)

            if mode == 'utf16le':
                new = _patch_utf16le(raw, cache)
            elif mode == 'text':
                lines = []
                try:
                    text = raw.decode('utf-8', errors='replace')
                    for line in text.splitlines(keepends=True):
                        tr_line = line
                        for orig, tr in cache.items():
                            if orig in tr_line:
                                tr_line = tr_line.replace(orig, tr)
                        lines.append(tr_line)
                    new = ''.join(lines).encode('utf-8')
                except Exception:
                    new = raw
            else:  # 'both'
                new = _patch_utf8_lpstr(raw, cache)
                new = _patch_utf16le(new, cache)

            if new != raw:
                tmp = fp.with_suffix(fp.suffix + '.tmp')
                tmp.write_bytes(new)
                os.replace(str(tmp), str(fp))
                changed_count += 1
                self.log(f"  Modificado: {fp.name}")

        _save_cache(cache_file, cache)
        self.set_progress(1.0, 'Concluido!')
        self.log(f"\nConcluido! {changed_count} arquivos modificados. Cache em: {cache_file}")
        return self.path
