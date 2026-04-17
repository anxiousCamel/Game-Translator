from __future__ import annotations
import threading
from typing import Callable

_lock = threading.Lock()
_installed: set[tuple[str, str]] = set()


def ensure_model(src: str, tgt: str, log_fn: Callable = print) -> None:
    key = (src, tgt)
    if key in _installed:
        return
    with _lock:
        if key in _installed:
            return
        import argostranslate.package
        import argostranslate.translate

        log_fn(f"Verificando modelo {src} -> {tgt}...")
        installed = argostranslate.translate.get_installed_languages()
        src_lang = next((l for l in installed if l.code == src), None)
        if src_lang:
            tgt_match = next((t for t in src_lang.translations_to if getattr(t, "code", None) == tgt), None)
            if tgt_match:
                _installed.add(key)
                log_fn(f"Modelo {src}->{tgt} ja instalado.")
                return

        log_fn(f"Baixando modelo {src}->{tgt} (aguarde na primeira vez)...")
        argostranslate.package.update_package_index()
        available = argostranslate.package.get_available_packages()
        pkg = next((p for p in available if p.from_code == src and p.to_code == tgt), None)
        if not pkg:
            raise ValueError(
                f"Modelo de traducao nao disponivel para {src}->{tgt}. "
                f"Verifique os idiomas selecionados."
            )
        argostranslate.package.install_from_path(pkg.download())
        _installed.add(key)
        log_fn(f"Modelo {src}->{tgt} instalado com sucesso.")


def translate_texts(texts: list[str], src: str, tgt: str) -> list[str]:
    from argostranslate.translate import translate

    return [translate(t, src, tgt) if t.strip() else t for t in texts]
