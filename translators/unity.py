from __future__ import annotations
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .base import BaseTranslator
from .engine import ensure_model, translate_texts

# ---------------------------------------------------------------------------
# Text filtering
# ---------------------------------------------------------------------------

_SKIP_MB_FIELDS = frozenset({
    "m_Name", "name", "m_Script", "m_GameObject",
    "guid", "fileID", "type", "m_PathID", "m_FileID",
    "m_ObjectHideFlags", "m_Tag", "m_Layer", "m_ClassName",
    "m_Namespace", "m_AssemblyName", "m_Font", "m_FontName",
    "m_AtlasName", "m_SpriteName", "m_ShaderName",
    "m_SceneName", "m_SceneGUID",
})

_GUID_RE      = re.compile(r'^[0-9a-f]{8,}$', re.ASCII)
_CAMEL_RE     = re.compile(r'^[a-z][a-zA-Z0-9]{2,}$')
_SCREAMING_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,}$')
_HEX_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{3,8}$')
_NUMBER_RE    = re.compile(r'^\d[\d.,\s%]*[a-zA-Z]?$')
# Unicode char-range strings like "20-7E,A1-AC,..." (font character maps)
_CHARRANGE_RE = re.compile(r'^[0-9A-Fa-f]{2,4}(-[0-9A-Fa-f]{2,4})?(,[0-9A-Fa-f])')

# Addressables catalog JSON — do not translate
_CATALOG_KEYS = frozenset({"m_LocatorId", "m_InstanceProviderData", "m_ProviderIds"})

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


def _looks_translatable(s: str) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 3:
        return False
    if not any(c.isalpha() for c in s):
        return False
    if _GUID_RE.match(s):
        return False
    if _CAMEL_RE.match(s) or _SCREAMING_RE.match(s):
        return False
    if _HEX_COLOR_RE.match(s) or _NUMBER_RE.match(s):
        return False
    if _CHARRANGE_RE.match(s):
        return False
    # Underscore-separated identifiers (bone names, animation states, asset refs)
    # "L_arm", "A_attack", "angler_01_Atlas" — but not "Give birth" (has space)
    if "_" in s and " " not in s:
        return False
    if "/" in s or "\\" in s:
        return False
    if s.startswith(("http", "Assets/", "assets/", "Packages/")):
        return False
    return True


def _collect(obj: Any, out: set) -> None:
    if isinstance(obj, str):
        if _looks_translatable(obj):
            out.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in _SKIP_MB_FIELDS:
                _collect(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect(item, out)


def _patch(obj: Any, cache: dict) -> Any:
    if isinstance(obj, str):
        return cache.get(obj, obj)
    if isinstance(obj, dict):
        return {k: (v if k in _SKIP_MB_FIELDS else _patch(v, cache)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_patch(item, cache) for item in obj]
    return obj


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

        for obj in env.objects:
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
                    for line in raw.splitlines():
                        line = line.strip()
                        if _looks_translatable(line):
                            out.add(line)

                elif obj.type.name == "MonoBehaviour":
                    try:
                        tree = obj.read_typetree()
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
        for line in raw.splitlines():
            line = line.strip()
            if _looks_translatable(line):
                out.add(line)
        return len(out) - before

    # ── Injection ──────────────────────────────────────────────────────────

    def _apply_unity_file(self, src: Path, dst: Path, cache: dict, generator=None) -> bool:
        """Load from src (backup), apply translations, write to dst (live game file)."""
        modified = False
        try:
            env = _load_env_with_generator(src, generator)
        except Exception:
            return False

        for obj in env.objects:
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
                    new_lines = []
                    changed = False
                    for line in raw.splitlines(keepends=True):
                        stripped = line.strip()
                        tr = cache.get(stripped)
                        if tr and tr != stripped:
                            new_lines.append(line.replace(stripped, tr, 1))
                            changed = True
                        else:
                            new_lines.append(line)
                    if changed:
                        data.text = "".join(new_lines)
                        data.save()
                        modified = True

                elif obj.type.name == "MonoBehaviour":
                    try:
                        tree = obj.read_typetree()
                        new_tree = _patch(tree, cache)
                        if new_tree != tree:
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
        new_lines = []
        changed = False
        for line in raw.splitlines(keepends=True):
            stripped = line.strip()
            tr = cache.get(stripped)
            if tr and tr != stripped:
                new_lines.append(line.replace(stripped, tr, 1))
                changed = True
            else:
                new_lines.append(line)
        if changed:
            dst.write_text("".join(new_lines), encoding="utf-8")
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

        ensure_model(self.src_lang, self.tgt_lang, self.log)

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
                translated = translate_texts(batch, self.src_lang, self.tgt_lang)
                cache.update(zip(batch, translated))
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
