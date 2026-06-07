from __future__ import annotations
import hashlib
import io
import json
import os
import re
import struct
from pathlib import Path

from .base import BaseTranslator
from .engine import (
    ensure_model, translate_texts,
    load_glossary, mask_glossary, unmask_glossary,
)

# ---------------------------------------------------------------------------
# PCK binary format (Godot 3 = v1, Godot 4 = v2)
# ---------------------------------------------------------------------------
#
# v1 header (40 bytes):  magic(4) version(4) godot_ver(12) reserved(16) file_count(4)
# v2 header (100 bytes): magic(4) version(4) godot_ver(12) pack_flags(4)
#                        file_base(8) reserved(64) file_count(4)
#
# File entry v1: path_len(4) path_bytes[path_len] offset(8) size(8) md5(16)
# File entry v2: path_len(4) path_bytes[path_len] offset(8) size(8) md5(16) flags(4)
#
# path_len includes null terminator + zero-padding to 4-byte boundary.

_PCK_MAGIC         = 0x43504447   # "GDPC" little-endian
_PACK_REL_FILEBASE = 1             # pack_flags bit: offsets relative to file_base
_V1_RESERVED       = 16
_V2_RESERVED       = 64


def _parse_pck(data: bytes) -> dict:
    buf = io.BytesIO(data)

    magic = struct.unpack('<I', buf.read(4))[0]
    if magic != _PCK_MAGIC:
        raise ValueError(f"Not a Godot PCK (magic {magic:#010x})")

    version              = struct.unpack('<I',  buf.read(4))[0]
    major, minor, patch  = struct.unpack('<III', buf.read(12))

    pack_flags = file_base = 0
    if version >= 2:
        pack_flags = struct.unpack('<I', buf.read(4))[0]
        file_base  = struct.unpack('<Q', buf.read(8))[0]
        buf.read(_V2_RESERVED)
    else:
        buf.read(_V1_RESERVED)

    file_count = struct.unpack('<I', buf.read(4))[0]

    files = []
    for _ in range(file_count):
        path_len = struct.unpack('<I', buf.read(4))[0]
        path_raw = buf.read(path_len)
        path     = path_raw.rstrip(b'\x00').decode('utf-8', errors='replace')
        ofs, sz  = struct.unpack('<QQ', buf.read(16))
        md5      = buf.read(16)
        flags    = struct.unpack('<I', buf.read(4))[0] if version >= 2 else 0
        if version >= 2 and (pack_flags & _PACK_REL_FILEBASE):
            ofs += file_base
        files.append({'path': path, 'offset': int(ofs), 'size': int(sz),
                      'md5': md5, 'flags': flags, 'data': None})

    for f in files:
        end = f['offset'] + f['size']
        f['data'] = data[f['offset']:end] if f['size'] > 0 and end <= len(data) else b''

    return {'version': version, 'godot_ver': (major, minor, patch),
            'pack_flags': pack_flags, 'file_base': file_base, 'files': files}


def _write_pck(info: dict) -> bytes:
    version   = info['version']
    godot_ver = info['godot_ver']
    files     = info['files']

    # Compute index size (header + all entries) to know where data starts
    idx_size = 4 + 4 + 12  # magic + version + godot_ver
    idx_size += (4 + 8 + _V2_RESERVED) if version >= 2 else _V1_RESERVED
    idx_size += 4  # file_count

    entry_meta = []
    for f in files:
        pb  = f['path'].encode('utf-8') + b'\x00'
        pad = (4 - (len(pb) % 4)) % 4
        es  = 4 + len(pb) + pad + 8 + 8 + 16 + (4 if version >= 2 else 0)
        idx_size += es
        entry_meta.append((pb, pad))

    # Assign absolute data offsets
    pos = idx_size
    offsets = []
    for f in files:
        offsets.append(pos)
        pos += len(f.get('data') or b'')

    out = bytearray()
    out += struct.pack('<I', _PCK_MAGIC)
    out += struct.pack('<I', version)
    out += struct.pack('<III', *godot_ver)
    if version >= 2:
        out += struct.pack('<I', 0)           # clear REL_FILEBASE flag
        out += struct.pack('<Q', idx_size)    # file_base = start of data
        out += bytes(_V2_RESERVED)
    else:
        out += bytes(_V1_RESERVED)
    out += struct.pack('<I', len(files))

    for i, (f, (pb, pad)) in enumerate(zip(files, entry_meta)):
        data = f.get('data') or b''
        out += struct.pack('<I', len(pb) + pad)
        out += pb + bytes(pad)
        out += struct.pack('<Q', offsets[i])
        out += struct.pack('<Q', len(data))
        out += hashlib.md5(data).digest()
        if version >= 2:
            out += struct.pack('<I', f.get('flags', 0))

    for f in files:
        out += f.get('data') or b''

    return bytes(out)


# ---------------------------------------------------------------------------
# Variable shield — single unified [T#] pass (no collision with [G#] glossary)
#
# Covers:
#   BBCode tags: [b], [/b], [color=red], [url=...], etc.
#   C printf:    %s, %d, %f, %.2f, %1$s, etc.
#   Named slots: {name}, {0}  (Godot 4 String.format)
#   Rich text:   <b>, <color=#fff>  (some Godot UI / TextMeshPro)
#   RPG vars:    %PLAYER_NAME%
# ---------------------------------------------------------------------------

_GODOT_SHIELD_RE = re.compile(
    r'\[/?(?:b|i|u|s|center|right|left|fill|indent|code|kbd|wave|tornado|shake|pulse|'
    r'rainbow|color|font|font_size|font_color|outline_size|outline_color|shadow_offset|'
    r'shadow_color|shadow_size|bgcolor|fgcolor|url|hint|img|table|cell|ol|ul|li|lb|rb|'
    r'lrm|rlm|p)(?:=[^\]]*)?\]'
    r'|(?<![A-Za-z0-9])%(?:\d+\$)?[-+0#]*\*?\d*(?:\.\*?\d*)?[sdifouxXeEgGcpn](?![A-Za-z0-9_])'
    r'|\{[A-Za-z0-9_]+\}'
    r'|<[^>]{1,60}>'
    r'|%[A-Z][A-Z0-9_]{1,20}%',
    re.IGNORECASE,
)


def _shield_vars(s: str) -> tuple[str, list[str]]:
    tokens: list[str] = []

    def _repl(m: re.Match) -> str:
        tokens.append(m.group(0))
        return f'[T{len(tokens) - 1}]'

    return _GODOT_SHIELD_RE.sub(_repl, s), tokens


def _unshield_vars(s: str, tokens: list[str]) -> str:
    for i, tok in enumerate(tokens):
        s = s.replace(f'[T{i}]', tok)
    return s


# ---------------------------------------------------------------------------
# Common translatable guard
# ---------------------------------------------------------------------------

_SKIP_GODOT = re.compile(
    r'^(?:res|user)://'
    r'|^[A-Z_][A-Z0-9_]{2,}$'
    r'|^\d'
    r'|^\.'
    r'|^[A-Za-z_]\w*\s*\('
    r'|^#'
)


def _translatable(s: str) -> bool:
    s = s.strip()
    if len(s) < 3:
        return False
    if sum(c.isalpha() for c in s) < 2:
        return False
    if _SKIP_GODOT.search(s):
        return False
    if '_' in s and ' ' not in s and '\n' not in s:
        return False
    return True


# ---------------------------------------------------------------------------
# .gd scripts — only tr() / atr() calls
# ---------------------------------------------------------------------------

_GD_TR_RE = re.compile(r'(\ba?tr\s*\(\s*)"((?:[^"\\]|\\.)*)"(\s*\))')


def _extract_gd(data: bytes, out: set) -> None:
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return
    for m in _GD_TR_RE.finditer(text):
        s = m.group(2).replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        if _translatable(s):
            out.add(s)


def _apply_gd(data: bytes, cache: dict) -> bytes:
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return data

    def _repl(m: re.Match) -> str:
        prefix, content, suffix = m.group(1), m.group(2), m.group(3)
        raw = content.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        tr = cache.get(raw)
        if tr and tr != raw:
            esc = (tr.replace('\\', '\\\\').replace('"', '\\"')
                     .replace('\n', '\\n').replace('\t', '\\t'))
            return f'{prefix}"{esc}"{suffix}'
        return m.group(0)

    new_text = _GD_TR_RE.sub(_repl, text)
    return new_text.encode('utf-8') if new_text != text else data


# ---------------------------------------------------------------------------
# .tscn / .tres — only values of known text-bearing properties
# ---------------------------------------------------------------------------

_TSCN_TEXT_PROPS = frozenset({
    'bb_code_text', 'button_text', 'caption', 'description', 'dialog_text',
    'footer', 'header', 'hint_tooltip', 'label', 'label_text', 'message',
    'placeholder_text', 'subtitle', 'tab_title', 'text', 'title',
    'tooltip_text', 'window_title',
})
_TSCN_PROP_RE = re.compile(
    r'^(' + '|'.join(sorted(_TSCN_TEXT_PROPS)) + r')\s*=\s*"((?:[^"\\]|\\.)*)"',
    re.MULTILINE,
)


def _extract_tscn(data: bytes, out: set) -> None:
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return
    for m in _TSCN_PROP_RE.finditer(text):
        s = m.group(2).replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        if _translatable(s):
            out.add(s)


def _apply_tscn(data: bytes, cache: dict) -> bytes:
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return data

    def _repl(m: re.Match) -> str:
        prop, content = m.group(1), m.group(2)
        raw = content.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        tr = cache.get(raw)
        if tr and tr != raw:
            esc = (tr.replace('\\', '\\\\').replace('"', '\\"')
                     .replace('\n', '\\n').replace('\t', '\\t'))
            return f'{prop} = "{esc}"'
        return m.group(0)

    new_text = _TSCN_PROP_RE.sub(_repl, text)
    return new_text.encode('utf-8') if new_text != text else data


# ---------------------------------------------------------------------------
# .csv — key column (first) preserved, value columns translated
# ---------------------------------------------------------------------------

def _csv_fields(line: str) -> list[str]:
    fields: list[str] = []
    i = 0
    n = len(line)
    while i <= n:
        if i == n:
            break
        if line[i] == '"':
            j, buf = i + 1, []
            while j < n:
                if line[j] == '"' and j + 1 < n and line[j + 1] == '"':
                    buf.append('"')
                    j += 2
                elif line[j] == '"':
                    j += 1
                    break
                else:
                    buf.append(line[j])
                    j += 1
            fields.append(''.join(buf))
            i = j + 1 if (j < n and line[j] == ',') else j
        else:
            end = line.find(',', i)
            if end == -1:
                fields.append(line[i:].strip())
                break
            fields.append(line[i:end].strip())
            i = end + 1
    return fields


def _csv_quote(s: str) -> str:
    return '"' + s.replace('"', '""') + '"' if (',' in s or '"' in s or '\n' in s) else s


def _extract_csv(data: bytes, out: set) -> None:
    try:
        text = data.decode('utf-8-sig', errors='replace')
    except Exception:
        return
    for line in text.splitlines()[1:]:   # skip header row
        if not line.strip():
            continue
        for cell in _csv_fields(line)[1:]:   # skip key column
            if _translatable(cell):
                out.add(cell)


def _apply_csv(data: bytes, cache: dict) -> bytes:
    try:
        text = data.decode('utf-8-sig', errors='replace')
    except Exception:
        return data
    lines = text.splitlines(keepends=True)
    if not lines:
        return data
    result = [lines[0]]
    for line in lines[1:]:
        stripped = line.rstrip('\r\n')
        ending   = line[len(stripped):]
        if not stripped.strip():
            result.append(line)
            continue
        cells = _csv_fields(stripped)
        if len(cells) < 2:
            result.append(line)
            continue
        new_cells = [cells[0]]  # key: never touch
        for cell in cells[1:]:
            tr = cache.get(cell)
            new_cells.append(tr if (tr and tr != cell) else cell)
        result.append(','.join(_csv_quote(c) for c in new_cells) + ending)
    new_text = ''.join(result)
    return new_text.encode('utf-8') if new_text != text.lstrip('﻿') else data


# ---------------------------------------------------------------------------
# .json — only string values (keys are never sent to translation)
# ---------------------------------------------------------------------------

def _json_extract(obj: object, out: set) -> None:
    if isinstance(obj, str):
        if _translatable(obj):
            out.add(obj)
    elif isinstance(obj, list):
        for item in obj:
            _json_extract(item, out)
    elif isinstance(obj, dict):
        for v in obj.values():   # keys: identifiers, skip
            _json_extract(v, out)


def _json_apply(obj: object, cache: dict) -> object:
    if isinstance(obj, str):
        return cache.get(obj, obj)
    if isinstance(obj, list):
        return [_json_apply(i, cache) for i in obj]
    if isinstance(obj, dict):
        return {k: _json_apply(v, cache) for k, v in obj.items()}
    return obj


def _extract_json(data: bytes, out: set) -> None:
    try:
        _json_extract(json.loads(data.decode('utf-8-sig', errors='replace')), out)
    except Exception:
        pass


def _apply_json(data: bytes, cache: dict) -> bytes:
    try:
        text = data.decode('utf-8-sig', errors='replace')
        obj  = json.loads(text)
        new  = _json_apply(obj, cache)
        return json.dumps(new, ensure_ascii=False, indent=2).encode('utf-8') if new != obj else data
    except Exception:
        return data


# ---------------------------------------------------------------------------
# .po gettext — msgstr values only (msgid are keys, preserved)
# ---------------------------------------------------------------------------

_PO_MSGID_RE = re.compile(r'^msgid\s+"((?:[^"\\]|\\.)*)"', re.MULTILINE)


def _extract_po(data: bytes, out: set) -> None:
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return
    for m in _PO_MSGID_RE.finditer(text):
        s = m.group(1).replace('\\n', '\n').replace('\\"', '"')
        if _translatable(s):
            out.add(s)


def _apply_po(data: bytes, cache: dict) -> bytes:
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return data
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        mid = re.match(r'^msgid\s+"((?:[^"\\]|\\.)*)"', line.rstrip())
        if mid:
            msgid = mid.group(1).replace('\\n', '\n').replace('\\"', '"')
            result.append(line)
            i += 1
            while i < len(lines) and not lines[i].strip():
                result.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].lstrip().startswith('msgstr'):
                ms = lines[i]
                tr = cache.get(msgid)
                if tr and tr != msgid:
                    esc = tr.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
                    ms = re.sub(r'^(\s*msgstr\s+)"[^"\\]*(?:\\.[^"\\]*)*"',
                                rf'\1"{esc}"', ms)
                result.append(ms)
                i += 1
            continue
        result.append(line)
        i += 1
    new_text = ''.join(result)
    return new_text.encode('utf-8') if new_text != text else data


# ---------------------------------------------------------------------------
# Dispatch table
# .scn / .res  — binary resource string tables hold property NAMES, not user text.
#                Modifying them corrupts scene loading. Skipped.
# .dtl / .dch  — Dialogic 2: encrypted in Godot 4 exports. Skipped.
# .gdc         — compiled GDScript bytecode: not decompilable here. Skipped.
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, tuple] = {
    '.gd':   (_extract_gd,   _apply_gd),
    '.tscn': (_extract_tscn, _apply_tscn),
    '.tres': (_extract_tscn, _apply_tscn),
    '.csv':  (_extract_csv,  _apply_csv),
    '.json': (_extract_json, _apply_json),
    '.po':   (_extract_po,   _apply_po),
}
_TEXT_EXTS = frozenset(_HANDLERS)

_SKIP_EXTS = frozenset({'.scn', '.res', '.dtl', '.dch', '.gdc'})


# ---------------------------------------------------------------------------
# Cache helper
# ---------------------------------------------------------------------------

def _save_cache(cache_file: Path, cache: dict) -> None:
    tmp = cache_file.with_suffix('.tmp')
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(tmp), str(cache_file))


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

class GodotTranslator(BaseTranslator):
    """
    Godot 3/4 game translator. Reads and rewrites standalone .pck files.

    Text formats handled (key/value isolation enforced):
      .gd   — only tr() / atr() string literals
      .tscn / .tres — only known text-bearing properties (text, tooltip_text, …)
      .csv  — key column preserved; value columns translated
      .json — only string values (keys never sent to AI)
      .po   — msgstr values only; msgid preserved as keys

    Skipped (binary / encrypted / bytecode):
      .scn .res .dtl .dch .gdc
    """

    def _find_pck(self) -> Path | None:
        for p in self.path.rglob('*.pck'):
            return p
        return None

    def translate(self) -> Path:
        pck_path = self._find_pck()
        if not pck_path:
            raise RuntimeError(
                "Nenhum arquivo .pck encontrado.\n"
                "Selecione a pasta raiz do jogo Godot (mesma pasta do .exe)."
            )

        ensure_model(self.src_lang, self.tgt_lang, self.log, engine=self.engine)

        cache_file = self.path / 'traducoes_godot.json'
        cache: dict[str, str] = {}
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text(encoding='utf-8'))
                self.log(f"Cache: {sum(1 for v in cache.values() if v)} entradas.")
            except Exception:
                pass

        glossary = load_glossary(force=True)
        if glossary:
            self.log(f"Glossario: {len(glossary)} termos.")

        # ── 1: Parse PCK ──────────────────────────────────────────────────────
        size_mb = pck_path.stat().st_size // 1_048_576
        self.log(f"\n[1/4] Lendo PCK: {pck_path.name} ({size_mb} MB)...")
        self.set_progress(0.03, 'Lendo PCK...')
        raw = pck_path.read_bytes()
        try:
            info = _parse_pck(raw)
        except Exception as e:
            raise RuntimeError(f"Falha ao ler PCK: {e}")

        gv    = info['godot_ver']
        files = info['files']
        self.log(f"  Godot {gv[0]}.{gv[1]}.{gv[2]} | v{info['version']} | {len(files)} arquivos")

        text_files = [f for f in files
                      if Path(f['path']).suffix.lower() in _TEXT_EXTS and f['data']]
        skipped = {Path(f['path']).suffix.lower() for f in files
                   if Path(f['path']).suffix.lower() in _SKIP_EXTS}
        if skipped:
            self.log(f"  Pulados (binario/criptografado): {', '.join(sorted(skipped))}")
        self.log(f"  Arquivos de texto: {len(text_files)}")

        if not text_files:
            self.log(
                "\nAviso: nenhum arquivo de texto encontrado no PCK.\n"
                "Jogos Godot 4 exportados compilam .tscn→.scn e .gd→.gdc.\n"
                "Tradução de recursos binários não é suportada (risco de corrupção)."
            )
            self.set_progress(1.0, 'Nenhum arquivo traduzivel.')
            return pck_path

        # ── 2: Extract strings ─────────────────────────────────────────────────
        self.log('\n[2/4] Extraindo strings...')
        all_strings: set[str] = set()
        n = max(len(text_files), 1)

        for i, f in enumerate(text_files):
            ext = Path(f['path']).suffix.lower()
            self.set_progress(0.08 + 0.12 * i / n, f"Texto: {Path(f['path']).name}")
            _HANDLERS[ext][0](f['data'], all_strings)

        to_translate = [s for s in sorted(all_strings) if not cache.get(s)]
        self.log(f"  {len(all_strings)} strings | cache: {len(cache)} | a traduzir: {len(to_translate)}")

        # ── 3: Translate ───────────────────────────────────────────────────────
        if to_translate:
            self.log(f"\n[3/4] Traduzindo {len(to_translate)} strings...")
            batch = 40
            total = len(to_translate)
            for i in range(0, total, batch):
                chunk = to_translate[i: i + batch]
                shield_data: list[tuple[list[str], list[str]]] = []
                masked: list[str] = []
                for s in chunk:
                    s1, vtoks = _shield_vars(s)        # BBCode + printf + {named} + <tags>
                    s2, gloss  = mask_glossary(s1, glossary)
                    masked.append(s2)
                    shield_data.append((vtoks, gloss))
                raw_tr = translate_texts(masked, self.src_lang, self.tgt_lang, engine=self.engine)
                translated: list[str] = []
                for t, (vtoks, gloss) in zip(raw_tr, shield_data):
                    t = unmask_glossary(t, gloss)
                    t = _unshield_vars(t, vtoks)
                    translated.append(t)
                cache.update(zip(chunk, translated))
                done = min(i + batch, total)
                if done % 500 < batch or done >= total:
                    _save_cache(cache_file, cache)
                self.set_progress(0.20 + 0.60 * done / total, f"Traduzindo... {done}/{total}")
                self.log(f"  {done}/{total}")
        else:
            self.log('\n[3/4] Tudo no cache.')

        # ── 4: Repack PCK ──────────────────────────────────────────────────────
        self.log('\n[4/4] Reempacotando PCK...')
        self.set_progress(0.82, 'Aplicando...')

        bak = pck_path.with_suffix('.pck.bak')
        if not bak.exists():
            import shutil
            self.log(f"  Backup: {bak.name}")
            shutil.copy2(pck_path, bak)

        changed = 0
        for f in text_files:
            ext = Path(f['path']).suffix.lower()
            new = _HANDLERS[ext][1](f['data'], cache)
            if new != f['data']:
                f['data'] = new
                changed += 1

        self.log(f"  {changed} arquivos modificados.")
        self.set_progress(0.94, 'Escrevendo PCK...')

        new_pck = _write_pck(info)
        tmp = pck_path.with_suffix('.pck.tmp')
        tmp.write_bytes(new_pck)
        os.replace(str(tmp), str(pck_path))

        _save_cache(cache_file, cache)
        self.set_progress(1.0, 'Concluido!')
        self.log(f"\nConcluido! Cache em: {cache_file}")
        return pck_path
