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
    mask_code_vars, unmask_code_vars,
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

_PCK_MAGIC      = 0x43504447   # "GDPC" little-endian
_PACK_REL_FILEBASE = 1          # pack_flags bit: offsets relative to file_base
_V1_RESERVED    = 16
_V2_RESERVED    = 64


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
# String extraction — text-format files (.gd, .tscn, .tres, .csv, .json)
# ---------------------------------------------------------------------------

_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.){2,})"')
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


def _extract_text_file(data: bytes, out: set) -> None:
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return
    # CSV: each cell can be a translatable string
    if text.lstrip().startswith('"') or ',' in text[:80]:
        for line in text.splitlines():
            for cell in line.split(','):
                s = cell.strip().strip('"')
                if _translatable(s):
                    out.add(s)
    # All quoted strings
    for m in _QUOTED_RE.finditer(text):
        s = m.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        if _translatable(s):
            out.add(s)


def _apply_text_file(data: bytes, cache: dict) -> bytes:
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        return data

    def _repl(m: re.Match) -> str:
        raw = m.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        tr  = cache.get(raw)
        if tr and tr != raw:
            esc = tr.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')
            return f'"{esc}"'
        return m.group(0)

    new_text = _QUOTED_RE.sub(_repl, text)
    return new_text.encode('utf-8') if new_text != text else data


# ---------------------------------------------------------------------------
# String extraction — Godot binary resources (.scn, .res, .translation)
# ---------------------------------------------------------------------------

_RSRC_MAGIC = b'RSRC'
_RSCC_MAGIC = b'RSCC'


def _extract_binary_resource(data: bytes, out: set) -> None:
    if len(data) < 16 or data[:4] not in (_RSRC_MAGIC, _RSCC_MAGIC):
        _brute_scan(data, out)
        return
    try:
        buf = io.BytesIO(data)
        buf.read(24)  # magic + endian + real64 + major + minor + format
        major = struct.unpack_from('<I', data, 12)[0]
        # resource type string
        tlen = struct.unpack('<I', buf.read(4))[0]
        if tlen > 512:
            raise ValueError('type too large')
        buf.read(tlen)
        if major >= 4:
            buf.read(40)  # uid + 4 × uint64 reserved
        st_size = struct.unpack('<I', buf.read(4))[0]
        if st_size > 200_000:
            raise ValueError('string table too large')
        for _ in range(st_size):
            slen = struct.unpack('<I', buf.read(4))[0]
            if slen > 8192:
                buf.read(min(slen, 8192))
                continue
            raw = buf.read(slen)
            try:
                s = raw.decode('utf-8')
                if _translatable(s):
                    out.add(s)
            except Exception:
                pass
    except Exception:
        _brute_scan(data, out)


def _apply_binary_resource(data: bytes, cache: dict) -> bytes:
    if len(data) < 16 or data[:4] not in (_RSRC_MAGIC, _RSCC_MAGIC):
        return data
    try:
        buf = io.BytesIO(data)
        hdr = bytearray()
        hdr += buf.read(24)
        major = struct.unpack_from('<I', data, 12)[0]
        tlen = struct.unpack('<I', buf.read(4))[0]
        if tlen > 512:
            return data
        tdata = buf.read(tlen)
        hdr += struct.pack('<I', tlen) + tdata
        if major >= 4:
            hdr += buf.read(40)
        st_size = struct.unpack('<I', buf.read(4))[0]
        if st_size > 200_000:
            return data
        old_entries = []
        for _ in range(st_size):
            slen = struct.unpack('<I', buf.read(4))[0]
            raw  = buf.read(min(slen, 8192)) if slen <= 8192 else b''
            old_entries.append(raw)
        changed   = False
        new_entries = []
        for raw in old_entries:
            try:
                s  = raw.decode('utf-8')
                tr = cache.get(s)
                if tr and tr != s:
                    new_entries.append(tr.encode('utf-8'))
                    changed = True
                    continue
            except Exception:
                pass
            new_entries.append(raw)
        if not changed:
            return data
        out = bytearray(hdr)
        out += struct.pack('<I', st_size)
        for raw in new_entries:
            out += struct.pack('<I', len(raw)) + raw
        out += buf.read()
        return bytes(out)
    except Exception:
        return data


def _brute_scan(data: bytes, out: set, min_n: int = 3, max_n: int = 512) -> None:
    i = 0
    dlen = len(data)
    while i < dlen - 4:
        n = struct.unpack_from('<I', data, i)[0]
        if min_n <= n <= max_n and i + 4 + n <= dlen:
            raw = data[i + 4: i + 4 + n]
            try:
                s = raw.decode('utf-8')
                if _translatable(s):
                    out.add(s)
            except Exception:
                pass
        i += 1


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

_TEXT_EXTS   = frozenset({'.gd', '.tscn', '.tres', '.csv', '.txt', '.json', '.po',
                          '.dtl', '.dch'})   # Dialogic 2 timeline/character files
_BINARY_EXTS = frozenset({'.scn', '.res', '.translation'})


class GodotTranslator(BaseTranslator):
    """
    Godot Engine 3/4 game translator.
    Reads and rewrites .pck files (standalone exports).
    Handles text-format scripts/scenes and binary resource string tables.
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

        # ── 1: Parse PCK ────────────────────────────────────────────────────
        size_mb = pck_path.stat().st_size // 1_048_576
        self.log(f"\n[1/4] Lendo PCK: {pck_path.name} ({size_mb} MB)...")
        self.set_progress(0.03, 'Lendo PCK...')
        raw = pck_path.read_bytes()
        try:
            info = _parse_pck(raw)
        except Exception as e:
            raise RuntimeError(f"Falha ao ler PCK: {e}")

        gv = info['godot_ver']
        files = info['files']
        self.log(f"  Godot {gv[0]}.{gv[1]}.{gv[2]} | v{info['version']} | {len(files)} arquivos")

        text_files = [f for f in files if Path(f['path']).suffix.lower() in _TEXT_EXTS   and f['data']]
        bin_files  = [f for f in files if Path(f['path']).suffix.lower() in _BINARY_EXTS and f['data']]
        self.log(f"  Texto: {len(text_files)} | Binario: {len(bin_files)}")

        # ── 2: Extract strings ───────────────────────────────────────────────
        self.log('\n[2/4] Extraindo strings...')
        all_strings: set[str] = set()
        n = max(len(text_files) + len(bin_files), 1)

        for i, f in enumerate(text_files):
            self.set_progress(0.08 + 0.12 * i / n, f"Texto: {Path(f['path']).name}")
            _extract_text_file(f['data'], all_strings)

        for i, f in enumerate(bin_files):
            self.set_progress(0.08 + 0.12 * (len(text_files) + i) / n,
                              f"Binario: {Path(f['path']).name}")
            _extract_binary_resource(f['data'], all_strings)

        to_translate = [s for s in sorted(all_strings) if not cache.get(s)]
        self.log(f"  {len(all_strings)} strings | cache: {len(cache)} | a traduzir: {len(to_translate)}")

        # ── 3: Translate ─────────────────────────────────────────────────────
        if to_translate:
            self.log(f"\n[3/4] Traduzindo {len(to_translate)} strings...")
            batch = 40
            total = len(to_translate)
            for i in range(0, total, batch):
                chunk = to_translate[i: i + batch]
                masked, tags_list, gloss_list = [], [], []
                for s in chunk:
                    m, tags  = mask_code_vars(s)
                    m, gloss = mask_glossary(m, glossary)
                    masked.append(m)
                    tags_list.append(tags)
                    gloss_list.append(gloss)
                translated = translate_texts(masked, self.src_lang, self.tgt_lang, engine=self.engine)
                translated = [
                    unmask_code_vars(unmask_glossary(t, g), tags)
                    for t, g, tags in zip(translated, gloss_list, tags_list)
                ]
                cache.update(zip(chunk, translated))
                done = min(i + batch, total)
                if done % 500 < batch or done >= total:
                    _save_cache(cache_file, cache)
                self.set_progress(0.20 + 0.60 * done / total, f"Traduzindo... {done}/{total}")
                self.log(f"  {done}/{total}")
        else:
            self.log('\n[3/4] Tudo no cache.')

        # ── 4: Repack ────────────────────────────────────────────────────────
        self.log('\n[4/4] Reempacotando PCK...')
        self.set_progress(0.82, 'Aplicando...')

        bak = pck_path.with_suffix('.pck.bak')
        if not bak.exists():
            import shutil
            self.log(f"  Backup: {bak.name}")
            shutil.copy2(pck_path, bak)

        changed = 0
        for f in text_files:
            new = _apply_text_file(f['data'], cache)
            if new != f['data']:
                f['data'] = new
                changed += 1
        for f in bin_files:
            new = _apply_binary_resource(f['data'], cache)
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
