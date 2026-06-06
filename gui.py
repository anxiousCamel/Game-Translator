#!/usr/bin/env python3
"""Game Translator — Interface gráfica."""
from __future__ import annotations

import json
import locale
import multiprocessing
import queue
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from translators.detector import detect_game_type, GAME_TYPE_LABELS, sample_game_texts
from translators.engine import (
    _hw_config, _gpu_libs_dir, _GPU_KEY_DLL,
    download_gpu_libs, detect_source_language,
)
from translators.twine import TwineTranslator
from translators.renpy import RenpyTranslator
from translators.rpgmaker import RPGMakerTranslator
from translators.unity import UnityTranslator

# ---------------------------------------------------------------------------
# I18n
# ---------------------------------------------------------------------------

APP_TEXTS: dict[str, dict[str, str]] = {
    "pt": {
        "nav_translate":         "Tradução",
        "nav_review":            "Revisão",
        "nav_settings":          "Configurações",
        "app_subtitle":          "Twine  |  RenPy  |  RPGMaker  |  Unity",
        "lbl_game_dir":          "Jogo / Diretório",
        "ph_game_dir":           "Selecione a pasta ou arquivo do jogo...",
        "btn_folder":            "Pasta",
        "btn_file":              "Arquivo",
        "lbl_type":              "Tipo",
        "lbl_engine":            "Motor",
        "engine_local":          "Local / Offline",
        "engine_google":         "Google (Online)",
        "lbl_src_lang":          "Origem",
        "lbl_tgt_lang":          "Destino",
        "type_auto":             "Auto-detectar",
        "lbl_log":               "Log",
        "btn_clear":             "Limpar",
        "status_waiting":        "Aguardando...",
        "btn_translate":         "Traduzir Agora",
        "btn_finished":          "Finalizado!",
        "btn_cancel":            "Cancelar",
        "log_abort":             "\nTradução abortada pelo usuário!",
        "btn_translating":       "Traduzindo...",
        "btn_downloading_gpu":   "Baixando GPU...",
        "status_starting":       "Iniciando...",
        "gpu_dialog_title":      "Aceleração GPU",
        "gpu_dialog_msg": (
            "Placa NVIDIA detectada!\n\n"
            "Baixar o Pacote GPU (~500 MB) para traduzir muito mais rápido?\n\n"
            "O download pode levar alguns minutos."
        ),
        "log_gpu_start":         "\n--- Baixando Pacote GPU ---",
        "log_gpu_ok":            "GPU pronta! Próxima tradução usará GPU.\n",
        "log_gpu_fail":          "Falha no download do pacote GPU. Usando CPU.\n",
        "log_detected":          "Detectado: {label} — analisando idioma...",
        "log_type_unknown":      "Tipo não detectado — selecione manualmente.",
        "log_lang_detected":     "Idioma detectado: {code} (editável)",
        "log_lang_unknown":      "Idioma não detectado — selecione manualmente.",
        "log_no_path":           "Selecione o jogo antes de traduzir.",
        "log_path_missing":      "Caminho não encontrado: {path}",
        "log_type_not_detected": "Não foi possível detectar o tipo de jogo.\nSelecione manualmente.",
        "log_same_lang":         "Idioma de origem e destino são iguais.",
        "log_trans_start":       "\n--- Iniciando tradução ({label}) ---",
        "log_path":              "Caminho: {path}",
        "log_langs":             "Idiomas: {src} → {tgt}\n",
        "screen_review_title":   "Revisão de Textos",
        "screen_review_hint":    "Os cartões de revisão aparecerão aqui após uma tradução.",
        "review_btn_google":      "Traduzir (Google)",
        "review_google_busy":    "Traduzindo...",
        "review_load_more":      "Carregar mais {n} resultados...",
        "review_btn_load":       "Carregar Cache",
        "review_ph_search":      "Buscar original ou tradução...",
        "review_btn_search":     "Buscar",
        "review_btn_save":       "Salvar Alterações",
        "review_status_loaded":  "Arquivo: {name}  |  {n} entradas",
        "review_status_showing": "Mostrando {n} de {total} (máx. 50)",
        "review_no_path":        "Selecione o jogo na aba Tradução primeiro.",
        "review_no_cache":       "Nenhum cache encontrado em: {path}",
        "review_load_err":       "Erro ao carregar: {err}",
        "review_saved":          "✔ {n} entradas salvas em {name}.",
        "review_save_err":       "Erro ao salvar: {err}",
        "review_redo_btn":       "✨ Refazer com IA",
        "review_redo_busy":      "Pensando...",
        "review_no_results":     "Nenhum resultado para \"{q}\".",
        "settings_ai_section":   "Configuração de IA Avançada (LiteLLM)",
        "settings_ai_model":     "Modelo",
        "settings_ai_model_ph":  "Ex: gemini/gemini-1.5-flash, gpt-4o-mini, ollama/llama3",
        "settings_ai_key":       "API Key",
        "settings_ai_url":       "Base URL (Opcional)",
        "settings_ai_url_ph":    "Ex: http://localhost:11434 (Ollama)",
        "settings_ai_save":           "Salvar Configuração de IA",
        "settings_ai_saved":          "✔ Configuração salva.",
        "settings_app_prefs":         "Preferências do Aplicativo",
        "settings_ai_preset":         "Provedor Rápido",
        "settings_ai_preset_custom":  "Personalizado",
        "settings_ai_key_local_ph":   "Não necessário para execução local",
        "settings_ai_model_hint":     "Ex: gemini/gemini-1.5-flash  •  gpt-4o-mini  •  openai/meta/llama-3.1-70b-instruct",
        "settings_ai_url_hint":       "Ex: https://integrate.api.nvidia.com/v1  (NVIDIA NIM / Groq / Ollama)",
        "settings_ai_help_title":     "Como configurar a IA",
        "settings_ai_help_msg": (
            "Como preencher os campos:\n\n"
            "• Google Gemini: Modelo 'gemini/gemini-1.5-flash'. Cole sua chave. Base URL vazia.\n"
            "• OpenAI: Modelo 'gpt-4o-mini'. Cole sua chave. Base URL vazia.\n"
            "• Anthropic (Claude): Modelo 'claude-3-5-sonnet-20240620'. Cole sua chave. Base URL vazia.\n"
            "• NVIDIA NIM: Modelo 'openai/meta/llama-3.1-70b-instruct'. Cole sua chave e coloque "
            "'https://integrate.api.nvidia.com/v1' na Base URL.\n"
            "• Ollama (Local): Modelo 'ollama/llama3'. Base URL 'http://localhost:11434'. Sem API Key."
        ),
        "settings_ai_test":           "Testar Conexão",
        "settings_ai_testing":        "Testando...",
        "settings_test_ok_title":     "Sucesso ✔",
        "settings_test_ok_msg":       "Conexão estabelecida! A IA respondeu corretamente.",
        "settings_test_fail_title":   "Erro de Conexão",
        "settings_test_fail_msg":     "Falha ao conectar:\n{err}",
        "warn_no_apikey_title":  "API Key Ausente",
        "warn_no_apikey_msg":    "API Key não encontrada!\n\nVá até 'Configurações' e insira sua chave para usar os recursos avançados de IA.",
        "warn_no_model_title":   "Modelo não configurado",
        "warn_no_model_msg":     "Configure o nome do modelo na aba 'Configurações' antes de usar a IA.",
        "screen_settings_title": "Configurações",
        "settings_lang":         "Idioma do aplicativo",
        "settings_theme":        "Tema",
        "settings_restart_note": "Reinicie o app para aplicar o novo idioma.",
        "theme_dark":            "Escuro",
        "theme_light":           "Claro",
        "theme_system":          "Sistema",
    },
    "en": {
        "nav_translate":         "Translation",
        "nav_review":            "Review",
        "nav_settings":          "Settings",
        "app_subtitle":          "Twine  |  RenPy  |  RPGMaker  |  Unity",
        "lbl_game_dir":          "Game / Directory",
        "ph_game_dir":           "Select the game folder or file...",
        "btn_folder":            "Folder",
        "btn_file":              "File",
        "lbl_type":              "Type",
        "lbl_engine":            "Engine",
        "engine_local":          "Local / Offline",
        "engine_google":         "Google (Online)",
        "lbl_src_lang":          "Source",
        "lbl_tgt_lang":          "Target",
        "type_auto":             "Auto-detect",
        "lbl_log":               "Log",
        "btn_clear":             "Clear",
        "status_waiting":        "Waiting...",
        "btn_translate":         "Translate Now",
        "btn_finished":          "Done!",
        "btn_cancel":            "Cancel",
        "log_abort":             "\nTranslation aborted by user!",
        "btn_translating":       "Translating...",
        "btn_downloading_gpu":   "Downloading GPU...",
        "status_starting":       "Starting...",
        "gpu_dialog_title":      "GPU Acceleration",
        "gpu_dialog_msg": (
            "NVIDIA GPU detected!\n\n"
            "Download the GPU Package (~500 MB) to translate much faster?\n\n"
            "The download may take a few minutes."
        ),
        "log_gpu_start":         "\n--- Downloading GPU Package ---",
        "log_gpu_ok":            "GPU ready! Next translation will use GPU.\n",
        "log_gpu_fail":          "GPU package download failed. Using CPU.\n",
        "log_detected":          "Detected: {label} — analyzing language...",
        "log_type_unknown":      "Type not detected — select manually.",
        "log_lang_detected":     "Language detected: {code} (editable)",
        "log_lang_unknown":      "Language not detected — select manually.",
        "log_no_path":           "Select the game before translating.",
        "log_path_missing":      "Path not found: {path}",
        "log_type_not_detected": "Could not detect game type.\nSelect manually.",
        "log_same_lang":         "Source and target languages are the same.",
        "log_trans_start":       "\n--- Starting translation ({label}) ---",
        "log_path":              "Path: {path}",
        "log_langs":             "Languages: {src} → {tgt}\n",
        "screen_review_title":   "Text Review",
        "screen_review_hint":    "Review cards will appear here after a translation.",
        "review_btn_google":      "Translate (Google)",
        "review_google_busy":    "Translating...",
        "review_load_more":      "Load {n} more results...",
        "review_btn_load":       "Load Cache",
        "review_ph_search":      "Search original or translation...",
        "review_btn_search":     "Search",
        "review_btn_save":       "Save Changes",
        "review_status_loaded":  "File: {name}  |  {n} entries",
        "review_status_showing": "Showing {n} of {total} (max 50)",
        "review_no_path":        "Select the game in the Translation tab first.",
        "review_no_cache":       "No cache found in: {path}",
        "review_load_err":       "Error loading: {err}",
        "review_saved":          "✔ {n} entries saved to {name}.",
        "review_save_err":       "Error saving: {err}",
        "review_redo_btn":       "✨ Redo with AI",
        "review_redo_busy":      "Thinking...",
        "review_no_results":     "No results for \"{q}\".",
        "settings_ai_section":   "Advanced AI Config (LiteLLM)",
        "settings_ai_model":     "Model",
        "settings_ai_model_ph":  "Ex: gemini/gemini-1.5-flash, gpt-4o-mini, ollama/llama3",
        "settings_ai_key":       "API Key",
        "settings_ai_url":       "Base URL (Optional)",
        "settings_ai_url_ph":    "Ex: http://localhost:11434 (Ollama)",
        "settings_ai_save":           "Save AI Config",
        "settings_ai_saved":          "✔ Configuration saved.",
        "settings_app_prefs":         "Application Preferences",
        "settings_ai_preset":         "Quick Provider",
        "settings_ai_preset_custom":  "Custom",
        "settings_ai_key_local_ph":   "Not required for local execution",
        "settings_ai_model_hint":     "Ex: gemini/gemini-1.5-flash  •  gpt-4o-mini  •  openai/meta/llama-3.1-70b-instruct",
        "settings_ai_url_hint":       "Ex: https://integrate.api.nvidia.com/v1  (NVIDIA NIM / Groq / Ollama)",
        "settings_ai_help_title":     "How to configure the AI",
        "settings_ai_help_msg": (
            "How to fill in the fields:\n\n"
            "• Google Gemini: Model 'gemini/gemini-1.5-flash'. Paste your key. Leave Base URL empty.\n"
            "• OpenAI: Model 'gpt-4o-mini'. Paste your key. Leave Base URL empty.\n"
            "• Anthropic (Claude): Model 'claude-3-5-sonnet-20240620'. Paste your key. Leave Base URL empty.\n"
            "• NVIDIA NIM: Model 'openai/meta/llama-3.1-70b-instruct'. Paste your key and set "
            "'https://integrate.api.nvidia.com/v1' as Base URL.\n"
            "• Ollama (Local): Model 'ollama/llama3'. Base URL 'http://localhost:11434'. No API Key needed."
        ),
        "settings_ai_test":           "Test Connection",
        "settings_ai_testing":        "Testing...",
        "settings_test_ok_title":     "Success ✔",
        "settings_test_ok_msg":       "Connection established! The AI responded correctly.",
        "settings_test_fail_title":   "Connection Error",
        "settings_test_fail_msg":     "Failed to connect:\n{err}",
        "warn_no_apikey_title":  "API Key Missing",
        "warn_no_apikey_msg":    "API Key not found!\n\nGo to 'Settings' and enter your key to use advanced AI features.",
        "warn_no_model_title":   "Model Not Configured",
        "warn_no_model_msg":     "Configure a model name in 'Settings' before using AI.",
        "screen_settings_title": "Settings",
        "settings_lang":         "App language",
        "settings_theme":        "Theme",
        "settings_restart_note": "Restart the app to apply the language change.",
        "theme_dark":            "Dark",
        "theme_light":           "Light",
        "theme_system":          "System",
    },
    "ru": {
        "nav_translate":         "Перевод",
        "nav_review":            "Проверка",
        "nav_settings":          "Настройки",
        "app_subtitle":          "Twine  |  RenPy  |  RPGMaker  |  Unity",
        "btn_translate":         "Перевести",
        "btn_finished":          "Готово!",
        "btn_cancel":            "Отмена",
        "btn_translating":       "Переводится...",
        "status_waiting":        "Ожидание...",
        "status_starting":       "Запуск...",
        "lbl_game_dir":          "Папка / Файл игры",
        "ph_game_dir":           "Выберите папку или файл игры...",
        "btn_folder":            "Папка",
        "btn_file":              "Файл",
        "lbl_type":              "Тип",
        "lbl_engine":            "Движок",
        "lbl_src_lang":          "Источник",
        "lbl_tgt_lang":          "Назначение",
        "lbl_log":               "Журнал",
        "btn_clear":             "Очистить",
        "screen_review_title":   "Проверка текстов",
        "screen_settings_title": "Настройки",
        "settings_app_prefs":    "Параметры приложения",
        "settings_lang":         "Язык приложения",
        "settings_restart_note": "Перезапустите приложение для применения изменений.",
        "settings_ai_section":   "Настройка ИИ (LiteLLM)",
        "settings_ai_preset":    "Провайдер",
        "settings_ai_model":     "Модель",
        "settings_ai_key":       "API-ключ",
        "settings_ai_url":       "Базовый URL (опционально)",
        "settings_ai_save":      "Сохранить настройки ИИ",
        "settings_ai_saved":     "✔ Сохранено.",
        "settings_ai_test":      "Тест соединения",
        "settings_ai_testing":   "Тестирование...",
        "settings_test_ok_msg":  "Соединение установлено! ИИ ответил.",
        "review_btn_load":       "Загрузить кэш",
        "review_btn_search":     "Поиск",
        "review_btn_save":       "Сохранить",
        "review_btn_google":     "Перевод (Google)",
        "review_redo_btn":       "✨ ИИ-перевод",
        "review_load_more":      "Загрузить ещё {n}...",
    },
    "ja": {
        "nav_translate":         "翻訳",
        "nav_review":            "確認",
        "nav_settings":          "設定",
        "app_subtitle":          "Twine  |  RenPy  |  RPGMaker  |  Unity",
        "btn_translate":         "今すぐ翻訳",
        "btn_finished":          "完了！",
        "btn_cancel":            "キャンセル",
        "btn_translating":       "翻訳中...",
        "status_waiting":        "待機中...",
        "status_starting":       "開始中...",
        "lbl_game_dir":          "ゲームフォルダ / ファイル",
        "ph_game_dir":           "ゲームのフォルダかファイルを選択...",
        "btn_folder":            "フォルダ",
        "btn_file":              "ファイル",
        "lbl_type":              "タイプ",
        "lbl_engine":            "エンジン",
        "lbl_src_lang":          "原語",
        "lbl_tgt_lang":          "対象言語",
        "lbl_log":               "ログ",
        "btn_clear":             "クリア",
        "screen_review_title":   "テキスト確認",
        "screen_settings_title": "設定",
        "settings_app_prefs":    "アプリの設定",
        "settings_lang":         "アプリ言語",
        "settings_restart_note": "言語変更を適用するにはアプリを再起動してください。",
        "settings_ai_section":   "AI設定 (LiteLLM)",
        "settings_ai_preset":    "プロバイダー",
        "settings_ai_model":     "モデル",
        "settings_ai_key":       "APIキー",
        "settings_ai_url":       "ベースURL (任意)",
        "settings_ai_save":      "AI設定を保存",
        "settings_ai_saved":     "✔ 保存しました。",
        "settings_ai_test":      "接続テスト",
        "settings_ai_testing":   "テスト中...",
        "settings_test_ok_msg":  "接続成功！AIが応答しました。",
        "review_btn_load":       "キャッシュ読込",
        "review_btn_search":     "検索",
        "review_btn_save":       "保存",
        "review_btn_google":     "Google翻訳",
        "review_redo_btn":       "✨ AIで再翻訳",
        "review_load_more":      "さらに{n}件読み込む...",
    },
    "ko": {
        "nav_translate":         "번역",
        "nav_review":            "검토",
        "nav_settings":          "설정",
        "app_subtitle":          "Twine  |  RenPy  |  RPGMaker  |  Unity",
        "btn_translate":         "지금 번역",
        "btn_finished":          "완료！",
        "btn_cancel":            "취소",
        "btn_translating":       "번역 중...",
        "status_waiting":        "대기 중...",
        "status_starting":       "시작 중...",
        "lbl_game_dir":          "게임 폴더 / 파일",
        "ph_game_dir":           "게임 폴더나 파일을 선택하세요...",
        "btn_folder":            "폴더",
        "btn_file":              "파일",
        "lbl_type":              "유형",
        "lbl_engine":            "엔진",
        "lbl_src_lang":          "원본 언어",
        "lbl_tgt_lang":          "대상 언어",
        "lbl_log":               "로그",
        "btn_clear":             "지우기",
        "screen_review_title":   "텍스트 검토",
        "screen_settings_title": "설정",
        "settings_app_prefs":    "앱 설정",
        "settings_lang":         "앱 언어",
        "settings_restart_note": "언어 변경 사항을 적용하려면 앱을 재시작하세요.",
        "settings_ai_section":   "AI 설정 (LiteLLM)",
        "settings_ai_preset":    "제공자",
        "settings_ai_model":     "모델",
        "settings_ai_key":       "API 키",
        "settings_ai_url":       "기본 URL (선택사항)",
        "settings_ai_save":      "AI 설정 저장",
        "settings_ai_saved":     "✔ 저장되었습니다.",
        "settings_ai_test":      "연결 테스트",
        "settings_ai_testing":   "테스트 중...",
        "settings_test_ok_msg":  "연결 성공！AI가 응답했습니다.",
        "review_btn_load":       "캐시 불러오기",
        "review_btn_search":     "검색",
        "review_btn_save":       "저장",
        "review_btn_google":     "Google 번역",
        "review_redo_btn":       "✨ AI 재번역",
        "review_load_more":      "{n}개 더 불러오기...",
    },
}


def _system_lang() -> str:
    try:
        code = (locale.getdefaultlocale()[0] or "").split("_")[0].lower()
        return code if code in APP_TEXTS else "en"
    except Exception:
        return "en"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path(__file__).parent / ".app_config.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    try:
        _CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


_cfg = _load_config()
APP_LANG: str = _cfg.get("lang") or _system_lang()


def _t(key: str, **kwargs) -> str:
    lang_dict = APP_TEXTS.get(APP_LANG, {})
    text = lang_dict.get(key) or APP_TEXTS.get("en", {}).get(key) or key
    return text.format(**kwargs) if kwargs else text


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

_LANG_NAMES: dict[str, dict[str, str]] = {
    "pt": {
        "en": "Inglês",
        "pt": "Português (BR)",
        "es": "Espanhol",
        "fr": "Francês",
        "de": "Alemão",
        "it": "Italiano",
        "ru": "Russo",
        "ja": "Japonês",
        "zh": "Chinês",
        "ko": "Coreano",
    },
    "en": {
        "en": "English",
        "pt": "Portuguese (BR)",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "it": "Italian",
        "ru": "Russian",
        "ja": "Japanese",
        "zh": "Chinese",
        "ko": "Korean",
    },
}

# code → display name in current UI language
_LANG_DISPLAY: dict[str, str] = _LANG_NAMES.get(APP_LANG, _LANG_NAMES["pt"])
# display name → code (used for lookups in _start_translation)
LANGUAGES: dict[str, str] = {v: k for k, v in _LANG_DISPLAY.items()}

GAME_TYPES: dict[str, str | None] = {
    _t("type_auto"):     None,
    "Twine / SugarCube": "twine",
    "RenPy":             "renpy",
    "RPGMaker MV/MZ":    "rpgmaker",
    "Unity":             "unity",
}

TRANSLATORS = {
    "twine":    TwineTranslator,
    "renpy":    RenpyTranslator,
    "rpgmaker": RPGMakerTranslator,
    "unity":    UnityTranslator,
}

# ---------------------------------------------------------------------------
# Worker process (separate OS process — zero GIL sharing with UI)
# ---------------------------------------------------------------------------

def worker_process(
    translator_cls,
    path_str: str,
    src_lang: str,
    tgt_lang: str,
    engine: str,
    ipc_queue: "multiprocessing.Queue[tuple]",
) -> None:
    """
    Runs the translator in an isolated process.
    IPC tags: ("LOG", msg) | ("PROGRESS", val, lbl) | ("DONE", success)
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
            engine=engine,
        )
        output = translator.translate()
        ipc_queue.put(("LOG", f"\nConcluído! Saída:\n  {output}"))
        ipc_queue.put(("DONE", True))
    except Exception as exc:
        ipc_queue.put(("LOG", f"\nErro: {exc}"))
        ipc_queue.put(("DONE", False))


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_BTN_DEFAULT = "#1F6AA5"
_BTN_HOVER   = "#2980B9"
_BTN_BUSY    = "#3D3D3D"
_BTN_SUCCESS = "#3EA6FF"
_SIDEBAR_BG  = ("gray90", "gray17")
_NAV_HOVER   = ("gray80", "gray27")

# AI provider presets: name → (model, base_url, api_key_required)
_AI_PRESETS: dict[str, tuple] = {
    "Google Gemini":      ("gemini/gemini-1.5-flash", "", True),
    "OpenAI":             ("gpt-4o-mini", "", True),
    "Anthropic (Claude)": ("claude-3-5-sonnet-20240620", "", True),
    "NVIDIA NIM":         ("openai/meta/llama-3.1-70b-instruct", "https://integrate.api.nvidia.com/v1", True),
    "Ollama (Local)":     ("ollama/llama3", "http://localhost:11434", False),
}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Game Translator")
        self.geometry("940x680")
        self.minsize(760, 560)

        self._translating = False
        self._progress_indeterminate = False
        self._last_progress_time = 0.0
        self._status_base: str = ""
        self._ellipsis_active = False
        self._ellipsis_step = 0
        self._ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._proc_queue: multiprocessing.Queue | None = None
        self._translate_proc: multiprocessing.Process | None = None
        self._screens: dict[str, ctk.CTkFrame] = {}
        self._nav_btns: dict[str, ctk.CTkButton] = {}

        # Review screen state
        self._review_cache: dict = {}
        self._review_path: Path | None = None
        self._review_textboxes: dict[str, ctk.CTkTextbox] = {}
        self._review_cards: list[ctk.CTkFrame] = []
        self._itens_filtrados: list = []
        self._itens_exibidos: int = 0

        # Settings AI help panel state
        self._help_visible: bool = False

        self._build_ui()
        self._show_screen("translate")
        self.after(800, self._check_gpu_prompt)
        self._poll_ui_queue()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()

        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.grid(row=0, column=1, sticky="nsew")
        self._content.grid_columnconfigure(0, weight=1)
        self._content.grid_rowconfigure(0, weight=1)

        self._screens["translate"] = self._build_translate_screen(self._content)
        self._screens["review"]    = self._build_review_screen(self._content)
        self._screens["settings"]  = self._build_settings_screen(self._content)

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=210, corner_radius=0, fg_color=_SIDEBAR_BG)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(0, weight=1)
        sb.grid_rowconfigure(10, weight=1)

        ctk.CTkLabel(
            sb,
            text="Game\nTranslator",
            font=ctk.CTkFont(size=22, weight="bold"),
            justify="center",
        ).grid(row=0, column=0, padx=20, pady=(28, 24))

        for idx, (key, text_key) in enumerate([
            ("translate", "nav_translate"),
            ("review",    "nav_review"),
            ("settings",  "nav_settings"),
        ]):
            btn = ctk.CTkButton(
                sb,
                text=_t(text_key),
                anchor="w",
                height=40,
                corner_radius=8,
                fg_color="transparent",
                hover_color=_NAV_HOVER,
                font=ctk.CTkFont(size=14),
                command=lambda k=key: self._show_screen(k),
            )
            btn.grid(row=idx + 1, column=0, padx=10, pady=3, sticky="ew")
            self._nav_btns[key] = btn

        ctk.CTkLabel(
            sb, text="v1.0.0", text_color="gray50", font=ctk.CTkFont(size=10)
        ).grid(row=11, column=0, pady=(0, 14))

    def _show_screen(self, key: str):
        for frame in self._screens.values():
            frame.grid_forget()
        self._screens[key].grid(row=0, column=0, sticky="nsew")
        for k, btn in self._nav_btns.items():
            btn.configure(fg_color=(_BTN_DEFAULT if k == key else "transparent"))

    # ── Translation screen ───────────────────────────────────────────

    def _build_translate_screen(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        # Screen header
        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=24, pady=(20, 8), sticky="ew")
        ctk.CTkLabel(hdr, text=_t("nav_translate"), font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkLabel(
            hdr, text=_t("app_subtitle"), font=ctk.CTkFont(size=11), text_color="gray"
        ).pack(side="left", padx=(12, 0), pady=(4, 0))

        # Path selection card
        sel = ctk.CTkFrame(frame)
        sel.grid(row=1, column=0, padx=24, pady=(0, 8), sticky="ew")
        sel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(sel, text=_t("lbl_game_dir"), font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=14, pady=(10, 4), sticky="w"
        )
        self.path_entry = ctk.CTkEntry(sel, placeholder_text=_t("ph_game_dir"))
        self.path_entry.grid(row=1, column=0, padx=(14, 6), pady=(0, 12), sticky="ew")
        ctk.CTkButton(sel, text=_t("btn_folder"), width=80, command=self._select_folder).grid(
            row=1, column=1, padx=2, pady=(0, 12)
        )
        ctk.CTkButton(sel, text=_t("btn_file"), width=80, command=self._select_file).grid(
            row=1, column=2, padx=(2, 14), pady=(0, 12)
        )

        # Options card: type + language pair
        opt = ctk.CTkFrame(frame)
        opt.grid(row=2, column=0, padx=24, pady=(0, 8), sticky="ew")
        opt.grid_columnconfigure(0, weight=0)
        opt.grid_columnconfigure(1, weight=0)
        opt.grid_columnconfigure(2, weight=1)

        # Type dropdown
        ctk.CTkLabel(opt, text=_t("lbl_type"), font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=(14, 8), pady=(10, 2), sticky="w"
        )
        self.type_var = ctk.StringVar(value=_t("type_auto"))
        ctk.CTkOptionMenu(opt, variable=self.type_var, values=list(GAME_TYPES.keys()), width=150).grid(
            row=1, column=0, padx=(14, 8), pady=(0, 12), sticky="w"
        )

        # Engine dropdown
        ctk.CTkLabel(opt, text=_t("lbl_engine"), font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=1, padx=(0, 12), pady=(10, 2), sticky="w"
        )
        self.engine_var = ctk.StringVar(value=_t("engine_local"))
        ctk.CTkOptionMenu(
            opt,
            variable=self.engine_var,
            values=[_t("engine_local"), _t("engine_google")],
            width=160,
        ).grid(row=1, column=1, padx=(0, 12), pady=(0, 12), sticky="w")

        # Language pair sub-frame (src ➔ tgt)
        lf = ctk.CTkFrame(opt, fg_color="transparent")
        lf.grid(row=0, column=2, rowspan=2, padx=(0, 14), pady=(10, 12), sticky="ew")
        lf.grid_columnconfigure(0, weight=1)
        lf.grid_columnconfigure(1, weight=0)
        lf.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(lf, text=_t("lbl_src_lang"), font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 2)
        )
        ctk.CTkLabel(lf, text=_t("lbl_tgt_lang"), font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=2, sticky="w", pady=(0, 2)
        )
        self.src_var = ctk.StringVar(value=_LANG_DISPLAY.get("en", "Inglês"))
        ctk.CTkOptionMenu(lf, variable=self.src_var, values=list(LANGUAGES.keys())).grid(
            row=1, column=0, sticky="ew"
        )
        ctk.CTkLabel(lf, text="➔", font=ctk.CTkFont(size=20)).grid(row=1, column=1, padx=10)
        self.tgt_var = ctk.StringVar(value=_LANG_DISPLAY.get("pt", "Português (BR)"))
        ctk.CTkOptionMenu(lf, variable=self.tgt_var, values=list(LANGUAGES.keys())).grid(
            row=1, column=2, sticky="ew"
        )

        # Log card
        log_card = ctk.CTkFrame(frame)
        log_card.grid(row=3, column=0, padx=24, pady=(0, 8), sticky="nsew")
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)

        log_hdr = ctk.CTkFrame(log_card, fg_color="transparent")
        log_hdr.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        ctk.CTkLabel(log_hdr, text=_t("lbl_log"), font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(
            log_hdr, text=_t("btn_clear"), width=60, height=22,
            font=ctk.CTkFont(size=11), command=self._clear_log
        ).pack(side="right")

        self.log_box = ctk.CTkTextbox(log_card, state="disabled", font=ctk.CTkFont(size=12))
        self.log_box.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="nsew")

        # Footer: status + progress + cancel/translate buttons
        ftr = ctk.CTkFrame(frame, fg_color="transparent")
        ftr.grid(row=4, column=0, padx=24, pady=(0, 20), sticky="ew")
        ftr.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            ftr, text=_t("status_waiting"), text_color="gray", font=ctk.CTkFont(size=12)
        )
        self.status_label.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.progress_bar = ctk.CTkProgressBar(ftr, height=10)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        btn_row = ctk.CTkFrame(ftr, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew")
        btn_row.grid_columnconfigure(1, weight=1)

        self.cancel_btn = ctk.CTkButton(
            btn_row,
            text=_t("btn_cancel"),
            width=120,
            height=46,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#A51F1F",
            hover_color="#C0392B",
            state="disabled",
            command=self._cancel_translation,
        )
        self.cancel_btn.grid(row=0, column=0, padx=(0, 8))

        self.translate_btn = ctk.CTkButton(
            btn_row,
            text=_t("btn_translate"),
            height=46,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=_BTN_DEFAULT,
            hover_color=_BTN_HOVER,
            command=self._start_translation,
        )
        self.translate_btn.grid(row=0, column=1, sticky="ew")

        return frame

    # ── Review screen ────────────────────────────────────────────────

    def _build_review_screen(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        # Header
        ctk.CTkLabel(
            frame, text=_t("screen_review_title"), font=ctk.CTkFont(size=20, weight="bold")
        ).grid(row=0, column=0, padx=24, pady=(24, 8), sticky="w")

        # Controls bar
        ctrl = ctk.CTkFrame(frame)
        ctrl.grid(row=1, column=0, padx=24, pady=(0, 4), sticky="ew")

        ctk.CTkButton(
            ctrl, text=_t("review_btn_load"), width=130,
            command=self._load_review_cache,
        ).pack(side="left", padx=(12, 6), pady=10)

        self._review_search_var = ctk.StringVar()
        search_entry = ctk.CTkEntry(
            ctrl, textvariable=self._review_search_var,
            placeholder_text=_t("review_ph_search"),
        )
        search_entry.pack(side="left", expand=True, fill="x", padx=6, pady=10)
        search_entry.bind(
            "<Return>",
            lambda e: self._render_review_cards(self._review_search_var.get()),
        )

        ctk.CTkButton(
            ctrl, text=_t("review_btn_search"), width=80,
            command=lambda: self._render_review_cards(self._review_search_var.get()),
        ).pack(side="left", padx=6, pady=10)

        ctk.CTkButton(
            ctrl, text=_t("review_btn_save"), width=150,
            fg_color="#27AE60", hover_color="#219A52",
            command=self._save_review,
        ).pack(side="left", padx=(6, 12), pady=10)

        # Status line
        self._review_status_lbl = ctk.CTkLabel(
            frame, text=_t("screen_review_hint"),
            text_color="gray", font=ctk.CTkFont(size=11),
        )
        self._review_status_lbl.grid(row=2, column=0, padx=28, pady=(0, 2), sticky="w")

        # Scrollable card area
        self._review_scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self._review_scroll.grid(row=3, column=0, padx=24, pady=(0, 16), sticky="nsew")
        self._review_scroll.grid_columnconfigure(0, weight=1)

        return frame

    # ── Settings screen ──────────────────────────────────────────────

    def _build_settings_screen(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)

        # ── Title ──────────────────────────────────────────────────────
        ctk.CTkLabel(
            frame, text=_t("screen_settings_title"), font=ctk.CTkFont(size=20, weight="bold")
        ).grid(row=0, column=0, padx=24, pady=(24, 8), sticky="w")

        # ── App preferences (language) ──────────────────────────────────
        ctk.CTkLabel(
            frame, text=_t("settings_app_prefs"), font=ctk.CTkFont(size=13, weight="bold"),
            text_color="gray",
        ).grid(row=1, column=0, padx=24, pady=(0, 4), sticky="w")

        app_card = ctk.CTkFrame(frame)
        app_card.grid(row=2, column=0, padx=24, sticky="ew")
        app_card.grid_columnconfigure(1, weight=0)

        ctk.CTkLabel(app_card, text=_t("settings_lang"), font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=(16, 12), pady=(16, 6), sticky="w"
        )
        _lang_display = {"pt": "Português", "en": "English", "ru": "Русский", "ja": "日本語", "ko": "한국어"}
        lang_var = ctk.StringVar(value=_lang_display.get(APP_LANG, "English"))
        ctk.CTkOptionMenu(
            app_card, variable=lang_var, values=list(_lang_display.values()), width=160,
            command=lambda v: self._change_lang(v, _lang_display),
        ).grid(row=0, column=1, padx=(0, 16), pady=(16, 6))

        self._restart_note = ctk.CTkLabel(
            app_card, text="", text_color="gray", font=ctk.CTkFont(size=11)
        )
        self._restart_note.grid(row=1, column=0, columnspan=2, padx=16, pady=(0, 12), sticky="w")

        # ── AI section header (title + [?] help button) ─────────────────
        ai_hdr = ctk.CTkFrame(frame, fg_color="transparent")
        ai_hdr.grid(row=3, column=0, padx=24, pady=(18, 4), sticky="ew")

        ctk.CTkLabel(
            ai_hdr, text=_t("settings_ai_section"), font=ctk.CTkFont(size=13, weight="bold"),
            text_color="gray",
        ).pack(side="left")
        ctk.CTkButton(
            ai_hdr, text="[?]", width=32, height=26, font=ctk.CTkFont(size=11),
            fg_color="#3D3D3D", hover_color="#555568",
            command=self._show_ai_help,
        ).pack(side="left", padx=(10, 0))

        # ── AI config card ──────────────────────────────────────────────
        ai_card = ctk.CTkFrame(frame)
        ai_card.grid(row=4, column=0, padx=24, pady=(0, 16), sticky="ew")
        ai_card.grid_columnconfigure(1, weight=1)

        _ai_cfg = _load_config()
        _saved_model = _ai_cfg.get("ai_model", "")
        _current_preset = next(
            (name for name, (m, _, _) in _AI_PRESETS.items() if m == _saved_model),
            _t("settings_ai_preset_custom"),
        )

        # Provider preset row
        ctk.CTkLabel(ai_card, text=_t("settings_ai_preset"), font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=(16, 12), pady=(16, 6), sticky="w"
        )
        _preset_choices = list(_AI_PRESETS.keys()) + [_t("settings_ai_preset_custom")]
        self._ai_preset_var = ctk.StringVar(value=_current_preset)
        ctk.CTkOptionMenu(
            ai_card, variable=self._ai_preset_var, values=_preset_choices, width=200,
            command=self._preset_changed,
        ).grid(row=0, column=1, padx=(0, 16), pady=(16, 6), sticky="w")

        # Model entry + hint
        ctk.CTkLabel(ai_card, text=_t("settings_ai_model"), font=ctk.CTkFont(weight="bold")).grid(
            row=1, column=0, padx=(16, 12), pady=(6, 2), sticky="w"
        )
        self._ai_model_var = ctk.StringVar(value=_saved_model)
        ctk.CTkEntry(
            ai_card, textvariable=self._ai_model_var,
            placeholder_text=_t("settings_ai_model_ph"),
        ).grid(row=1, column=1, padx=(0, 16), pady=(6, 2), sticky="ew")
        ctk.CTkLabel(
            ai_card, text=_t("settings_ai_model_hint"),
            text_color="#8A8A93", font=ctk.CTkFont(size=10), justify="left",
        ).grid(row=2, column=1, padx=(0, 16), pady=(0, 6), sticky="w")

        # API Key entry (ref saved for enable/disable)
        ctk.CTkLabel(ai_card, text=_t("settings_ai_key"), font=ctk.CTkFont(weight="bold")).grid(
            row=3, column=0, padx=(16, 12), pady=(4, 6), sticky="w"
        )
        self._ai_key_var = ctk.StringVar(value=_ai_cfg.get("ai_api_key", ""))
        _ollama_active = (_current_preset == "Ollama (Local)")
        self._ai_key_entry = ctk.CTkEntry(
            ai_card, textvariable=self._ai_key_var,
            placeholder_text=_t("settings_ai_key_local_ph") if _ollama_active else "sk-...",
            show="*",
            state="disabled" if _ollama_active else "normal",
        )
        self._ai_key_entry.grid(row=3, column=1, padx=(0, 16), pady=(4, 6), sticky="ew")

        # Base URL entry + hint
        ctk.CTkLabel(ai_card, text=_t("settings_ai_url"), font=ctk.CTkFont(weight="bold")).grid(
            row=4, column=0, padx=(16, 12), pady=(4, 2), sticky="w"
        )
        self._ai_url_var = ctk.StringVar(value=_ai_cfg.get("ai_base_url", ""))
        ctk.CTkEntry(
            ai_card, textvariable=self._ai_url_var,
            placeholder_text=_t("settings_ai_url_ph"),
        ).grid(row=4, column=1, padx=(0, 16), pady=(4, 2), sticky="ew")
        ctk.CTkLabel(
            ai_card, text=_t("settings_ai_url_hint"),
            text_color="#8A8A93", font=ctk.CTkFont(size=10), justify="left",
        ).grid(row=5, column=1, padx=(0, 16), pady=(0, 8), sticky="w")

        # Status note
        self._ai_save_note = ctk.CTkLabel(
            ai_card, text="", text_color="gray", font=ctk.CTkFont(size=11)
        )
        self._ai_save_note.grid(row=6, column=0, columnspan=2, padx=16, pady=(0, 4), sticky="w")

        # Save + Test buttons
        btns = ctk.CTkFrame(ai_card, fg_color="transparent")
        btns.grid(row=7, column=0, columnspan=2, padx=16, pady=(0, 16), sticky="w")

        ctk.CTkButton(btns, text=_t("settings_ai_save"), command=self._save_ai_config).pack(
            side="left", padx=(0, 8)
        )
        self._test_btn = ctk.CTkButton(
            btns, text=_t("settings_ai_test"),
            fg_color="#1A5276", hover_color="#1F618D",
            command=self._testar_conexao_ia,
        )
        self._test_btn.pack(side="left")

        self._lbl_teste_resultado = ctk.CTkLabel(
            ai_card, text="", font=ctk.CTkFont(size=11)
        )
        self._lbl_teste_resultado.grid(row=8, column=0, columnspan=2, padx=16, pady=(0, 8), sticky="w")

        # Help panel (hidden by default, toggled by [?] button)
        self._help_frame = ctk.CTkFrame(frame, fg_color="#1E1E24", corner_radius=10)
        self._help_frame.grid(row=5, column=0, padx=24, pady=(0, 16), sticky="ew")
        ctk.CTkLabel(
            self._help_frame,
            text=_t("settings_ai_help_msg"),
            text_color="#8A8A93",
            justify="left",
            wraplength=560,
            font=ctk.CTkFont(size=11),
        ).pack(padx=16, pady=12, anchor="w")
        self._help_frame.grid_remove()

        return frame

    def _change_lang(self, display_val: str, lang_display: dict) -> None:
        global APP_LANG
        code = {v: k for k, v in lang_display.items()}.get(display_val, "pt")
        APP_LANG = code
        cfg = _load_config()
        cfg["lang"] = code
        _save_config(cfg)
        self._restart_note.configure(text=_t("settings_restart_note"))

    # ------------------------------------------------------------------
    # Review screen
    # ------------------------------------------------------------------

    def _review_set_status(self, msg: str) -> None:
        self._review_status_lbl.configure(text=msg)

    def _load_review_cache(self) -> None:
        path_str = self.path_entry.get().strip()
        if not path_str:
            self._review_set_status(_t("review_no_path"))
            return

        base = Path(path_str)
        search_root = base.parent if base.is_file() else base

        # Collect candidate JSON files (look one level deep)
        candidates: list[Path] = []
        for d in [search_root, search_root.parent]:
            for f in d.glob("*.json"):
                name = f.name.lower()
                if any(kw in name for kw in ("tradu", "cache", "traducao")):
                    candidates.append(f)

        if not candidates:
            self._review_set_status(_t("review_no_cache", path=search_root.name))
            return

        cache_file = max(candidates, key=lambda f: f.stat().st_size)

        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for wrap_key in ("texts", "cache", "translations"):
                    if wrap_key in data and isinstance(data[wrap_key], dict):
                        data = data[wrap_key]
                        break
            self._review_cache = {
                k: v for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }
            self._review_path = cache_file
            self._render_review_cards()
            self._review_set_status(
                _t("review_status_loaded", name=cache_file.name, n=len(self._review_cache))
            )
        except Exception as exc:
            self._review_set_status(_t("review_load_err", err=exc))

    def _render_review_cards(self, filter_text: str = "") -> None:
        for card in self._review_cards:
            card.destroy()
        self._review_cards.clear()
        self._review_textboxes.clear()
        self._itens_exibidos = 0

        if not self._review_cache:
            return

        q = filter_text.strip().lower()
        self._itens_filtrados = [
            (o, t) for o, t in self._review_cache.items()
            if not q or q in o.lower() or q in t.lower()
        ]

        if not self._itens_filtrados:
            lbl = ctk.CTkLabel(
                self._review_scroll,
                text=_t("review_no_results", q=q) if q else "",
                text_color="gray",
            )
            lbl.grid(row=0, column=0, pady=20)
            self._review_cards.append(lbl)
            return

        self._render_next_batch()

    def _render_next_batch(self) -> None:
        start = self._itens_exibidos
        batch = self._itens_filtrados[start : start + 50]
        if not batch:
            return

        for original, translated in batch:
            idx = len(self._review_cards)
            self._build_review_card(idx, original, translated)

        self._itens_exibidos += len(batch)
        total = len(self._itens_filtrados)

        self._review_set_status(_t("review_status_showing", n=self._itens_exibidos, total=total))

        if self._itens_exibidos < total:
            remaining = total - self._itens_exibidos
            load_btn = ctk.CTkButton(
                self._review_scroll,
                text=_t("review_load_more", n=remaining),
                fg_color="#2B2B36",
                hover_color="#3D3D50",
                command=self._load_more_cards,
            )
            load_btn.grid(row=len(self._review_cards), column=0, padx=4, pady=8, sticky="ew")
            self._review_cards.append(load_btn)

    def _load_more_cards(self) -> None:
        if self._review_cards:
            self._review_cards[-1].destroy()
            self._review_cards.pop()
        self._render_next_batch()

    def _build_review_card(self, idx: int, original: str, translated: str) -> None:
        card = ctk.CTkFrame(self._review_scroll, fg_color="#1E1E24", corner_radius=10)
        card.grid(row=idx, column=0, padx=4, pady=4, sticky="ew")
        self._review_cards.append(card)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card,
            text=original,
            text_color="#8A8A93",
            justify="left",
            wraplength=500,
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(10, 4), sticky="w")

        tb = ctk.CTkTextbox(card, height=65, font=ctk.CTkFont(size=12))
        tb.insert("1.0", translated)
        tb.grid(row=1, column=0, padx=(12, 4), pady=(0, 10), sticky="ew")
        self._review_textboxes[original] = tb

        btn_col = ctk.CTkFrame(card, fg_color="transparent")
        btn_col.grid(row=1, column=1, padx=(4, 12), pady=(0, 10))

        google_btn = ctk.CTkButton(
            btn_col,
            text=_t("review_btn_google"),
            width=150,
            height=28,
            font=ctk.CTkFont(size=11),
            fg_color="#1A5276",
            hover_color="#1F618D",
        )
        google_btn.configure(
            command=lambda o=original, t=tb, b=google_btn: self._translate_card_google(o, t, b)
        )
        google_btn.pack(pady=(0, 4))

        ai_btn = ctk.CTkButton(
            btn_col,
            text=_t("review_redo_btn"),
            width=150,
            height=28,
            font=ctk.CTkFont(size=11),
            fg_color="#3D3D3D",
            hover_color="#555568",
        )
        ai_btn.configure(
            command=lambda o=original, t=tb, b=ai_btn: self._redo_card_with_ai(o, t, b)
        )
        ai_btn.pack()

    def _save_review(self) -> None:
        if not self._review_path:
            self._review_set_status(_t("review_no_path"))
            return

        updated = 0
        for original, tb in self._review_textboxes.items():
            new_text = tb.get("1.0", "end").rstrip("\n")
            if new_text and new_text != self._review_cache.get(original):
                self._review_cache[original] = new_text
                updated += 1

        try:
            raw = self._review_path.read_text(encoding="utf-8")
            existing = json.loads(raw)
            if isinstance(existing, dict) and "texts" in existing:
                out = {**existing, "texts": self._review_cache}
            else:
                out = self._review_cache
            self._review_path.write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._review_set_status(
                _t("review_saved", n=updated, name=self._review_path.name)
            )
        except Exception as exc:
            self._review_set_status(_t("review_save_err", err=exc))

    def _translate_card_google(
        self, original: str, textbox: ctk.CTkTextbox, btn: ctk.CTkButton
    ) -> None:
        btn.configure(state="disabled", text=_t("review_google_busy"))
        src_lang = LANGUAGES.get(self.src_var.get(), "en")
        tgt_lang = LANGUAGES.get(self.tgt_var.get(), "pt")
        _G = {"zh": "zh-CN"}

        def run():
            try:
                from deep_translator import GoogleTranslator
                result = GoogleTranslator(
                    source=_G.get(src_lang, src_lang),
                    target=_G.get(tgt_lang, tgt_lang),
                ).translate(original)
            except Exception:
                result = None

            def done():
                if result:
                    textbox.delete("1.0", "end")
                    textbox.insert("1.0", result)
                btn.configure(state="normal", text=_t("review_btn_google"))

            self._schedule(done)

        threading.Thread(target=run, daemon=True).start()

    def _redo_card_with_ai(
        self, original: str, textbox: ctk.CTkTextbox, btn: ctk.CTkButton
    ) -> None:
        cfg = _load_config()
        model = cfg.get("ai_model", "").strip()
        api_key = cfg.get("ai_api_key", "").strip()
        base_url = cfg.get("ai_base_url", "").strip()

        if not model:
            messagebox.showwarning(_t("warn_no_model_title"), _t("warn_no_model_msg"))
            return

        is_local = model.startswith("ollama/") or "localhost" in model or "127.0.0.1" in (base_url or "")
        if not is_local and not api_key:
            messagebox.showwarning(_t("warn_no_apikey_title"), _t("warn_no_apikey_msg"))
            return

        btn.configure(state="disabled", text=_t("review_redo_busy"))
        src_lang = LANGUAGES.get(self.src_var.get(), "en")
        tgt_lang = LANGUAGES.get(self.tgt_var.get(), "pt")

        def run():
            try:
                from translators.engine import refazer_com_ia_litellm
                result = refazer_com_ia_litellm(
                    texto_original=original,
                    src_lang=src_lang,
                    tgt_lang=tgt_lang,
                    model_name=model,
                    api_key=api_key,
                    base_url=base_url,
                )
            except Exception:
                result = None

            def done():
                if result:
                    textbox.delete("1.0", "end")
                    textbox.insert("1.0", result)
                btn.configure(state="normal", text=_t("review_redo_btn"))

            self._schedule(done)

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # Thread-safe UI queue
    # ------------------------------------------------------------------

    def _poll_ui_queue(self):
        """Drain thread callbacks and process IPC messages at ~60 fps."""
        try:
            while True:
                self._ui_queue.get_nowait()()
        except queue.Empty:
            pass

        if self._proc_queue is not None:
            try:
                while True:
                    self._handle_proc_msg(self._proc_queue.get_nowait())
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

    def _schedule(self, fn) -> None:
        """Enqueue a callable for the main thread. Safe from any thread."""
        self._ui_queue.put(fn)

    # ------------------------------------------------------------------
    # GPU acceleration
    # ------------------------------------------------------------------

    def _check_gpu_prompt(self):
        if (_gpu_libs_dir() / _GPU_KEY_DLL).exists():
            return
        if _hw_config().get("device") == "cuda":
            self._show_gpu_dialog()

    def _show_gpu_dialog(self):
        if messagebox.askyesno(_t("gpu_dialog_title"), _t("gpu_dialog_msg"), icon="question"):
            self._start_gpu_download()

    def _start_gpu_download(self):
        self.translate_btn.configure(state="disabled", text=_t("btn_downloading_gpu"))
        self._log(_t("log_gpu_start"))

        def run():
            ok = download_gpu_libs(
                log_fn=lambda m: self._schedule(lambda msg=m: self._log(msg)),
                progress_fn=lambda v, l=None: self._schedule(
                    lambda vv=v, ll=l: self._set_progress(vv, ll)
                ),
            )

            def done():
                self.translate_btn.configure(state="normal", text=_t("btn_translate"))
                self._log(_t("log_gpu_ok") if ok else _t("log_gpu_fail"))
                self._set_progress(0, _t("status_waiting"))

            self._schedule(done)

        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # File selection
    # ------------------------------------------------------------------

    def _select_folder(self):
        p = filedialog.askdirectory(title=_t("lbl_game_dir"))
        if p:
            self._set_path(Path(p))

    def _select_file(self):
        p = filedialog.askopenfilename(
            title=_t("lbl_game_dir"),
            filetypes=[("Twine HTML", "*.html *.htm"), ("Todos", "*.*")],
        )
        if p:
            self._set_path(Path(p))

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
            self._log(_t("log_detected", label=label))
            threading.Thread(target=self._detect_lang_async, args=(path, detected), daemon=True).start()
        else:
            self._log(_t("log_type_unknown"))

    def _detect_lang_async(self, path: Path, game_type: str):
        try:
            texts = sample_game_texts(path, game_type)
            code = detect_source_language(texts)
            if code:
                for name, c in LANGUAGES.items():
                    if c == code:
                        self._schedule(lambda n=name: self.src_var.set(n))
                        self._schedule(lambda cc=code: self._log(_t("log_lang_detected", code=cc.upper())))
                        return
            self._schedule(lambda: self._log(_t("log_lang_unknown")))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    def _set_progress(self, value: float, label: str = None):
        now = time.time()
        skip_bar = value not in (0.0, 1.0) and (now - self._last_progress_time) < 0.05
        if label:
            self._status_base = label
            if not self._ellipsis_active:
                self.status_label.configure(text=label)
        if self._translating and value <= 0:
            if not self._progress_indeterminate:
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start()
                self._progress_indeterminate = True
                self._start_ellipsis()
        else:
            if self._progress_indeterminate:
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate")
                self._progress_indeterminate = False
                self._stop_ellipsis()
            if not skip_bar:
                self.progress_bar.set(value)
                self._last_progress_time = now

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    def _start_translation(self):
        if self._translating:
            return
        path_str = self.path_entry.get().strip()
        if not path_str:
            self._log(_t("log_no_path"))
            return
        path = Path(path_str)
        if not path.exists():
            self._log(_t("log_path_missing", path=path))
            return

        type_key = GAME_TYPES.get(self.type_var.get())
        if not type_key:
            type_key = detect_game_type(path)
            if not type_key:
                self._log(_t("log_type_not_detected"))
                return

        src_lang = LANGUAGES.get(self.src_var.get(), "en")
        tgt_lang = LANGUAGES.get(self.tgt_var.get(), "pt")
        if src_lang == tgt_lang:
            self._log(_t("log_same_lang"))
            return

        self._translating = True
        self.translate_btn.configure(
            state="disabled", text=_t("btn_translating"), fg_color=_BTN_BUSY
        )
        self._progress_indeterminate = False
        self._set_progress(0, _t("status_starting"))
        self._log(_t("log_trans_start", label=GAME_TYPE_LABELS.get(type_key, type_key)))
        self._log(_t("log_path", path=path))
        self._log(_t("log_langs", src=self.src_var.get(), tgt=self.tgt_var.get()))

        _engine_map = {_t("engine_local"): "local", _t("engine_google"): "google"}
        engine = _engine_map.get(self.engine_var.get(), "local")

        self._proc_queue = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=worker_process,
            args=(TRANSLATORS[type_key], str(path), src_lang, tgt_lang, engine, self._proc_queue),
            daemon=True,
        )
        proc.start()
        self._translate_proc = proc
        self.cancel_btn.configure(state="normal")

    def _on_translation_done(self, success: bool):
        self._translating = False
        self._translate_proc = None
        self.cancel_btn.configure(state="disabled")
        self._stop_ellipsis()
        if self._progress_indeterminate:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self._progress_indeterminate = False
        if success:
            self.progress_bar.set(1.0)
            self.translate_btn.configure(
                state="normal", text=_t("btn_finished"), fg_color=_BTN_SUCCESS
            )
            self.after(2000, self._restore_btn_color)
        else:
            self.translate_btn.configure(
                state="normal", text=_t("btn_translate"), fg_color=_BTN_DEFAULT
            )

    def _restore_btn_color(self):
        self.translate_btn.configure(fg_color=_BTN_DEFAULT, text=_t("btn_translate"))

    def _cancel_translation(self) -> None:
        if self._translate_proc and self._translate_proc.is_alive():
            self._translate_proc.terminate()
        self._translate_proc = None
        self._proc_queue = None
        self._log(_t("log_abort"))
        self._on_translation_done(False)
        self._set_progress(0, _t("status_waiting"))

    # ------------------------------------------------------------------
    # Animated ellipsis (shows activity during GIL-heavy phases)
    # ------------------------------------------------------------------

    def _start_ellipsis(self) -> None:
        if self._ellipsis_active:
            return
        self._ellipsis_active = True
        self._ellipsis_step = 0
        self._tick_ellipsis()

    def _stop_ellipsis(self) -> None:
        self._ellipsis_active = False
        self.status_label.configure(text=self._status_base)

    def _tick_ellipsis(self) -> None:
        if not self._ellipsis_active:
            return
        self._ellipsis_step = (self._ellipsis_step + 1) % 3
        dots = "." * (self._ellipsis_step + 1)
        self.status_label.configure(text=self._status_base + dots)
        self.after(500, self._tick_ellipsis)

    # ------------------------------------------------------------------
    # AI settings save
    # ------------------------------------------------------------------

    def _save_ai_config(self) -> None:
        cfg = _load_config()
        cfg["ai_model"] = self._ai_model_var.get().strip()
        cfg["ai_api_key"] = self._ai_key_var.get().strip()
        cfg["ai_base_url"] = self._ai_url_var.get().strip()
        _save_config(cfg)
        self._ai_save_note.configure(text=_t("settings_ai_saved"))

    def _preset_changed(self, provider: str) -> None:
        if provider not in _AI_PRESETS:
            # Custom: re-enable key field, leave entries untouched
            self._ai_key_entry.configure(state="normal", placeholder_text="sk-...")
            return
        model, url, key_required = _AI_PRESETS[provider]
        self._ai_model_var.set(model)
        self._ai_url_var.set(url)
        if key_required:
            self._ai_key_entry.configure(state="normal", placeholder_text="sk-...")
        else:
            self._ai_key_var.set("")
            self._ai_key_entry.configure(
                state="disabled", placeholder_text=_t("settings_ai_key_local_ph")
            )

    def _show_ai_help(self) -> None:
        if self._help_visible:
            self._help_frame.grid_remove()
            self._help_visible = False
        else:
            self._help_frame.grid()
            self._help_visible = True

    def _testar_conexao_ia(self) -> None:
        model = self._ai_model_var.get().strip()
        api_key = self._ai_key_var.get().strip()
        base_url = self._ai_url_var.get().strip()

        if not model:
            messagebox.showwarning(_t("warn_no_model_title"), _t("warn_no_model_msg"))
            return

        self._test_btn.configure(state="disabled", text=_t("settings_ai_testing"))

        def run():
            try:
                import litellm
                kwargs: dict = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Responda apenas com a palavra 'OK' se você estiver recebendo esta mensagem."}],
                    "max_tokens": 10,
                }
                if api_key:
                    kwargs["api_key"] = api_key
                if base_url:
                    kwargs["base_url"] = base_url
                litellm.completion(**kwargs)

                def ok():
                    self._lbl_teste_resultado.configure(
                        text="✔ " + _t("settings_test_ok_msg"), text_color="#27AE60"
                    )
                    self._test_btn.configure(state="normal", text=_t("settings_ai_test"))
                    self.after(5000, lambda: self._lbl_teste_resultado.configure(text=""))

                self._schedule(ok)
            except Exception as exc:
                err = str(exc)[:120]

                def fail():
                    self._lbl_teste_resultado.configure(
                        text="✖ " + err, text_color="#E74C3C"
                    )
                    self._test_btn.configure(state="normal", text=_t("settings_ai_test"))
                    self.after(5000, lambda: self._lbl_teste_resultado.configure(text=""))

                self._schedule(fail)

        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
