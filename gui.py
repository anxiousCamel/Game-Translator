#!/usr/bin/env python3
"""
Game Translator — Interface grafica para traducao de jogos Twine, RenPy e RPGMaker MV/MZ.
"""
from __future__ import annotations

import threading
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from translators.detector import detect_game_type, GAME_TYPE_LABELS
from translators.twine import TwineTranslator
from translators.renpy import RenpyTranslator
from translators.rpgmaker import RPGMakerTranslator

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

LANGUAGES: dict[str, str] = {
    "English": "en",
    "Portugues (BR)": "pt",
    "Espanol": "es",
    "Francais": "fr",
    "Deutsch": "de",
    "Italiano": "it",
    "Русский": "ru",
    "Nihongo (JA)": "ja",
    "Zhongwen (ZH)": "zh",
    "Korean (KO)": "ko",
}

GAME_TYPES: dict[str, str | None] = {
    "Auto-detectar": None,
    "Twine / SugarCube": "twine",
    "RenPy": "renpy",
    "RPGMaker MV/MZ": "rpgmaker",
}

TRANSLATORS = {
    "twine": TwineTranslator,
    "renpy": RenpyTranslator,
    "rpgmaker": RPGMakerTranslator,
}


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Game Translator")
        self.geometry("680x620")
        self.minsize(560, 520)
        self._translating = False
        self._build_ui()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # Cabecalho
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(18, 4), sticky="ew")
        ctk.CTkLabel(
            header,
            text="Game Translator",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text="Twine  |  RenPy  |  RPGMaker MV/MZ",
            font=ctk.CTkFont(size=12),
            text_color="gray",
        ).pack(side="left", padx=(12, 0), pady=(6, 0))

        # Selecao de jogo
        self._build_selection()

        # Opcoes (tipo + idiomas)
        self._build_options()

        # Log
        self._build_log()

        # Progresso + botao
        self._build_footer()

    def _build_selection(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=1, column=0, padx=20, pady=(6, 6), sticky="ew")
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame, text="Jogo / Diretorio", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=14, pady=(10, 4), sticky="w"
        )

        self.path_entry = ctk.CTkEntry(
            frame, placeholder_text="Selecione a pasta ou arquivo do jogo..."
        )
        self.path_entry.grid(row=1, column=0, padx=(14, 6), pady=(0, 12), sticky="ew")

        ctk.CTkButton(
            frame, text="Pasta", width=80, command=self._select_folder
        ).grid(row=1, column=1, padx=2, pady=(0, 12))

        ctk.CTkButton(
            frame, text="Arquivo", width=80, command=self._select_file
        ).grid(row=1, column=2, padx=(2, 14), pady=(0, 12))

    def _build_options(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=2, column=0, padx=20, pady=(0, 6), sticky="ew")
        frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        labels = ["Tipo", "Idioma de origem", "", "Idioma de destino"]
        for col, text in enumerate(labels):
            ctk.CTkLabel(frame, text=text, font=ctk.CTkFont(weight="bold")).grid(
                row=0, column=col, padx=(14 if col == 0 else 6, 6), pady=(10, 2), sticky="w"
            )

        self.type_var = ctk.StringVar(value="Auto-detectar")
        ctk.CTkOptionMenu(
            frame, variable=self.type_var, values=list(GAME_TYPES.keys()), width=160
        ).grid(row=1, column=0, padx=(14, 6), pady=(0, 12), sticky="ew")

        self.src_var = ctk.StringVar(value="English")
        ctk.CTkOptionMenu(
            frame, variable=self.src_var, values=list(LANGUAGES.keys())
        ).grid(row=1, column=1, padx=6, pady=(0, 12), sticky="ew")

        ctk.CTkLabel(frame, text="->", font=ctk.CTkFont(size=18)).grid(row=1, column=2, padx=2)

        self.tgt_var = ctk.StringVar(value="Portugues (BR)")
        ctk.CTkOptionMenu(
            frame, variable=self.tgt_var, values=list(LANGUAGES.keys())
        ).grid(row=1, column=3, padx=(6, 14), pady=(0, 12), sticky="ew")

    def _build_log(self):
        frame = ctk.CTkFrame(self)
        frame.grid(row=4, column=0, padx=20, pady=(0, 6), sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        ctk.CTkLabel(header, text="Log", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(
            header, text="Limpar", width=60, height=22,
            font=ctk.CTkFont(size=11), command=self._clear_log
        ).pack(side="right")

        self.log_box = ctk.CTkTextbox(frame, state="disabled", font=ctk.CTkFont(size=12))
        self.log_box.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="nsew")

    def _build_footer(self):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=5, column=0, padx=20, pady=(0, 16), sticky="ew")
        frame.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            frame, text="Aguardando...", text_color="gray", font=ctk.CTkFont(size=12)
        )
        self.status_label.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.progress_bar = ctk.CTkProgressBar(frame, height=10)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        self.translate_btn = ctk.CTkButton(
            frame,
            text="Traduzir Agora",
            height=46,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._start_translation,
        )
        self.translate_btn.grid(row=2, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # Acoes
    # ------------------------------------------------------------------

    def _select_folder(self):
        path = filedialog.askdirectory(title="Selecione a pasta do jogo")
        if path:
            self._set_path(Path(path))

    def _select_file(self):
        path = filedialog.askopenfilename(
            title="Selecione o arquivo do jogo",
            filetypes=[
                ("Jogos Twine", "*.html *.htm"),
                ("Todos os arquivos", "*.*"),
            ],
        )
        if path:
            self._set_path(Path(path))

    def _set_path(self, path: Path):
        self.path_entry.delete(0, "end")
        self.path_entry.insert(0, str(path))
        detected = detect_game_type(path)
        if detected:
            label = GAME_TYPE_LABELS.get(detected, detected)
            for name, key in GAME_TYPES.items():
                if key == detected:
                    self.type_var.set(name)
                    break
            self._log(f"Detectado: {label}")
        else:
            self._log("Tipo nao detectado — selecione manualmente.")

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _set_progress(self, value: float, label: str = None):
        self.progress_bar.set(value)
        if label:
            self.status_label.configure(text=label)

    def _start_translation(self):
        if self._translating:
            return

        path_str = self.path_entry.get().strip()
        if not path_str:
            self._log("Selecione o jogo antes de traduzir.")
            return

        path = Path(path_str)
        if not path.exists():
            self._log(f"Caminho nao encontrado: {path}")
            return

        type_key = GAME_TYPES.get(self.type_var.get())
        if not type_key:
            type_key = detect_game_type(path)
            if not type_key:
                self._log(
                    "Nao foi possivel detectar o tipo de jogo.\n"
                    "Selecione manualmente no menu 'Tipo'."
                )
                return

        src_lang = LANGUAGES[self.src_var.get()]
        tgt_lang = LANGUAGES[self.tgt_var.get()]
        if src_lang == tgt_lang:
            self._log("Idioma de origem e destino sao iguais.")
            return

        translator_cls = TRANSLATORS[type_key]
        self._translating = True
        self.translate_btn.configure(state="disabled", text="Traduzindo...")
        self._set_progress(0, "Iniciando...")
        self._log(f"\n--- Iniciando traducao ({GAME_TYPE_LABELS[type_key]}) ---")
        self._log(f"Caminho: {path}")
        self._log(f"Idiomas: {self.src_var.get()} -> {self.tgt_var.get()}\n")

        def run():
            try:
                translator = translator_cls(
                    path=path,
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    log_fn=lambda msg: self.after(0, lambda m=msg: self._log(m)),
                    progress_fn=lambda v, l=None: self.after(
                        0, lambda vv=v, ll=l: self._set_progress(vv, ll)
                    ),
                )
                output = translator.translate()
                self.after(0, lambda: self._log(f"\nConcluido! Saida:\n  {output}"))
            except Exception as exc:
                self.after(0, lambda: self._log(f"\nErro: {exc}"))
            finally:
                self._translating = False
                self.after(
                    0,
                    lambda: self.translate_btn.configure(state="normal", text="Traduzir Agora"),
                )

        threading.Thread(target=run, daemon=True).start()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
