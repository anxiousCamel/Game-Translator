#!/usr/bin/env python3
"""
Game Translator — Interface grafica para traducao de jogos Twine, RenPy e RPGMaker MV/MZ.
"""
from __future__ import annotations

import multiprocessing
import queue
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from translators.detector import detect_game_type, GAME_TYPE_LABELS, sample_game_texts
from translators.engine import _hw_config, _gpu_libs_dir, _GPU_KEY_DLL, download_gpu_libs, detect_source_language
from translators.twine import TwineTranslator
from translators.renpy import RenpyTranslator
from translators.rpgmaker import RPGMakerTranslator
from translators.unity import UnityTranslator

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
    "Unity": "unity",
}

TRANSLATORS = {
    "twine": TwineTranslator,
    "renpy": RenpyTranslator,
    "rpgmaker": RPGMakerTranslator,
    "unity": UnityTranslator,
}

# ---------------------------------------------------------------------------
# Worker (runs in a separate OS process — zero GIL sharing with UI)
# ---------------------------------------------------------------------------

def worker_process(
    translator_cls,
    path_str: str,
    src_lang: str,
    tgt_lang: str,
    ipc_queue: "multiprocessing.Queue[tuple]",
) -> None:
    """
    Instantiates and runs the translator completely outside the UI process.
    Communicates back via tagged tuples on ipc_queue:
      ("LOG",      msg: str)
      ("PROGRESS", val: float, lbl: str | None)
      ("DONE",     success: bool)
    """
    from pathlib import Path as _Path

    def _log(msg: str) -> None:
        ipc_queue.put(("LOG", msg))

    def _progress(val: float, lbl: str | None = None) -> None:
        ipc_queue.put(("PROGRESS", val, lbl))

    try:
        translator = translator_cls(
            path=_Path(path_str),
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            log_fn=_log,
            progress_fn=_progress,
        )
        output = translator.translate()
        ipc_queue.put(("LOG", f"\nConcluido! Saida:\n  {output}"))
        ipc_queue.put(("DONE", True))
    except Exception as exc:
        ipc_queue.put(("LOG", f"\nErro: {exc}"))
        ipc_queue.put(("DONE", False))


_BTN_DEFAULT = "#1F6AA5"   # CTk default blue
_BTN_HOVER   = "#2980B9"   # lighter blue
_BTN_BUSY    = "#3D3D3D"   # dark gray (disabled feel)
_BTN_SUCCESS = "#3EA6FF"   # bright blue flash on success


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Game Translator")
        self.geometry("680x620")
        self.minsize(560, 520)
        self._translating = False
        self._progress_indeterminate = False
        self._last_progress_time = 0.0
        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._proc_queue: multiprocessing.Queue | None = None
        self._build_ui()
        self.after(800, self._check_gpu_prompt)
        self._poll_ui_queue()

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
            text="Twine  |  RenPy  |  RPGMaker MV/MZ  |  Unity",
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
            fg_color=_BTN_DEFAULT,
            hover_color=_BTN_HOVER,
            command=self._start_translation,
        )
        self.translate_btn.grid(row=2, column=0, sticky="ew")

    # ------------------------------------------------------------------
    # Thread-safe UI queue
    # ------------------------------------------------------------------

    def _poll_ui_queue(self):
        """Drain thread callbacks and process IPC messages in main thread at ~60 fps."""
        # Thread-based callables (GPU download, lang detect, etc.)
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                fn()
        except queue.Empty:
            pass

        # Process-based IPC tuples (translator worker)
        if self._proc_queue is not None:
            try:
                while True:
                    msg = self._proc_queue.get_nowait()
                    self._handle_proc_msg(msg)
            except Exception:
                pass

        self.after(16, self._poll_ui_queue)

    def _handle_proc_msg(self, msg: tuple) -> None:
        tag = msg[0]
        if tag == "LOG":
            self._log(msg[1])
        elif tag == "PROGRESS":
            self._set_progress(msg[1], msg[2] if len(msg) > 2 else None)
        elif tag == "DONE":
            self._proc_queue = None
            self._on_translation_done(msg[1])

    def _schedule(self, fn):
        """Enqueue a callable to run on the main thread. Safe from any thread."""
        self._ui_queue.put(fn)

    # ------------------------------------------------------------------
    # GPU acceleration
    # ------------------------------------------------------------------

    def _check_gpu_prompt(self):
        gpu_dir = _gpu_libs_dir()
        key_dll = gpu_dir / _GPU_KEY_DLL
        if key_dll.exists():
            return  # already downloaded
        hw = _hw_config()
        if hw["device"] == "cuda":
            self._show_gpu_dialog()

    def _show_gpu_dialog(self):
        answer = messagebox.askyesno(
            "Aceleracao GPU detectada",
            "Placa de video NVIDIA detectada!\n\n"
            "Deseja baixar o Pacote de Aceleracao de Hardware (~500 MB) "
            "para traduzir significativamente mais rapido?\n\n"
            "O download pode demorar alguns minutos dependendo da sua conexao.",
            icon="question",
        )
        if answer:
            self._start_gpu_download()

    def _start_gpu_download(self):
        self.translate_btn.configure(state="disabled", text="Baixando GPU...")
        self._log("\n--- Baixando Pacote de Aceleracao GPU ---")

        def run():
            success = download_gpu_libs(
                log_fn=lambda msg: self._schedule(lambda m=msg: self._log(m)),
                progress_fn=lambda v, l=None: self._schedule(
                    lambda vv=v, ll=l: self._set_progress(vv, ll)
                ),
            )

            def done():
                self.translate_btn.configure(state="normal", text="Traduzir Agora")
                if success:
                    self._log("GPU pronta! A proxima traducao usara aceleracao por GPU.\n")
                    self._set_progress(0, "Aguardando...")
                else:
                    self._log("Falha no download do pacote GPU. Traduzindo via CPU.\n")
                    self._set_progress(0, "Aguardando...")

            self._schedule(done)

        threading.Thread(target=run, daemon=True).start()

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
            self._log(f"Detectado: {label} — analisando idioma...")
            threading.Thread(
                target=self._detect_lang_async, args=(path, detected), daemon=True
            ).start()
        else:
            self._log("Tipo nao detectado — selecione manualmente.")

    def _detect_lang_async(self, path: Path, game_type: str):
        try:
            texts = sample_game_texts(path, game_type)
            lang_code = detect_source_language(texts)
            if lang_code:
                for name, code in LANGUAGES.items():
                    if code == lang_code:
                        self._schedule(lambda n=name: self.src_var.set(n))
                        self._schedule(
                            lambda c=lang_code: self._log(
                                f"Idioma de origem detectado: {c.upper()} (editavel)"
                            )
                        )
                        return
            self._schedule(lambda: self._log("Idioma nao detectado — selecione manualmente."))
        except Exception:
            pass

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
        now = time.time()
        # Throttle bar updates — skip if too soon, except for anchors 0.0 and 1.0
        skip_bar = (
            value not in (0.0, 1.0)
            and (now - self._last_progress_time) < 0.05
        )
        if label:
            self.status_label.configure(text=label)
        if self._translating and value <= 0:
            if not self._progress_indeterminate:
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start()
                self._progress_indeterminate = True
        else:
            if self._progress_indeterminate:
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate")
                self._progress_indeterminate = False
            if not skip_bar:
                self.progress_bar.set(value)
                self._last_progress_time = now

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
        self.translate_btn.configure(
            state="disabled", text="Traduzindo...", fg_color=_BTN_BUSY
        )
        # Start indeterminate immediately — will switch once real progress arrives
        self._progress_indeterminate = False
        self._set_progress(0, "Iniciando...")
        self._log(f"\n--- Iniciando traducao ({GAME_TYPE_LABELS[type_key]}) ---")
        self._log(f"Caminho: {path}")
        self._log(f"Idiomas: {self.src_var.get()} -> {self.tgt_var.get()}\n")

        self._proc_queue = multiprocessing.Queue()
        p = multiprocessing.Process(
            target=worker_process,
            args=(translator_cls, str(path), src_lang, tgt_lang, self._proc_queue),
            daemon=True,
        )
        p.start()


    def _on_translation_done(self, success: bool):
        self._translating = False
        # Stop indeterminate animation if still running
        if self._progress_indeterminate:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self._progress_indeterminate = False
        if success:
            self.progress_bar.set(1.0)
            self.translate_btn.configure(
                state="normal", text="Traduzir Agora", fg_color=_BTN_SUCCESS
            )
            self.after(2000, self._restore_btn_color)
        else:
            self.translate_btn.configure(
                state="normal", text="Traduzir Agora", fg_color=_BTN_DEFAULT
            )

    def _restore_btn_color(self):
        self.translate_btn.configure(fg_color=_BTN_DEFAULT)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for PyInstaller on Windows
    main()
