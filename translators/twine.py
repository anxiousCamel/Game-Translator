from __future__ import annotations
import html as html_mod
import json
import re
from pathlib import Path

from .base import BaseTranslator
from .engine import ensure_model, translate_texts

PASSAGE_RE = re.compile(r"(<tw-passagedata[^>]*>)(.*?)(</tw-passagedata>)", re.DOTALL)
# Preserva: macros SC <<...>>, links/imagens [[...]] e [img[...]], tags HTML, variáveis $x
PRESERVE_RE = re.compile(
    r"(<<.*?>>|\[\[.*?\]\]|\[[a-z]+\[.*?\]\]+|<[^>]+>|\$[\w.]+)",
    re.DOTALL,
)
SKIP_RE = re.compile(r"^[\s\d\W]{0,3}$")
# Tags de passagem que contêm código — não devem ser traduzidas
_CODE_TAGS = {"script", "stylesheet", "widget", "init", "annotation", "nobr"}


class TwineTranslator(BaseTranslator):
    def translate(self) -> Path:
        html_path = self._resolve_html()
        ensure_model(self.src_lang, self.tgt_lang, self.log)

        self.log(f"Lendo: {html_path.name}")
        original = html_path.read_text(encoding="utf-8", errors="replace")
        passages = _extract_passages(original)
        self.log(f"{len(passages)} passagens encontradas.")

        cache_path = html_path.with_name(html_path.stem + ".traducoes.json")
        cache = _load_cache(cache_path, self.log)

        texts = _collect_texts(passages)
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
            progress = done / max(total, 1)
            self.set_progress(progress * 0.95, f"Traduzindo... {done}/{total}")
            self.log(f"  {done}/{total} textos traduzidos")

        self.log("Aplicando traducoes ao HTML...")
        new_html = _apply_translations(passages, cache, original)

        output_path = html_path.with_name(html_path.stem + f"-{self.tgt_lang}.html")
        output_path.write_text(new_html, encoding="utf-8")
        self.set_progress(1.0, "Concluido!")
        return output_path

    def _resolve_html(self) -> Path:
        if self.path.is_file():
            return self.path
        candidates = list(self.path.glob("*.html")) + list(self.path.glob("*.htm"))
        for c in candidates:
            snippet = c.read_text(encoding="utf-8", errors="ignore")[:200_000]
            if "tw-passagedata" in snippet:
                return c
        if candidates:
            return candidates[0]
        raise FileNotFoundError(f"Nenhum HTML Twine encontrado em: {self.path}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_passages(content: str) -> list[dict]:
    passages = []
    for m in PASSAGE_RE.finditer(content):
        open_tag = m.group(1)
        name_m = re.search(r'name="([^"]*)"', open_tag)
        tags_m = re.search(r'tags="([^"]*)"', open_tag)
        tag_set = set((tags_m.group(1) if tags_m else "").lower().split())
        passages.append(
            {
                "open": open_tag,
                "content": m.group(2),
                "close": m.group(3),
                "name": name_m.group(1) if name_m else "",
                "skip": bool(tag_set & _CODE_TAGS),
                "span": (m.start(), m.end()),
            }
        )
    return passages


def _collect_texts(passages: list[dict]) -> list[str]:
    seen: set[str] = set()
    result = []
    for p in passages:
        if p["skip"]:
            continue
        decoded = html_mod.unescape(p["content"])
        for seg in PRESERVE_RE.split(decoded):
            seg = seg.strip()
            if seg and not PRESERVE_RE.match(seg) and not SKIP_RE.match(seg) and len(seg) > 2:
                if seg not in seen:
                    seen.add(seg)
                    result.append(seg)
    return result


def _apply_translations(passages: list[dict], cache: dict, original: str) -> str:
    result = []
    prev = 0
    for p in passages:
        result.append(original[prev : p["span"][0]])
        if p["skip"]:
            # Passagem de código: mantém intacta
            result.append(p["open"] + p["content"] + p["close"])
        else:
            decoded = html_mod.unescape(p["content"])
            parts = PRESERVE_RE.split(decoded)
            new_parts = []
            for seg in parts:
                stripped = seg.strip()
                if stripped and not PRESERVE_RE.match(stripped) and cache.get(stripped):
                    new_parts.append(seg.replace(stripped, cache[stripped], 1))
                else:
                    new_parts.append(seg)
            new_content = html_mod.escape("".join(new_parts), quote=False)
            result.append(p["open"] + new_content + p["close"])
        prev = p["span"][1]
    result.append(original[prev:])
    return "".join(result)


def _load_cache(path: Path, log) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cache = data.get("texts", data)
        log(f"Cache carregado: {sum(1 for v in cache.values() if v)} traducoes.")
        return cache
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict):
    path.write_text(
        json.dumps({"texts": cache}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
