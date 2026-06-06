from __future__ import annotations
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .base import BaseTranslator
from .engine import ensure_model, translate_texts

# ---------------------------------------------------------------------------
# Text filtering
# ---------------------------------------------------------------------------

_SKIP_MB_FIELDS = frozenset({
    # Unity internals — identity / references
    "m_Name", "name", "m_Script", "m_GameObject",
    "guid", "fileID", "type", "m_PathID", "m_FileID",
    "m_ObjectHideFlags", "m_Tag", "m_Layer",
    "m_ClassName", "m_Namespace", "m_AssemblyName",
    # Asset names / paths
    "m_Font", "m_FontName", "m_AtlasName", "m_SpriteName",
    "m_ShaderName", "m_SceneName", "m_SceneGUID",
    "m_Path", "m_AssetPath", "m_ResourcePath",
    # UnityEvent / button onClick — CRITICAL: method names break buttons if translated
    "m_MethodName", "m_TargetAssemblyTypeName",
    "m_StringArgument", "m_ObjectArgumentAssemblyTypeName",
    # Unity.Localization — table identifiers, locale codes
    "m_TableCollectionName", "m_TableCollectionNameGuid",
    "m_LocaleId", "m_Code", "m_Identifier", "m_CustomLocaleName",
    # Animation / state machine identifiers
    "m_StateMachineName", "m_DefaultState",
})

_GUID_RE       = re.compile(r'^[0-9a-f]{8,}$', re.ASCII)
_CAMEL_RE      = re.compile(r'^[a-z][a-zA-Z0-9]{2,}$')
_SCREAMING_RE  = re.compile(r'^[A-Z][A-Z0-9_]{2,}$')
_HEX_COLOR_RE  = re.compile(r'^#[0-9a-fA-F]{3,8}$')
_NUMBER_RE     = re.compile(r'^\d[\d.,\s%]*[a-zA-Z]?$')
# Unicode char-range strings like "20-7E,A1-AC,..." (font character maps)
_CHARRANGE_RE  = re.compile(r'^[0-9A-Fa-f]{2,4}(-[0-9A-Fa-f]{2,4})?(,[0-9A-Fa-f])')
# PascalCase compound identifier without spaces: "NewGame", "OnLoadGame", "MainMenuButton"
# Matches: uppercase letter, 1+ lowercase, then at least one more uppercase → compound
_PASCAL_ID_RE  = re.compile(r'^[A-Z][a-z]+(?:[A-Z][a-zA-Z0-9]*)+$')
# Common asset file extensions inside strings
_EXT_RE        = re.compile(r'\.(png|jpg|wav|mp3|ogg|prefab|unity|asset|mat|anim|controller|fbx|obj|ttf|otf|shader)(\b|$)', re.IGNORECASE)
# Unity / TextMeshPro Rich Text tags: <b>, <size=10>, <color=#FF0000>, </color>, etc.
_TAG_RE        = re.compile(r'<[^>]+>')

# Unity lifecycle / event handler names — never translatable
_UNITY_RESERVED = frozenset({
    "Awake", "Start", "Update", "FixedUpdate", "LateUpdate",
    "OnEnable", "OnDisable", "OnDestroy", "OnApplicationQuit",
    "OnTriggerEnter", "OnTriggerExit", "OnTriggerStay",
    "OnCollisionEnter", "OnCollisionExit", "OnCollisionStay",
    "OnClick", "OnValueChanged", "OnSubmit", "OnEndEdit",
    "GameObject", "Transform", "Component", "MonoBehaviour",
    "ScriptableObject", "UnityEvent", "UnityEngine",
    "Assembly-CSharp", "Assembly-CSharp-firstpass",
})

# Addressables catalog JSON — do not translate
_CATALOG_KEYS = frozenset({"m_LocatorId", "m_InstanceProviderData", "m_ProviderIds"})

# Known AI hallucination outputs — discard these translations and keep the original
_HALLUCINATIONS: frozenset[str] = frozenset({
    "não, não, não.",
    "não sei.",
    "o que é isso?",
    "o que é isso",
    "não sei",
    "no, no, no.",
    "i don't know.",
    "what is this?",
    "what is this",
    "i don't know",
})

# File names that must never be modified (Unity engine config, not game text)
_SKIP_FILENAMES = frozenset({
    "UnityServicesProjectConfiguration.json",
    "catalog.json",
    "settings.json",
    "network_diag_report.json",
})

# Unity.Localization bundle language name → ISO code
_LOCALE_BUNDLE_LANGS: dict[str, str] = {
    "english": "en",
    "japanese": "ja",
    "chinese": "zh",
    "korean": "ko",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "russian": "ru",
    "portuguese": "pt",
    "thai": "th",
    "arabic": "ar",
    "turkish": "tr",
    "polish": "pl",
    "dutch": "nl",
    "indonesian": "id",
    "vietnamese": "vi",
    "hungarian": "hu",
    "czech": "cs",
    "romanian": "ro",
    "ukrainian": "uk",
}

# Unity object types that never contain user-visible text
_SKIP_OBJ_TYPES = frozenset({
    "Mesh", "Texture2D", "Texture3D", "Cubemap", "RenderTexture",
    "Sprite", "AudioClip", "AudioMixer", "Shader", "ComputeShader",
    "AnimationClip", "Avatar", "RuntimeAnimatorController", "AnimatorController",
    "AnimatorStateMachine", "AnimatorState", "AnimatorTransition",
    "BlendTree", "StateMachineBehaviour",
    "Font", "Material", "PhysicMaterial", "LightmapSettings",
    "NavMeshData", "TerrainData", "VideoClip", "LightingDataAsset",
    "SpriteAtlas", "BuildSettings", "PlayerSettings", "QualitySettings",
    "InputManager", "TagManager", "PhysicsManager", "GraphicsSettings",
    "TimeManager", "DynamicsManager", "AudioManager",
    "GameObject", "Transform", "RectTransform",
    "MeshRenderer", "MeshFilter", "SkinnedMeshRenderer",
    "BoxCollider", "SphereCollider", "CapsuleCollider", "MeshCollider",
    "BoxCollider2D", "CircleCollider2D", "PolygonCollider2D",
    "Rigidbody", "Rigidbody2D", "CharacterController",
    "Camera", "Light", "LensFlare", "Projector",
    "Canvas", "CanvasRenderer", "CanvasScaler", "CanvasGroup",
    "EventSystem", "GraphicRaycaster", "StandaloneInputModule",
    "Image", "RawImage", "Mask", "RectMask2D",
    "Animator", "Animation", "NavMeshAgent", "NavMeshObstacle",
    "ParticleSystem", "ParticleSystemRenderer",
    "LineRenderer", "TrailRenderer",
    "AudioSource", "AudioListener", "AudioReverbZone",
    "AssetBundle", "AssetBundleManifest",
    "Terrain", "WindZone", "OcclusionCullingSettings",
})

_UNITY_EXTS = frozenset({".assets", ".unity3d", ".bundle"})
_TEXT_EXTS  = frozenset({".json", ".csv", ".txt"})


def _mask_tags(s: str) -> tuple[str, list[str]]:
    """Replace <...> tags with [T0], [T1],... tokens. Returns (masked_str, original_tags)."""
    tags: list[str] = []

    def _replacer(m: re.Match) -> str:
        tags.append(m.group(0))
        return f"[T{len(tags) - 1}]"

    return _TAG_RE.sub(_replacer, s), tags


def _unmask_tags(s: str, tags: list[str]) -> str:
    """Restore [T0], [T1],... tokens back to their original tags."""
    for idx, tag in enumerate(tags):
        s = s.replace(f"[T{idx}]", tag)
    return s


def _looks_translatable(s: str) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 3:
        return False
    # Strip Rich Text tags and require at least 2 visible alphabetic characters.
    # Prevents "E", "<size=10>F</size>" etc. from being sent for translation.
    visible = _TAG_RE.sub("", s).strip()
    if sum(1 for c in visible if c.isalpha()) < 2:
        return False
    if not any(c.isalpha() for c in s):
        return False

    # Fast-pass: newline = real dialogue, never an identifier.
    if "\n" in s or "\r" in s:
        return True

    # Normalize whitespace for visual/identifier checks.
    # Multi-line strings like "Load\nGame" look like "Load Game" to the rules,
    # so they pass space-based tests and don't falsely match code patterns.
    # The caller always stores the ORIGINAL string — this variable is check-only.
    check = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")

    # --- Hard identifier patterns (run on `check` = newlines→spaces) ---
    if _GUID_RE.match(check):
        return False
    if _CAMEL_RE.match(check) or _SCREAMING_RE.match(check):
        return False
    if _HEX_COLOR_RE.match(check) or _NUMBER_RE.match(check):
        return False
    if _CHARRANGE_RE.match(check):
        return False

    # Underscore identifiers: block if original string (before normalization) has underscore.
    # Real human dialogue never has underscores; "L_arm\nR_arm" must still be blocked.
    if "_" in s:
        return False

    # File extensions (Unity Rich Text tags like </color> contain / — don't block slashes)
    if _EXT_RE.search(check):
        return False

    # Known non-translatable prefixes
    if check.startswith(("http", "Assets/", "assets/", "Packages/", "@")):
        return False

    # Method call: no whitespace + parens = code identifier
    if "(" in check and ")" in check and " " not in check:
        return False

    # Assembly-qualified type name
    if ", Assembly-" in check or check.endswith(", Assembly-CSharp"):
        return False

    # Unity reserved lifecycle / event names
    if check in _UNITY_RESERVED:
        return False

    # PascalCase compound without whitespace: "OnNewGame", "LoadScene", "MainMenu"
    if " " not in check and _PASCAL_ID_RE.match(check):
        return False

    return True


_BLANK_LINE_RE = re.compile(r"(?:\r?\n){2,}")  # two+ newlines = paragraph boundary


def _normalize_nl(s: str) -> str:
    """Normalize line endings to \\n. Used as cache key so \\r\\n == \\n never mismatches."""
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _apply_text_blocks(raw: str, cache: dict) -> tuple[str, bool]:
    """
    Apply cache translations to a plain-text string.

    Strategy (mirrors _collect_text_blocks priority):
    1. Try to replace each paragraph block (multi-line) as a whole.
    2. For paragraphs not found in cache, fall back to line-by-line replacement.

    Returns (new_text, changed).
    """
    raw_n = _normalize_nl(raw)
    parts = _BLANK_LINE_RE.split(raw_n)
    # Separators between parts (the blank-line sequences themselves)
    seps = _BLANK_LINE_RE.findall(raw_n)

    result = []
    changed = False
    for idx, block in enumerate(parts):
        key = block.strip()
        tr = cache.get(key) if key else None

        if tr and tr != key:
            # Whole block translated — preserve leading/trailing whitespace of original block
            leading  = block[: len(block) - len(block.lstrip())]
            trailing = block[len(block.rstrip()):]
            result.append(leading + tr + trailing)
            changed = True
        else:
            # Fall back to line-by-line within this block
            new_lines = []
            block_changed = False
            for line in block.splitlines(keepends=True):
                stripped = _normalize_nl(line.strip())
                line_tr = cache.get(stripped) if stripped else None
                if line_tr and line_tr != stripped:
                    new_lines.append(line.replace(line.strip(), line_tr, 1))
                    block_changed = True
                else:
                    new_lines.append(line)
            if block_changed:
                result.append("".join(new_lines))
                changed = True
            else:
                result.append(block)

        # Re-insert separator
        if idx < len(seps):
            result.append(seps[idx])

    return "".join(result), changed


def _collect_text_blocks(raw: str, out: set) -> None:
    """
    Collect translatable text from a plain-text string (TextAsset or file).

    Strategy:
    1. Try to collect multi-line dialogue blocks (paragraphs separated by blank lines).
       A paragraph preserves its internal \\n so the full block is one cache key.
    2. Also collect individual non-empty lines as fallback for single-line entries.

    All strings are \\r\\n-normalized before storage.
    """
    raw_n = _normalize_nl(raw)

    # Pass 1 — paragraph blocks (blank-line boundaries)
    for block in _BLANK_LINE_RE.split(raw_n):
        block = block.strip()
        if _looks_translatable(block):
            out.add(block)

    # Pass 2 — individual lines (handles single-line entries not part of paragraphs)
    for line in raw_n.splitlines():
        line = line.strip()
        if _looks_translatable(line):
            out.add(line)


def _collect(obj: Any, out: set) -> None:
    if isinstance(obj, str):
        s = _normalize_nl(obj)
        if _looks_translatable(s):
            out.add(s)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in _SKIP_MB_FIELDS:
                _collect(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect(item, out)


def _patch(obj: Any, cache: dict) -> Any:
    if isinstance(obj, str):
        # Normalize the lookup key so \r\n originals hit \n-normalized cache entries
        return cache.get(_normalize_nl(obj), obj)
    if isinstance(obj, dict):
        return {k: (v if k in _SKIP_MB_FIELDS else _patch(v, cache)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_patch(item, cache) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Unity.Localization StringTable native support
# ---------------------------------------------------------------------------

def _is_string_table(tree: dict) -> bool:
    """True if MonoBehaviour is a Unity.Localization StringTable."""
    return (
        isinstance(tree.get("m_TableData"), list)
        and len(tree["m_TableData"]) > 0
        and isinstance(tree["m_TableData"][0], dict)
        and "m_Localized" in tree["m_TableData"][0]
    )


def _collect_string_table(tree: dict, out: set) -> None:
    """Collect only m_Localized values from StringTable entries."""
    for entry in tree.get("m_TableData", []):
        val = _normalize_nl(entry.get("m_Localized", ""))
        if _looks_translatable(val):
            out.add(val)


def _patch_string_table(tree: dict, cache: dict) -> dict:
    """
    Patch StringTable safely: only replace m_Localized.
    Keys (m_Id), metadata, and table name are never touched.
    Returns a new tree dict (does not mutate original).
    """
    new_entries = []
    changed = False
    for entry in tree.get("m_TableData", []):
        orig = _normalize_nl(entry.get("m_Localized", ""))
        translated = cache.get(orig)
        if translated and translated != orig:
            new_entry = dict(entry)
            new_entry["m_Localized"] = translated
            new_entries.append(new_entry)
            changed = True
        else:
            new_entries.append(entry)
    if not changed:
        return tree
    new_tree = dict(tree)
    new_tree["m_TableData"] = new_entries
    return new_tree


# ---------------------------------------------------------------------------
# Unity version detection
# ---------------------------------------------------------------------------

def _detect_unity_version(env: Any) -> str | None:
    """
    Extract Unity version string from a loaded UnityPy Environment.
    Checks inner files (BundleFile wrapping SerializedFiles) and direct SerializedFiles.
    Returns e.g. "2021.3.45f2" or None.
    """
    for f in env.files.values():
        # Direct SerializedFile
        v = getattr(f, "unity_version", None)
        if v:
            return v
        # BundleFile wrapping inner SerializedFiles
        inner = getattr(f, "files", None)
        if inner:
            for inner_f in inner.values():
                v = getattr(inner_f, "unity_version", None)
                if v:
                    return v
    return None


# ---------------------------------------------------------------------------
# TypeTree generator (Mono + IL2CPP)
# ---------------------------------------------------------------------------

def _build_typetree_generator(game_root: Path, data_dir: Path, unity_version: str, log):
    """
    Build a TypeTreeGenerator for the game if TypeTreeGeneratorAPI is available.

    Detection:
    - IL2CPP: GameAssembly.dll present in game_root
    - Mono:   Managed/ folder present inside data_dir

    Returns a loaded TypeTreeGenerator or None (on missing dep or any error).
    """
    try:
        from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
        # Will raise ImportError inside __init__ if TypeTreeGeneratorAPI not installed
        gen = TypeTreeGenerator(unity_version)
    except ImportError:
        log("  [info] TypeTreeGeneratorAPI nao instalada — MonoBehaviours sem type tree.")
        return None
    except Exception as e:
        log(f"  [aviso] Nao foi possivel criar TypeTreeGenerator: {e}")
        return None

    # IL2CPP: GameAssembly.dll + global-metadata.dat
    ga_dll = game_root / "GameAssembly.dll"
    metadata = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    if ga_dll.exists() and metadata.exists():
        try:
            log("  Backend: IL2CPP — carregando GameAssembly.dll...")
            gen.load_il2cpp(ga_dll.read_bytes(), metadata.read_bytes())
            log("  IL2CPP: carregado.")
            return gen
        except Exception as e:
            log(f"  [aviso] Falha ao carregar IL2CPP assemblies: {e}")
            return None

    # Mono: Managed/ folder with .dll files
    managed = data_dir / "Managed"
    if managed.is_dir():
        dlls = list(managed.glob("*.dll"))
        if dlls:
            try:
                log(f"  Backend: Mono — carregando {len(dlls)} DLLs de Managed/...")
                gen.load_local_dll_folder(str(managed))
                log("  Mono: carregado.")
                return gen
            except Exception as e:
                log(f"  [aviso] Falha ao carregar Mono DLLs: {e}")
                return None

    log("  [info] Nenhum assembly encontrado (Managed/ e GameAssembly.dll ausentes).")
    return None


def _load_env_with_generator(path: Path, generator) -> Any:
    """Load a Unity file into an Environment with optional TypeTreeGenerator attached."""
    import UnityPy as unitypy
    env = unitypy.Environment()
    if generator is not None:
        env.typetree_generator = generator
    env.load_file(str(path))
    return env


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


class UnityTranslator(BaseTranslator):
    """
    Universal Unity game translator.

    Strategy (in priority order, best for each game type):
    1. Unity.Localization Addressable bundles  — structured, language-aware
    2. TextAsset objects (JSON/CSV/plain text) — most readable
    3. MonoBehaviour typetree fields           — needs TypeTreeGeneratorAPI for full coverage
    4. Plain text files in StreamingAssets

    TypeTreeGeneratorAPI (optional pip package) enables reading custom MonoBehaviour
    fields in Mono builds. IL2CPP and games without Managed/ degrade gracefully.
    """

    def _find_data_dir(self) -> Path | None:
        for item in self.path.iterdir():
            if item.is_dir() and item.name.endswith("_Data"):
                return item
        return None

    @staticmethod
    def _locale_bundle_lang(bundle: Path) -> str | None:
        """Return ISO code if bundle is a Unity.Localization string-table, else None."""
        name = bundle.stem.lower()
        if "localization-string-tables" not in name and "string-table" not in name:
            return None
        for lang_name, code in _LOCALE_BUNDLE_LANGS.items():
            if lang_name in name:
                return code
        return None

    def _iter_asset_files(self, data_dir: Path):
        """Yield (path, is_locale_bundle, bundle_lang) in priority order."""
        aa = data_dir / "StreamingAssets" / "aa"
        if aa.exists():
            for f in sorted(aa.rglob("*.bundle")):
                if "catalog" not in f.name.lower():
                    lang = self._locale_bundle_lang(f)
                    yield f, lang is not None, lang

        sa = data_dir / "StreamingAssets"
        if sa.exists():
            for pat in ("*.json", "*.csv", "*.txt"):
                for f in sa.rglob(pat):
                    if (
                        "aa" not in f.parts
                        and "catalog" not in f.name.lower()
                        and f.name not in _SKIP_FILENAMES
                        and "il2cpp_data" not in f.parts
                    ):
                        yield f, False, None

        for f in sorted(data_dir.glob("*.assets")):
            yield f, False, None

        main = data_dir / "data.unity3d"
        if main.exists():
            yield main, False, None

    # ── Extraction ─────────────────────────────────────────────────────────

    def _extract_unity_file(self, path: Path, out: set, generator=None) -> int:
        import UnityPy as unitypy
        before = len(out)
        try:
            env = _load_env_with_generator(path, generator)
        except Exception as e:
            self.log(f"    [aviso] {path.name}: {e}")
            return 0

        for i, obj in enumerate(env.objects):
            if i % 50 == 0:
                time.sleep(0.001)
            if obj.type.name in _SKIP_OBJ_TYPES:
                continue
            try:
                if obj.type.name == "TextAsset":
                    data = obj.read()
                    raw: str = getattr(data, "text", "") or ""
                    if not raw.strip():
                        continue
                    if raw.lstrip()[:1] in ("{", "["):
                        try:
                            parsed = json.loads(raw)
                            if isinstance(parsed, dict) and _CATALOG_KEYS & parsed.keys():
                                continue
                            _collect(parsed, out)
                            continue
                        except json.JSONDecodeError:
                            pass
                    _collect_text_blocks(raw, out)

                elif obj.type.name == "MonoBehaviour":
                    try:
                        tree = obj.read_typetree()
                        if _is_string_table(tree):
                            _collect_string_table(tree, out)
                        else:
                            _collect(tree, out)
                    except Exception:
                        pass
            except Exception:
                continue
        return len(out) - before

    def _extract_text_file(self, path: Path, out: set) -> int:
        before = len(out)
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return 0
        if path.suffix.lower() == ".json":
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and _CATALOG_KEYS & parsed.keys():
                    return 0
                _collect(parsed, out)
                return len(out) - before
            except json.JSONDecodeError:
                pass
        _collect_text_blocks(raw, out)
        return len(out) - before

    # ── Injection ──────────────────────────────────────────────────────────

    def _apply_unity_file(self, src: Path, dst: Path, cache: dict, generator=None) -> bool:
        """Load from src (backup), apply translations, write to dst (live game file)."""
        modified = False
        try:
            env = _load_env_with_generator(src, generator)
        except Exception:
            return False

        for i, obj in enumerate(env.objects):
            if i % 50 == 0:
                time.sleep(0.001)
            if obj.type.name in _SKIP_OBJ_TYPES:
                continue
            try:
                if obj.type.name == "TextAsset":
                    data = obj.read()
                    raw: str = getattr(data, "text", "") or ""
                    if not raw.strip():
                        continue
                    if raw.lstrip()[:1] in ("{", "["):
                        try:
                            parsed = json.loads(raw)
                            if isinstance(parsed, dict) and _CATALOG_KEYS & parsed.keys():
                                continue
                            new_parsed = _patch(parsed, cache)
                            new_raw = json.dumps(new_parsed, ensure_ascii=False, separators=(",", ":"))
                            if new_raw != raw:
                                data.text = new_raw
                                data.save()
                                modified = True
                            continue
                        except json.JSONDecodeError:
                            pass
                    new_text, ta_changed = _apply_text_blocks(raw, cache)
                    if ta_changed:
                        data.text = new_text
                        data.save()
                        modified = True

                elif obj.type.name == "MonoBehaviour":
                    try:
                        tree = obj.read_typetree()
                        if _is_string_table(tree):
                            new_tree = _patch_string_table(tree, cache)
                        else:
                            new_tree = _patch(tree, cache)
                        if new_tree is not tree:
                            obj.save_typetree(new_tree)
                            modified = True
                    except Exception:
                        pass
            except Exception:
                continue

        if modified:
            try:
                if not env.files:
                    return False
                file_obj = next(iter(env.files.values()))
                # Serialize BEFORE opening dst to avoid truncating it on save error
                raw_bytes = file_obj.save()
                with open(dst, "wb") as f:
                    f.write(raw_bytes)
            except Exception as e:
                self.log(f"    [erro] Nao foi possivel salvar {dst.name}: {e}")
                return False
        return modified

    def _apply_text_file(self, src: Path, dst: Path, cache: dict) -> bool:
        try:
            raw = src.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return False
        if src.suffix.lower() == ".json":
            try:
                parsed = json.loads(raw)
                new_parsed = _patch(parsed, cache)
                new_raw = json.dumps(new_parsed, ensure_ascii=False, indent=2)
                if new_raw != raw:
                    dst.write_text(new_raw, encoding="utf-8")
                    return True
                return False
            except json.JSONDecodeError:
                pass
        new_text, changed = _apply_text_blocks(raw, cache)
        if changed:
            dst.write_text(new_text, encoding="utf-8")
            return True
        return False

    # ── Backup helpers ─────────────────────────────────────────────────────

    def _backup_path(self, af: Path, data_dir: Path, backup_root: Path) -> Path:
        rel = af.relative_to(data_dir)
        return backup_root / rel

    def _ensure_backup(self, af: Path, data_dir: Path, backup_root: Path) -> Path:
        backup = self._backup_path(af, data_dir, backup_root)
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            size_mb = af.stat().st_size // (1024 * 1024)
            if size_mb >= 50:
                self.log(f"  Backup: {af.name} ({size_mb} MB)...")
            shutil.copy2(af, backup)
        return backup

    # ── Unity version probe ─────────────────────────────────────────────────

    def _probe_unity_version(self, asset_files_iter) -> str | None:
        """
        Try to detect the Unity version by loading the smallest available asset file.
        Returns version string or None.
        """
        import UnityPy as unitypy
        for af, _, _ in asset_files_iter:
            if af.suffix.lower() not in _UNITY_EXTS:
                continue
            try:
                env = unitypy.load(str(af))
                v = _detect_unity_version(env)
                if v:
                    return v
            except Exception:
                continue
        return None

    # ── Main pipeline ──────────────────────────────────────────────────────

    def translate(self) -> Path:
        try:
            import UnityPy as unitypy  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "Biblioteca UnityPy nao instalada.\n"
                "Execute: pip install UnityPy"
            )

        data_dir = self._find_data_dir()
        if not data_dir:
            raise RuntimeError(
                "Pasta *_Data nao encontrada. "
                "Selecione a pasta raiz do jogo (mesma pasta do .exe)."
            )

        ensure_model(self.src_lang, self.tgt_lang, self.log, engine=self.engine)

        backup_root = self.path / "_unity_backup"
        cache_file  = self.path / "traducoes_unity.json"

        cache: dict[str, str] = {}
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
                self.log(f"Cache carregado: {sum(1 for v in cache.values() if v)} traducoes.")
            except Exception:
                pass

        asset_entries = list(self._iter_asset_files(data_dir))
        self.log(f"Arquivos a processar: {len(asset_entries)}")

        # ── Build TypeTree generator ────────────────────────────────────────
        self.log("\nDetectando engine Unity...")
        unity_version = self._probe_unity_version(iter(asset_entries))
        if unity_version:
            self.log(f"  Versao Unity: {unity_version}")
        else:
            self.log("  Versao Unity: nao detectada")

        generator = None
        if unity_version:
            self.set_progress(0.03, "Carregando assemblies...")
            generator = _build_typetree_generator(self.path, data_dir, unity_version, self.log)
            if generator:
                self.log("  TypeTree generator pronto — MonoBehaviours acessiveis.")
            else:
                self.log("  Sem TypeTree generator — apenas TextAssets e bundles de localizacao.")
        else:
            self.log("  Sem TypeTree generator (versao desconhecida).")

        # ── Phase 1: Extract ────────────────────────────────────────────────
        self.log("\n[1/3] Extraindo texto dos assets...")
        all_strings: set[str] = set()
        for i, (af, is_locale, bundle_lang) in enumerate(asset_entries):
            self.set_progress(
                0.05 + 0.20 * i / max(len(asset_entries), 1),
                f"Extraindo: {af.name}",
            )
            if is_locale and bundle_lang is not None and bundle_lang != self.src_lang:
                self.log(f"  {af.name} — pulando (idioma {bundle_lang})")
                continue

            backup = self._backup_path(af, data_dir, backup_root)
            src = backup if backup.exists() else af
            size_kb = af.stat().st_size // 1024
            self.log(f"  {af.name} ({size_kb} KB)")
            if af.suffix.lower() in _UNITY_EXTS:
                n = self._extract_unity_file(src, all_strings, generator)
            else:
                n = self._extract_text_file(src, all_strings)
            if n:
                self.log(f"    +{n} strings")

        to_translate = [s for s in sorted(all_strings) if not cache.get(s)]
        self.log(
            f"\nTotal: {len(all_strings)} strings unicas | "
            f"cache: {len(cache)} | a traduzir: {len(to_translate)}"
        )

        # ── Phase 2: Translate ──────────────────────────────────────────────
        if to_translate:
            self.log(
                f"\n[2/3] Traduzindo {len(to_translate)} strings "
                f"({self.src_lang} -> {self.tgt_lang})..."
            )
            batch_size = 40
            total = len(to_translate)
            for i in range(0, total, batch_size):
                batch = to_translate[i : i + batch_size]

                # Step 1 — mask Rich Text tags so the model never sees <size=10> etc.
                masked_strs: list[str] = []
                tag_maps:    list[list[str]] = []
                for s in batch:
                    masked, tags = _mask_tags(s)
                    masked_strs.append(masked)
                    tag_maps.append(tags)

                # Step 2 — mask \n so the neural model doesn't destroy line breaks
                safe_batch = [
                    s.replace("\r\n", " <BR> ").replace("\n", " <BR> ").replace("\r", " <BR> ")
                    for s in masked_strs
                ]

                translated_safe = translate_texts(safe_batch, self.src_lang, self.tgt_lang, engine=self.engine)

                # Step 3 — restore \n
                translated_unmasked = [
                    t.replace(" <BR> ", "\n").replace("<BR>", "\n")
                    for t in translated_safe
                ]

                # Step 4 — restore Rich Text tags
                translated_tagged = [
                    _unmask_tags(t, tags)
                    for t, tags in zip(translated_unmasked, tag_maps)
                ]

                # Step 5 — anti-hallucination: discard known bad outputs
                translated = [
                    orig if t.strip().lower() in _HALLUCINATIONS else t
                    for orig, t in zip(batch, translated_tagged)
                ]

                cache.update(zip(batch, translated))
                time.sleep(0.01)  # yield GIL to Tkinter thread between batches
                cache_file.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                done = min(i + batch_size, total)
                self.set_progress(
                    0.25 + 0.55 * done / total,
                    f"Traduzindo... {done}/{total}",
                )
                self.log(f"  {done}/{total} traduzidos")
        else:
            self.log("\n[2/3] Tudo no cache, pulando traducao.")

        # ── Phase 3: Inject ─────────────────────────────────────────────────
        self.log("\n[3/3] Aplicando traducoes nos assets...")
        for i, (af, is_locale, bundle_lang) in enumerate(asset_entries):
            self.set_progress(
                0.82 + 0.16 * i / max(len(asset_entries), 1),
                f"Aplicando: {af.name}",
            )
            if is_locale and bundle_lang is not None and bundle_lang != self.src_lang:
                continue

            backup = self._ensure_backup(af, data_dir, backup_root)
            if af.suffix.lower() in _UNITY_EXTS:
                changed = self._apply_unity_file(backup, af, cache, generator)
            else:
                changed = self._apply_text_file(backup, af, cache)
            if changed:
                self.log(f"  Modificado: {af.name}")

        self.set_progress(1.0, "Concluido!")
        self.log(f"\nConcluido! Reinicie o jogo para ver a traducao.")
        self.log(f"Cache em: {cache_file}")
        self.log(f"Backup em: {backup_root}")
        return data_dir
