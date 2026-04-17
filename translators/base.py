from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional


class BaseTranslator(ABC):
    def __init__(
        self,
        path: Path,
        src_lang: str,
        tgt_lang: str,
        log_fn: Callable = print,
        progress_fn: Optional[Callable] = None,
    ):
        self.path = path
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.log = log_fn
        self._progress_fn = progress_fn

    def set_progress(self, value: float, label: str = None):
        if self._progress_fn:
            self._progress_fn(value, label)

    @abstractmethod
    def translate(self) -> Path:
        """Executa tradução e retorna o caminho do arquivo/pasta gerado."""
        pass
