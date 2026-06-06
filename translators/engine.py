from __future__ import annotations
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Language detection helpers
# ---------------------------------------------------------------------------

# Strips formatting codes before feeding text to langid:
#   RPGMaker: \c[3]  \v[1]  \n  \!  \>  etc.
#   RenPy:    {b}  {/b}  {color=#fff}  [variable]
#   HTML/Twine: <span>  &amp;
_LANG_STRIP_RE = re.compile(
    r"\\[a-zA-Z]\[\d+\]"   # \c[3]  \v[1]
    r"|\\[!>.<^{}\\|n]"    # \!  \n  \>  etc.
    r"|\{[^}]*\}"           # {b}  {/b}  {color=…}
    r"|\[[^\]]*\]"          # [variable]
    r"|<[^>]+>"             # <span>  <br>
    r"|&\w+;"               # &amp;  &lt;
)


def _strip_for_detect(s: str) -> str:
    return _LANG_STRIP_RE.sub("", s).strip()


def _is_dialog_like(s: str) -> bool:
    s = _strip_for_detect(s)
    if len(s) < 8:
        return False
    # Pure identifier / path / variable name → not dialog
    if re.match(r'^[\w\.\$\#\/\\:\-]+$', s):
        return False
    return True


def detect_source_language(texts: list[str], sample_size: int = 50) -> str | None:
    """Return ISO 639-1 language code detected from a sample of game texts, or None."""
    try:
        import langid
    except ImportError:
        return None

    from collections import Counter
    samples = [_strip_for_detect(t) for t in texts if _is_dialog_like(t)][:sample_size]
    if not samples:
        return None

    votes: Counter = Counter()
    for s in samples:
        try:
            lang, _ = langid.classify(s)
            votes[lang] += 1
        except Exception:
            pass

    if not votes:
        return None
    winner, count = votes.most_common(1)[0]
    return winner if (count / len(samples)) >= 0.4 else None


# ---------------------------------------------------------------------------
# Hardware auto-detection
# ---------------------------------------------------------------------------

_hw: dict | None = None
_hw_lock = threading.Lock()


def _detect_hardware() -> dict:
    try:
        import psutil
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        phys_cores = psutil.cpu_count(logical=False) or 4
    except Exception:
        ram_gb = 4.0
        phys_cores = 2

    has_gpu = False
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        has_gpu = r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        pass

    if not has_gpu:
        try:
            import ctranslate2
            has_gpu = bool(ctranslate2.get_supported_compute_types("cuda"))
        except Exception:
            pass

    if has_gpu:
        return dict(device="cuda", compute_type="float16",
                    inter_threads=1, intra_threads=1, batch_size=512)

    batch = 128 if ram_gb >= 8 else (64 if ram_gb >= 4 else 32)
    return dict(device="cpu", compute_type="int8",
                inter_threads=1, intra_threads=phys_cores, batch_size=batch)


def _hw_config() -> dict:
    global _hw
    if _hw is not None:
        return _hw
    with _hw_lock:
        if _hw is not None:
            return _hw
        _hw = _detect_hardware()
        return _hw


# ---------------------------------------------------------------------------
# GPU runtime libs — on-demand download from PyPI wheels
# ---------------------------------------------------------------------------

def _gpu_libs_dir() -> Path:
    """Consistent path for extracted CUDA DLLs, works in dev and PyInstaller."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
    return base / "gpu_libs"


# Only cuBLAS is needed by CTranslate2; add nvidia-cudnn-cu12 if future errors require it.
_GPU_PACKAGES = ["nvidia-cublas-cu12"]
_GPU_KEY_DLL = "cublas64_12.dll"


def download_gpu_libs(
    log_fn: Callable = print,
    progress_fn: Callable | None = None,
) -> bool:
    """Download NVIDIA CUDA runtime DLLs from PyPI wheels into gpu_libs/.
    Returns True on success. DLLs are extracted flat (no subdirs)."""
    import json
    import urllib.request
    import zipfile

    gpu_dir = _gpu_libs_dir()
    gpu_dir.mkdir(exist_ok=True)

    n = len(_GPU_PACKAGES)
    for idx, pkg_name in enumerate(_GPU_PACKAGES):
        base_frac = idx / n
        span = 1.0 / n

        log_fn(f"Consultando PyPI: {pkg_name}...")
        try:
            with urllib.request.urlopen(
                f"https://pypi.org/pypi/{pkg_name}/json", timeout=15
            ) as resp:
                meta = json.loads(resp.read())
        except Exception as exc:
            log_fn(f"Erro ao consultar PyPI ({pkg_name}): {exc}")
            return False

        version = meta["info"]["version"]
        wheel = next(
            (u for u in meta["urls"] if u["filename"].endswith(".whl") and "win_amd64" in u["filename"]),
            None,
        )
        if not wheel:
            log_fn(f"Wheel win_amd64 nao encontrado para {pkg_name} {version}.")
            return False

        url, size, filename = wheel["url"], wheel.get("size", 0), wheel["filename"]
        wheel_path = gpu_dir / filename
        log_fn(f"Baixando {filename} ({size / 1_048_576:.0f} MB)...")

        downloaded = 0
        try:
            with urllib.request.urlopen(url, timeout=300) as resp, open(wheel_path, "wb") as out:
                while chunk := resp.read(65536):
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_fn and size:
                        frac = base_frac + (downloaded / size) * span * 0.9
                        progress_fn(frac, f"Baixando {pkg_name}: {downloaded/1_048_576:.0f}/{size/1_048_576:.0f} MB")
        except Exception as exc:
            log_fn(f"Erro no download de {filename}: {exc}")
            wheel_path.unlink(missing_ok=True)
            return False

        log_fn(f"Extraindo DLLs de {filename}...")
        try:
            with zipfile.ZipFile(wheel_path) as zf:
                for member in (m for m in zf.namelist() if m.lower().endswith(".dll")):
                    dll_name = member.rsplit("/", 1)[-1]
                    with zf.open(member) as src, open(gpu_dir / dll_name, "wb") as dst:
                        dst.write(src.read())
                    log_fn(f"  + {dll_name}")
        except Exception as exc:
            log_fn(f"Erro ao extrair {filename}: {exc}")
            return False
        finally:
            wheel_path.unlink(missing_ok=True)

        if progress_fn:
            progress_fn(base_frac + span, f"{pkg_name} instalado.")

    if progress_fn:
        progress_fn(1.0, "Pacote GPU pronto! Reinicie a traducao.")
    log_fn("Pacote GPU instalado. Reinicie a traducao para usar aceleracao por GPU.")
    return True


# ---------------------------------------------------------------------------
# Permission fix (Windows stanza/argos install issue)
# ---------------------------------------------------------------------------

def _fix_argos_perms() -> None:
    """Remove files left locked by a crashed install so the next attempt can overwrite them.
    On Windows, os.remove works via DELETE_CHILD on the parent even when the file
    itself has a broken or inherited ACE. On POSIX, fall back to chmod."""
    base = os.path.join(os.path.expanduser("~"), ".local", "share", "argos-translate", "packages")
    if not os.path.isdir(base):
        return
    for root, dirs, files in os.walk(base):
        for name in files:
            path = os.path.join(root, name)
            try:
                with open(path, "r+b"):
                    pass
            except PermissionError:
                try:
                    os.remove(path)
                except OSError:
                    if os.name != "nt":
                        try:
                            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
                        except OSError:
                            pass


# ---------------------------------------------------------------------------
# Model installation
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_installed: set[tuple[str, str]] = set()
_translation_paths: dict[tuple[str, str], list[str]] = {}


def _check_hop_installed(src: str, tgt: str, installed_langs: list) -> bool:
    src_l = next((l for l in installed_langs if l.code == src), None)
    return src_l is not None and any(
        getattr(t, "code", None) == tgt for t in src_l.translations_to
    )


def _find_hop_path(src: str, tgt: str, available: list) -> list[str] | None:
    """Return [src, tgt] for direct or [src, pivot, tgt] for one-hop. None if unreachable."""
    if any(p.from_code == src and p.to_code == tgt for p in available):
        return [src, tgt]
    src_dests = {p.to_code for p in available if p.from_code == src}
    tgt_srcs = {p.from_code for p in available if p.to_code == tgt}
    pivots = src_dests & tgt_srcs
    if "en" in pivots:
        return [src, "en", tgt]
    if pivots:
        return [src, min(pivots), tgt]
    return None


def _install_pkg(src: str, tgt: str, available: list, log_fn: Callable) -> None:
    import argostranslate.package
    pkg = next((p for p in available if p.from_code == src and p.to_code == tgt), None)
    if not pkg:
        raise ValueError(f"Pacote {src}->{tgt} nao encontrado no indice.")
    log_fn(f"  Baixando {src}->{tgt}...")
    downloaded = pkg.download()
    for _attempt in range(10):
        try:
            argostranslate.package.install_from_path(downloaded)
            break
        except PermissionError as exc:
            blocked = getattr(exc, "filename", None)
            if blocked and os.path.isfile(blocked):
                try:
                    os.remove(blocked)
                    continue
                except OSError:
                    pass
            raise
    log_fn(f"  {src}->{tgt} instalado.")


def ensure_model(src: str, tgt: str, log_fn: Callable = print, engine: str = "local") -> None:
    if engine != "local":
        return  # online engines don't need local model installation
    key = (src, tgt)
    if key in _installed:
        return
    with _lock:
        if key in _installed:
            return

        hw = _hw_config()
        device_label = "GPU (CUDA)" if hw["device"] == "cuda" else "CPU"
        log_fn(f"Hardware: {device_label} | batch={hw['batch_size']} | threads={hw['intra_threads']}")

        import argostranslate.package
        import argostranslate.translate

        log_fn(f"Verificando modelo {src} -> {tgt}...")
        installed_langs = argostranslate.translate.get_installed_languages()

        # Fast path: direct pair already installed
        if _check_hop_installed(src, tgt, installed_langs):
            _fix_argos_perms()
            _translation_paths[key] = [src, tgt]
            _installed.add(key)
            log_fn(f"Modelo {src}->{tgt} ja instalado.")
            return

        # Fetch package index to resolve path and install missing hops
        log_fn(f"Resolvendo rota de traducao {src}->{tgt}...")
        _fix_argos_perms()
        argostranslate.package.update_package_index()
        available = argostranslate.package.get_available_packages()

        path = _find_hop_path(src, tgt, available)
        if path is None:
            src_opts = sorted({p.to_code for p in available if p.from_code == src})
            raise ValueError(
                f"Sem rota de traducao para {src}->{tgt}. "
                f"Pares disponiveis de '{src}': {src_opts}"
            )

        if len(path) == 3:
            pivot = path[1]
            log_fn(f"Modelo direto {src}->{tgt} indisponivel. Rota: {src}->{pivot}->{tgt}")
            if not _check_hop_installed(src, pivot, installed_langs):
                _install_pkg(src, pivot, available, log_fn)
            if not _check_hop_installed(pivot, tgt, installed_langs):
                _install_pkg(pivot, tgt, available, log_fn)
        else:
            log_fn(f"Baixando modelo {src}->{tgt} (aguarde na primeira vez)...")
            _install_pkg(src, tgt, available, log_fn)

        _fix_argos_perms()
        _translation_paths[key] = path
        _installed.add(key)
        log_fn(f"Modelo {src}->{tgt} pronto.")


# ---------------------------------------------------------------------------
# Fast sentence boundary detection (regex — bypasses stanza entirely)
# ---------------------------------------------------------------------------

# Splits after .!? when followed by whitespace + capital letter, quote, or accented char.
# Conservative: avoids splitting mid-abbreviation in most cases (e.g. "Mr. Smith" → "r"
# before "." is lowercase but "S" after space is capital, so this WILL split there).
# Acceptable trade-off for game dialogue quality vs. stanza overhead.
_SENT_RE = re.compile(
    r"(?<=[!?])\s+(?=[A-Z\"'À-ɏ])"
    r"|(?<=\.)\s+(?=[A-Z\"'À-ɏ])"
    r"|(?<=…)\s+"
)


def _split_sentences(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return [""]
    parts = [p for p in _SENT_RE.split(stripped) if p]
    return parts or [stripped]


# ---------------------------------------------------------------------------
# CTranslate2 translator accessor (bypasses argostranslate high-level API)
# ---------------------------------------------------------------------------

_ct2_cache: dict[tuple[str, str], tuple] = {}
_ct2_lock = threading.Lock()


def _get_ct2(src: str, tgt: str) -> tuple:
    """Return (ctranslate2.Translator, tokenizer, target_prefix_or_None) for src→tgt."""
    key = (src, tgt)
    if key in _ct2_cache:
        return _ct2_cache[key]
    with _ct2_lock:
        if key in _ct2_cache:
            return _ct2_cache[key]

        from argostranslate.translate import get_translation_from_codes
        import ctranslate2

        cached_trans = get_translation_from_codes(src, tgt)
        if cached_trans is None:
            raise ValueError(
                f"Sem pacote de traducao para {src}->{tgt}. "
                "Execute ensure_model antes de traduzir."
            )

        pt = cached_trans.underlying  # PackageTranslation
        pkg = pt.pkg
        hw = _hw_config()

        # Inject downloaded GPU libs before any CUDA init.
        # os.add_dll_directory covers LOAD_LIBRARY_SEARCH_USER_DIRS (extension modules).
        # PATH modification covers plain LoadLibrary calls inside ctranslate2's C++ code.
        # ctypes pre-load puts the DLLs in the process table so any subsequent
        # LoadLibrary("cublas64_12.dll") call finds them already mapped.
        gpu_dir = _gpu_libs_dir()
        if gpu_dir.is_dir() and os.name == "nt":
            gpu_str = str(gpu_dir)
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(gpu_str)
            os.environ["PATH"] = gpu_str + os.pathsep + os.environ.get("PATH", "")
            import ctypes
            for dll in sorted(gpu_dir.glob("*.dll")):
                try:
                    ctypes.CDLL(str(dll))
                except OSError:
                    pass

        model_path = str(pkg.package_path / "model")
        try:
            translator = ctranslate2.Translator(
                model_path,
                device=hw["device"],
                inter_threads=hw["inter_threads"],
                intra_threads=hw["intra_threads"],
                compute_type=hw["compute_type"],
            )
        except Exception as exc:
            # cuBLAS/cuDNN DLLs may be absent even when CUDA device is detected.
            # Any GPU init failure falls back to CPU automatically.
            if hw["device"] == "cpu":
                raise
            try:
                import psutil
                phys_cores = psutil.cpu_count(logical=False) or 4
                ram_gb = psutil.virtual_memory().total / (1024 ** 3)
            except Exception:
                phys_cores, ram_gb = 2, 4.0
            hw = dict(device="cpu", compute_type="int8",
                      inter_threads=1, intra_threads=phys_cores,
                      batch_size=128 if ram_gb >= 8 else 64)
            global _hw
            _hw = hw
            print(f"Aviso: GPU falhou ({exc}). Usando CPU | batch={hw['batch_size']} | threads={hw['intra_threads']}")
            translator = ctranslate2.Translator(
                model_path,
                device="cpu",
                inter_threads=hw["inter_threads"],
                intra_threads=hw["intra_threads"],
                compute_type=hw["compute_type"],
            )
        pt.translator = translator

        tokenizer = pkg.tokenizer
        tgt_prefix = [pkg.target_prefix] if pkg.target_prefix else None

        result = (translator, tokenizer, tgt_prefix)
        _ct2_cache[key] = result
        return result


# ---------------------------------------------------------------------------
# True-batch translation (all texts → one ctranslate2.translate_batch call)
# ---------------------------------------------------------------------------

def _translate_single_pass(texts: list[str], src: str, tgt: str) -> list[str]:
    translator, tokenizer, tgt_prefix = _get_ct2(src, tgt)
    batch_size = _hw_config()["batch_size"]

    all_sents: list[str] = []
    bounds: list[tuple[int, int]] = []
    for text in texts:
        start = len(all_sents)
        sents = _split_sentences(text) if text.strip() else [""]
        all_sents.extend(sents)
        bounds.append((start, len(all_sents)))

    # Tokenize all sentences in one pass
    all_tok: list[list[str]] = [tokenizer.encode(s) for s in all_sents]

    # Send to CTranslate2 in hw-sized token batches
    all_results = []
    for i in range(0, len(all_tok), batch_size):
        chunk = all_tok[i : i + batch_size]
        prefix = [tgt_prefix] * len(chunk) if tgt_prefix else None
        out = translator.translate_batch(
            chunk,
            target_prefix=prefix,
            replace_unknowns=True,
            max_batch_size=batch_size,
            batch_type="tokens",
            beam_size=2,
            num_hypotheses=1,
            length_penalty=0.2,
        )
        all_results.extend(out)

    # Reconstruct per-text translations by concatenating sentence tokens then decoding
    translated: list[str] = []
    for idx, (start, end) in enumerate(bounds):
        orig = texts[idx]
        if not orig.strip():
            translated.append(orig)
            continue

        tokens: list[str] = []
        for r in all_results[start:end]:
            tokens.extend(r.hypotheses[0])

        decoded = tokenizer.decode(tokens).strip()
        translated.append(decoded if decoded else orig)

    return translated


# ---------------------------------------------------------------------------
# Google Translate backend (deep-translator, no API key needed)
# ---------------------------------------------------------------------------

# Google uses different codes for some languages
_GOOGLE_LANG = {"zh": "zh-CN"}


def _translate_google(texts: list[str], src: str, tgt: str) -> list[str]:
    from deep_translator import GoogleTranslator

    gsrc = _GOOGLE_LANG.get(src, src)
    gtgt = _GOOGLE_LANG.get(tgt, tgt)

    result: list[str] = []
    chunk_size = 50
    for i in range(0, len(texts), chunk_size):
        chunk = texts[i : i + chunk_size]
        non_empty = [(j, t) for j, t in enumerate(chunk) if t.strip()]
        if not non_empty:
            result.extend(chunk)
            continue
        idxs, to_tr = zip(*non_empty)
        try:
            translated = GoogleTranslator(source=gsrc, target=gtgt).translate_batch(list(to_tr))
            row = list(chunk)
            for j, tr in zip(idxs, translated):
                row[j] = tr or chunk[j]
            result.extend(row)
        except Exception:
            result.extend(chunk)  # fallback: keep original on error
        time.sleep(1.5)  # anti-ban: avoid HTTP 429 on large games

    return result


# ---------------------------------------------------------------------------
# Public translation entry point
# ---------------------------------------------------------------------------

def refazer_com_ia_litellm(
    texto_original: str,
    src_lang: str = "en",
    tgt_lang: str = "pt",
    model_name: str = "",
    api_key: str = "",
    base_url: str = "",
) -> str:
    """Translate a single text via LiteLLM (any provider: OpenAI, Gemini, Ollama, etc.)."""
    import litellm

    system_prompt = (
        f"You are a professional game localization translator. "
        f"Translate the following text from {src_lang} to {tgt_lang}. "
        "Preserve placeholder tokens like [T0], [T1], [T2] exactly as written. "
        "Return ONLY the translated text — no explanations, no quotes."
    )
    kwargs: dict = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": texto_original},
        ],
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    response = litellm.completion(**kwargs)
    return response.choices[0].message.content.strip()


def translate_texts(texts: list[str], src: str, tgt: str, workers: int = 0, engine: str = "local") -> list[str]:
    if not texts:
        return []
    if engine == "google":
        return _translate_google(texts, src, tgt)
    path = _translation_paths.get((src, tgt), [src, tgt])
    if len(path) == 3:
        pivot = path[1]
        intermediate = _translate_single_pass(texts, src, pivot)
        return _translate_single_pass(intermediate, pivot, tgt)
    return _translate_single_pass(texts, src, tgt)
