#!/usr/bin/env python3
"""
twine-translator — Traduz jogos Twine/SugarCube (HTML) para pt-BR.

Uso:
  python3 translate.py <arquivo.html>               # traduz e gera <arquivo>-ptbr.html
  python3 translate.py <arquivo.html> --batch 20    # tamanho do lote (padrão: 20)
  python3 translate.py <arquivo.html> --resume      # continua tradução interrompida
  python3 translate.py --install-model              # baixa modelo EN→PT (só na primeira vez)
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Extração / reconstrução de passagens Twine
# ---------------------------------------------------------------------------

PASSAGE_RE = re.compile(r'(<tw-passagedata[^>]*>)(.*?)(</tw-passagedata>)', re.DOTALL)
PRESERVE_RE = re.compile(r'(<<.*?>>|\[\[.*?\]\]|<[^>]+>|\$\w+)', re.DOTALL)
SKIP_RE = re.compile(r'^[\s\d\W]{0,3}$')


def extract_passages(content: str) -> list[dict]:
    passages = []
    for m in PASSAGE_RE.finditer(content):
        name_m = re.search(r'name="([^"]*)"', m.group(1))
        passages.append({
            "open": m.group(1),
            "content": m.group(2),
            "close": m.group(3),
            "name": name_m.group(1) if name_m else "",
            "span": (m.start(), m.end()),
        })
    return passages


def collect_texts(passages: list[dict]) -> dict[str, str]:
    texts = {}
    for p in passages:
        decoded = html.unescape(p["content"])
        for seg in PRESERVE_RE.split(decoded):
            seg = seg.strip()
            if seg and not PRESERVE_RE.match(seg) and not SKIP_RE.match(seg) and len(seg) > 2:
                texts[seg] = ""
    return texts


def apply_translations(passages: list[dict], translations: dict[str, str], original_html: str) -> str:
    result = []
    prev = 0
    for p in passages:
        result.append(original_html[prev: p["span"][0]])
        decoded = html.unescape(p["content"])
        parts = PRESERVE_RE.split(decoded)
        new_parts = []
        for seg in parts:
            stripped = seg.strip()
            if stripped and not PRESERVE_RE.match(stripped) and stripped in translations and translations[stripped]:
                new_parts.append(seg.replace(stripped, translations[stripped], 1))
            else:
                new_parts.append(seg)
        new_content = html.escape("".join(new_parts), quote=False)
        result.append(p["open"] + new_content + p["close"])
        prev = p["span"][1]
    result.append(original_html[prev:])
    return "".join(result)


# ---------------------------------------------------------------------------
# Backend de tradução — argostranslate
# ---------------------------------------------------------------------------

def install_model():
    try:
        import argostranslate.package
    except ImportError:
        print("Execute primeiro: pip install -r requirements.txt")
        sys.exit(1)

    print("Atualizando índice de pacotes...")
    argostranslate.package.update_package_index()
    pkgs = argostranslate.package.get_available_packages()
    pkg = next((p for p in pkgs if p.from_code == "en" and p.to_code == "pt"), None)
    if not pkg:
        print("Modelo EN→PT não encontrado no índice.")
        sys.exit(1)
    print(f"Baixando {pkg}...")
    argostranslate.package.install_from_path(pkg.download())
    print("Modelo instalado com sucesso!")


def translate_batch(texts: list[str]) -> list[str]:
    import argostranslate.translate
    return [argostranslate.translate.translate(t, "en", "pt") for t in texts]


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def run(input_path: Path, batch_size: int, resume: bool):
    cache_path = input_path.with_suffix(".traducoes.json")
    output_path = input_path.with_name(input_path.stem + "-ptbr.html")

    print(f"Lendo {input_path.name}...")
    original = input_path.read_text(encoding="utf-8")
    passages = extract_passages(original)
    print(f"  {len(passages)} passagens encontradas.")

    # Carrega ou cria cache de traduções
    if resume and cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        texts = data["texts"]
        print(f"  Retomando: {sum(1 for v in texts.values() if v)} já traduzidos.")
    else:
        texts = collect_texts(passages)
        print(f"  {len(texts)} textos únicos extraídos.")
        cache_path.write_text(json.dumps({"texts": texts}, ensure_ascii=False, indent=2), encoding="utf-8")

    to_do = [k for k, v in texts.items() if not v.strip()]
    print(f"  {len(to_do)} textos para traduzir...\n")

    if not to_do:
        print("Nada a traduzir — todos já estão no cache.")
    else:
        try:
            import argostranslate.translate
        except ImportError:
            print("Dependências ausentes. Execute:\n  pip install -r requirements.txt\n  python3 translate.py --install-model")
            sys.exit(1)

        total = len(to_do)
        for i in range(0, total, batch_size):
            batch = to_do[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total + batch_size - 1) // batch_size
            print(f"  Lote {batch_num}/{total_batches}...", end=" ", flush=True)
            try:
                translated = translate_batch(batch)
                for orig, trad in zip(batch, translated):
                    texts[orig] = trad
                print(f"OK  ({min(i + batch_size, total)}/{total})")
            except Exception as e:
                print(f"ERRO: {e}")
            # salva progresso
            cache_path.write_text(json.dumps({"texts": texts}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nAplicando traduções...")
    new_html = apply_translations(passages, texts, original)
    output_path.write_text(new_html, encoding="utf-8")
    done = sum(1 for v in texts.values() if v)
    print(f"Pronto! {done}/{len(texts)} textos traduzidos.")
    print(f"Arquivo gerado: {output_path.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Traduz jogos Twine/SugarCube para pt-BR.")
    parser.add_argument("input", nargs="?", help="Arquivo HTML do jogo")
    parser.add_argument("--batch", type=int, default=20, help="Textos por lote (padrão: 20)")
    parser.add_argument("--resume", action="store_true", help="Retoma tradução interrompida")
    parser.add_argument("--install-model", action="store_true", help="Baixa modelo EN→PT")
    args = parser.parse_args()

    if args.install_model:
        install_model()
        return

    if not args.input:
        parser.print_help()
        sys.exit(1)

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        print(f"Arquivo não encontrado: {input_path}")
        sys.exit(1)

    run(input_path, args.batch, args.resume)


if __name__ == "__main__":
    main()
