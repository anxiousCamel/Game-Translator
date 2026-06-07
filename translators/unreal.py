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
# Unreal PAK footer constants
# ---------------------------------------------------------------------------
_PAK_MAGIC   = 0x5A6F12E1
_PAK_MAGIC_B = struct.pack('<I', _PAK_MAGIC)

# ---------------------------------------------------------------------------
# FString read/write helpers
# ---------------------------------------------------------------------------

def _read_fstring(buf: io.BytesIO) -> str:
    length = struct.unpack('<i', buf.read(4))[0]
    if length == 0:
        return ''
    if length > 0:
        raw = buf.read(length)
        return raw[:-1].decode('utf-8', errors='replace')   # drops null terminator
    else:
        count = -length  # number of UTF-16LE code units (includes null)
        raw   = buf.read(count * 2)
        return raw[:-2].decode('utf-16-le', errors='replace')


def _write_fstring(s: str) -> bytes:
    if not s:
        return struct.pack('<i', 0)
    try:
        enc = (s + '\x00').encode('utf-8')
        return struct.pack('<i', len(enc)) + enc
    except UnicodeEncodeError:
        enc = (s + '\x00').encode('utf-16-le')
        return struct.pack('<i', -(len(enc) // 2)) + enc


# ---------------------------------------------------------------------------
# locres binary format
# ---------------------------------------------------------------------------
# Magic:   uint64 = 0x7574283AA86585D3
# Version: uint8
# Then namespace/key/value triplets (layout varies by version).

_LOCRES_MAGIC = 0x7574283AA86585D3


def _parse_locres(data: bytes) -> tuple[list, int] | None:
    """
    Parse a .locres file.
    Returns (entries, version) where entries = list of (namespace, key, value) tuples,
    or None on failure.
    """
    if len(data) < 9:
        return None
    buf = io.BytesIO(data)
    try:
        magic = struct.unpack('<Q', buf.read(8))[0]
        if magic != _LOCRES_MAGIC:
            return None
        version = struct.unpack('B', buf.read(1))[0]
    except Exception:
        return None

    entries: list[tuple[str, str, str]] = []

    try:
        if version == 0:
            # Pre-4.21: simple namespace → keys → values
            ns_count = struct.unpack('<I', buf.read(4))[0]
            for _ in range(ns_count):
                ns       = _read_fstring(buf)
                key_cnt  = struct.unpack('<I', buf.read(4))[0]
                for _ in range(key_cnt):
                    key = _read_fstring(buf)
                    val = _read_fstring(buf)
                    entries.append((ns, key, val))

        elif version in (1, 2, 3):
            # 4.21+: shared string array + hash table
            str_count = struct.unpack('<I', buf.read(4))[0]
            strings   = [_read_fstring(buf) for _ in range(str_count)]

            ns_count = struct.unpack('<I', buf.read(4))[0]
            for _ in range(ns_count):
                ns      = _read_fstring(buf)
                key_cnt = struct.unpack('<I', buf.read(4))[0]
                for _ in range(key_cnt):
                    key    = _read_fstring(buf)
                    _hash  = buf.read(4)  # uint32 string hash (discard)
                    si     = struct.unpack('<I', buf.read(4))[0]
                    val    = strings[si] if si < len(strings) else ''
                    entries.append((ns, key, val))

        else:
            # Unknown version — attempt v0 fallback
            buf.seek(9)
            ns_count = struct.unpack('<I', buf.read(4))[0]
            if ns_count > 50_000:
                return None
            for _ in range(ns_count):
                ns      = _read_fstring(buf)
                key_cnt = struct.unpack('<I', buf.read(4))[0]
                if key_cnt > 100_000:
                    break
                for _ in range(key_cnt):
                    key = _read_fstring(buf)
                    val = _read_fstring(buf)
                    entries.append((ns, key, val))

    except Exception:
        pass  # return whatever we collected

    return entries, version


def _write_locres(entries: list, version: int) -> bytes:
    """Re-encode a .locres file from (namespace, key, value) tuples."""
    buf = bytearray()
    buf += struct.pack('<Q', _LOCRES_MAGIC)
    buf += struct.pack('B', version)

    if version == 0:
        # Group by namespace
        from collections import defaultdict
        ns_map: dict[str, list] = defaultdict(list)
        for ns, key, val in entries:
            ns_map[ns].append((key, val))
        buf += struct.pack('<I', len(ns_map))
        for ns, pairs in ns_map.items():
            buf += _write_fstring(ns)
            buf += struct.pack('<I', len(pairs))
            for key, val in pairs:
                buf += _write_fstring(key)
                buf += _write_fstring(val)

    else:
        # Build shared string array
        strings    = []
        str_idx: dict[str, int] = {}
        for _, _, val in entries:
            if val not in str_idx:
                str_idx[val] = len(strings)
                strings.append(val)

        buf += struct.pack('<I', len(strings))
        for s in strings:
            buf += _write_fstring(s)

        from collections import defaultdict
        ns_map: dict[str, list] = defaultdict(list)
        for ns, key, val in entries:
            ns_map[ns].append((key, val))

        buf += struct.pack('<I', len(ns_map))
        for ns, pairs in ns_map.items():
            buf += _write_fstring(ns)
            buf += struct.pack('<I', len(pairs))
            for key, val in pairs:
                buf += _write_fstring(key)
                buf += struct.pack('<I', 0)               # placeholder hash
                buf += struct.pack('<I', str_idx.get(val, 0))

    return bytes(buf)


# ---------------------------------------------------------------------------
# PAK reading
# ---------------------------------------------------------------------------

def _find_pak_footer(data: bytes) -> int | None:
    """Return byte offset of PAK magic in data (search backward from end)."""
    idx = data.rfind(_PAK_MAGIC_B, max(0, len(data) - 2048))
    return idx if idx >= 0 else None


def _parse_pak_footer(data: bytes, magic_ofs: int) -> dict | None:
    """Parse PAK footer starting at magic_ofs. Returns dict or None."""
    buf = io.BytesIO(data[magic_ofs:])
    try:
        _magic   = struct.unpack('<I', buf.read(4))[0]
        version  = struct.unpack('<I', buf.read(4))[0]
        idx_ofs  = struct.unpack('<Q', buf.read(8))[0]
        idx_size = struct.unpack('<Q', buf.read(8))[0]
        idx_hash = buf.read(20)
        encrypted_index = 0
        enc_key_guid    = b'\x00' * 16
        if version >= 3:
            encrypted_index = struct.unpack('B', buf.read(1))[0]
        if version >= 7:
            enc_key_guid = buf.read(16)
        compression_methods = []
        if version >= 8:
            method_count = struct.unpack('<I', buf.read(4))[0]
            for _ in range(min(method_count, 16)):
                raw = buf.read(32)
                name = raw.rstrip(b'\x00').decode('ascii', errors='ignore')
                compression_methods.append(name)
        return {
            'version': version,
            'index_offset': idx_ofs,
            'index_size':   idx_size,
            'index_hash':   idx_hash,
            'encrypted':    encrypted_index,
            'enc_key_guid': enc_key_guid,
            'compression':  compression_methods,
            'footer_offset': magic_ofs,
            'footer_end':   magic_ofs + buf.tell(),
        }
    except Exception:
        return None


def _parse_pak_index_v1(buf: io.BytesIO) -> list[dict]:
    """Parse PAK index for v1-v8 (sequential entry list)."""
    mount_point = _read_fstring(buf)
    entry_count = struct.unpack('<I', buf.read(4))[0]
    entries     = []
    for _ in range(entry_count):
        try:
            path              = _read_fstring(buf)
            offset            = struct.unpack('<Q', buf.read(8))[0]
            compressed_size   = struct.unpack('<Q', buf.read(8))[0]
            uncompressed_size = struct.unpack('<Q', buf.read(8))[0]
            compression       = struct.unpack('<I', buf.read(4))[0]  # v4+
            sha1              = buf.read(20)
            encrypted         = False
            block_count       = struct.unpack('<I', buf.read(4))[0]
            blocks = []
            for _ in range(block_count):
                bs = struct.unpack('<Q', buf.read(8))[0]
                be = struct.unpack('<Q', buf.read(8))[0]
                blocks.append((bs, be))
            encrypted    = struct.unpack('B', buf.read(1))[0] != 0
            block_size   = struct.unpack('<I', buf.read(4))[0]
            entries.append({
                'path': path, 'offset': offset,
                'compressed_size': compressed_size,
                'uncompressed_size': uncompressed_size,
                'compression': compression, 'encrypted': encrypted,
                'blocks': blocks, 'block_size': block_size,
            })
        except Exception:
            break
    return entries


# ---------------------------------------------------------------------------
# Cache helper
# ---------------------------------------------------------------------------

def _save_cache(path: Path, cache: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Translatable string check (simple: has letters, not a code identifier)
# ---------------------------------------------------------------------------

_SKIP_UE = re.compile(r'^[A-Z_][A-Z0-9_]{2,}$|^[a-z][A-Za-z0-9_]{3,}$|^\d|^/Game/')


def _translatable(s: str) -> bool:
    s = s.strip()
    if len(s) < 3:
        return False
    if sum(c.isalpha() for c in s) < 2:
        return False
    if _SKIP_UE.search(s):
        return False
    return True


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

class UnrealTranslator(BaseTranslator):
    """
    Unreal Engine game translator.

    Reads .pak archives, extracts .locres localization resources,
    translates text values, and rebuilds the .pak.

    Supports PAK versions 4-8 (UE 4.x). Encrypted PAKs require external
    unpacking first (use UnrealPak.exe -extract).
    """

    def _find_pak(self) -> list[Path]:
        return [p for p in self.path.rglob('*.pak') if p.stat().st_size > 1024]

    def translate(self) -> Path:
        pak_files = self._find_pak()
        if not pak_files:
            raise RuntimeError(
                "Nenhum arquivo .pak encontrado.\n"
                "Selecione a pasta raiz do jogo Unreal Engine."
            )

        ensure_model(self.src_lang, self.tgt_lang, self.log, engine=self.engine)

        cache_file = self.path / 'traducoes_unreal.json'
        cache: dict[str, str] = {}
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text(encoding='utf-8'))
                self.log(f"Cache: {sum(1 for v in cache.values() if v)} entradas.")
            except Exception:
                pass

        glossary = load_glossary(force=True)
        all_strings: set[str] = set()
        # Collect (pak_path, footer, entries, locres_entries)
        pak_data: list[dict] = []

        # ── 1: Parse PAKs ───────────────────────────────────────────────────
        for pak_path in pak_files:
            size_mb = pak_path.stat().st_size // 1_048_576
            self.log(f"\n[1/?] Lendo: {pak_path.name} ({size_mb} MB)...")
            self.set_progress(0.03, f'Lendo {pak_path.name}...')
            raw = pak_path.read_bytes()

            magic_ofs = _find_pak_footer(raw)
            if magic_ofs is None:
                self.log(f"  [aviso] Magic PAK nao encontrado em {pak_path.name}. Pulando.")
                continue

            footer = _parse_pak_footer(raw, magic_ofs)
            if not footer:
                self.log(f"  [aviso] Footer invalido em {pak_path.name}. Pulando.")
                continue

            self.log(f"  PAK v{footer['version']} | index@{footer['index_offset']}+{footer['index_size']}")

            if footer['encrypted']:
                self.log(f"  [aviso] Index criptografado — PAK precisa ser extraido com UnrealPak.exe primeiro.")
                continue

            # Read and parse index
            idx_start = footer['index_offset']
            idx_end   = idx_start + footer['index_size']
            if idx_end > len(raw):
                self.log(f"  [aviso] Index fora dos limites. Pulando.")
                continue

            idx_buf = io.BytesIO(raw[idx_start:idx_end])
            try:
                entries = _parse_pak_index_v1(idx_buf)
            except Exception as e:
                self.log(f"  [aviso] Falha ao parsear index: {e}. Pulando.")
                continue

            self.log(f"  {len(entries)} arquivos no PAK.")

            # Find .locres files
            locres_entries = [e for e in entries
                              if e['path'].lower().endswith('.locres')
                              and e['compression'] == 0
                              and not e['encrypted']]
            self.log(f"  {len(locres_entries)} arquivos .locres (sem compressao).")

            # Parse locres files
            locres_parsed: list[dict] = []
            for e in locres_entries:
                ofs  = e['offset']
                size = e['uncompressed_size']
                if ofs + size > len(raw):
                    continue
                lr_data = raw[ofs:ofs + size]
                result  = _parse_locres(lr_data)
                if not result:
                    continue
                lr_entries, lr_version = result
                self.log(f"    {Path(e['path']).name}: {len(lr_entries)} strings (locres v{lr_version})")
                for _, _, val in lr_entries:
                    if _translatable(val):
                        all_strings.add(val)
                locres_parsed.append({
                    'pak_entry': e,
                    'lr_entries': lr_entries,
                    'lr_version': lr_version,
                    'original_data': lr_data,
                })

            pak_data.append({
                'path': pak_path,
                'raw': raw,
                'footer': footer,
                'entries': entries,
                'locres': locres_parsed,
            })

        # Also scan for loose .locres files (extracted PAKs)
        for lr_path in self.path.rglob('*.locres'):
            try:
                lr_data = lr_path.read_bytes()
                result  = _parse_locres(lr_data)
                if result:
                    lr_entries, lr_version = result
                    self.log(f"  Loose {lr_path.name}: {len(lr_entries)} strings")
                    for _, _, val in lr_entries:
                        if _translatable(val):
                            all_strings.add(val)
                    pak_data.append({
                        'path': None,
                        'loose_path': lr_path,
                        'raw': None,
                        'locres': [{'pak_entry': None, 'lr_entries': lr_entries,
                                    'lr_version': lr_version, 'original_data': lr_data}],
                    })
            except Exception:
                pass

        to_translate = [s for s in sorted(all_strings) if not cache.get(s)]
        self.log(f"\n  Total: {len(all_strings)} strings | a traduzir: {len(to_translate)}")

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

        for pak_info in pak_data:
            pak_path = pak_info.get('path')
            raw      = pak_info.get('raw')

            for lr_info in pak_info['locres']:
                lr_entries = lr_info['lr_entries']
                lr_version = lr_info['lr_version']
                orig_data  = lr_info['original_data']

                # Apply translations to entries
                new_entries = []
                changed     = False
                for ns, key, val in lr_entries:
                    tr = cache.get(val)
                    if tr and tr != val:
                        new_entries.append((ns, key, tr))
                        changed = True
                    else:
                        new_entries.append((ns, key, val))

                if not changed:
                    continue

                new_locres = _write_locres(new_entries, lr_version)

                if pak_path is None:
                    # Loose .locres file
                    lr_path = lr_info.get('loose_path') or pak_info.get('loose_path')
                    if lr_path:
                        bak = lr_path.with_suffix('.locres.bak')
                        if not bak.exists():
                            bak.write_bytes(orig_data)
                        tmp = lr_path.with_suffix('.locres.tmp')
                        tmp.write_bytes(new_locres)
                        os.replace(str(tmp), str(lr_path))
                        self.log(f"  Modificado: {lr_path.name}")
                    continue

                # Embedded in PAK: patch in-place (only if same or smaller size)
                pak_entry = lr_info['pak_entry']
                if pak_entry is None:
                    continue

                ofs    = pak_entry['offset']
                orig_sz = pak_entry['uncompressed_size']

                if len(new_locres) <= orig_sz:
                    # Pad with zeros to fill original slot
                    padded = new_locres + bytes(orig_sz - len(new_locres))
                    raw = raw[:ofs] + padded + raw[ofs + orig_sz:]
                    self.log(f"  Patch in-place: {Path(pak_entry['path']).name}")
                else:
                    self.log(
                        f"  [aviso] {Path(pak_entry['path']).name}: traducao maior que original "
                        f"({len(new_locres)} > {orig_sz} bytes) — pulando (use UnrealPak para rebuild)."
                    )

            if raw and pak_path and raw != pak_info['raw']:
                bak = pak_path.with_suffix('.pak.bak')
                if not bak.exists():
                    import shutil
                    self.log(f"  Backup: {bak.name}")
                    shutil.copy2(pak_path, bak)
                tmp = pak_path.with_suffix('.pak.tmp')
                tmp.write_bytes(raw)
                os.replace(str(tmp), str(pak_path))
                self.log(f"  PAK salvo: {pak_path.name}")

        _save_cache(cache_file, cache)
        self.set_progress(1.0, 'Concluido!')
        self.log(f"\nConcluido! Cache em: {cache_file}")
        return self.path
