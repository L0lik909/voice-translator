"""
Kat — голосовой переводчик для игр с оверлеем поверх экрана.

F9  (держать): говоришь по-русски -> внизу экрана перевод на английский
               (и он копируется в буфер обмена, можно вставить в чат Ctrl+V).
F10 (нажать):  переводит на русский то, что собеседники сказали за последние
               ~10 секунд (звук из наушников/колонок) -> вверху экрана.
F11 (нажать):  живой перевод вкл/выкл: Kat сам слушает собеседников и
               переводит их прямо по ходу речи, как субтитры.

Управление — иконка в трее: Настройки / Пауза / Перезапустить / Открыть папку / Выход.
Выход также по Ctrl+Shift+F9. Всё, что программа сообщает, пишется в log.txt.
words.txt — подсказки распознаванию (имена, сленг), glossary.txt — словарь для перевода.

Запуск: start.bat (установка/обновление + ярлыки) или ярлык «Kat»
на рабочем столе / в меню «Пуск».
Ключи командной строки:
  --setup      создать иконку и ярлыки, затем выйти (вызывает start.bat)
  --restart    дождаться, пока закроется предыдущая копия (при перезапуске)
  --elevated   уже запущены через «от администратора», повторно не просить
"""

import os
import re
import sys
import glob
import time
import queue
import ctypes
import hashlib
import threading
import traceback
import subprocess
import collections
import configparser

APP_NAME = "Kat"
APP_VERSION = "1.3.0"  # менять при каждом обновлении + запись в CHANGELOG.md
OLD_APP_NAMES = ("Голосовой переводчик",)  # прежние названия — их ярлыки заменяем новыми
# FROZEN — запущены как Kat.exe из установщика (PyInstaller), а не как translator.py
FROZEN = bool(getattr(sys, "frozen", False))
SCRIPT = sys.executable if FROZEN else os.path.abspath(__file__)
BASE_DIR = os.path.dirname(SCRIPT)
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.ini")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
ICON_VERSION = 3  # увеличить, когда меняется рисунок в draw_icon
# Версия в имени файла: Windows кэширует иконки по пути, и при том же имени
# ярлыки продолжали бы показывать старую картинку.
ICON_FILE = os.path.join(BASE_DIR, f"icon-v{ICON_VERSION}.ico")
PYTHONW = os.path.join(BASE_DIR, "venv", "Scripts", "pythonw.exe")
REQUIREMENTS = os.path.join(BASE_DIR, "requirements.txt")
REQ_STAMP = os.path.join(BASE_DIR, "venv", "requirements.sha256")  # пишет start.bat
SAMPLE_RATE = 16000  # Whisper работает только с 16 кГц — не настройка
GITHUB_REPO = "L0lik909/voice-translator"  # отсюда проверяются обновления (релизы)
CUDA_DIR = os.path.join(BASE_DIR, "cuda")  # Kat.exe: сюда докачиваются DLL для видеокарты


# ======================= ЛОГ =======================

def message_box(text, error=True):
    """Окно с сообщением — единственный способ что-то сказать, когда консоли нет."""
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(0, str(text), APP_NAME, 0x10 if error else 0x40)


def ask_yes_no(text):
    MB_YESNO, MB_ICONQUESTION, IDYES = 0x4, 0x20, 6
    return ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, MB_YESNO | MB_ICONQUESTION) == IDYES


class Log:
    """Всё, что печатается, пишет в log.txt (с временем), а если есть консоль — и в неё."""

    MAX_SIZE = 2 * 2**20  # больше 2 МБ — старый лог переименовывается в log.old.txt

    def __init__(self, path, echo):
        if os.path.exists(path) and os.path.getsize(path) > self.MAX_SIZE:
            os.replace(path, os.path.splitext(path)[0] + ".old.txt")
        self.f = open(path, "a", encoding="utf-8")
        self.echo = echo        # консоль или None (под pythonw.exe её нет)
        self.bol = True         # следующий символ — начало строки
        self.lock = threading.Lock()
        self.encoding = "utf-8"

    def write(self, s):
        if not s:
            return 0
        with self.lock:
            parts = []
            for line in s.splitlines(keepends=True):
                if self.bol:
                    parts.append(time.strftime("[%H:%M:%S] "))
                parts.append(line)
                self.bol = line.endswith("\n")
            self.f.write("".join(parts))
            self.f.flush()
        if self.echo is not None:
            try:
                self.echo.write(s)
                self.echo.flush()
            except Exception:
                pass
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def setup_logging():
    sys.stdout = sys.stderr = Log(LOG_FILE, sys.__stdout__)
    print(f"===== Запуск {APP_NAME} {APP_VERSION}, {time.strftime('%Y-%m-%d %H:%M:%S')} =====")

    def excepthook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        message_box(f"{exc}\n\nПодробности в {LOG_FILE}")

    def thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        print(f"[!] Ошибка в потоке {args.thread.name if args.thread else '?'}:")
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


if __name__ == "__main__":
    setup_logging()  # до чтения настроек, чтобы ошибка в settings.ini попала в лог и на экран


# ======================= НАСТРОЙКИ =======================
# Настройки читаются из settings.ini. Если файла нет — он создаётся из этого
# текста. Если в файле не хватает какой-то строки — берётся значение отсюда.
DEFAULT_SETTINGS = """\
; Настройки Kat — голосового переводчика.
; Строки, начинающиеся с ";", — комментарии.
;
; Горячие клавиши, микрофон, устройство вывода, прозрачность, размер шрифта,
; время показа и положение текста удобнее менять в окне «Настройки»
; (иконка Kat в трее, возле часов) — там они применяются сразу.
; Остальное меняется здесь и вступает в силу после перезапуска программы.
;
; Если что-то сломалось — удалите этот файл, он создастся заново
; с настройками по умолчанию.

[моя_речь]
; Моя речь: говорю по-русски -> перевод на английский.

; Клавиша: держать и говорить (одна клавиша, без сочетаний).
hotkey = f9

; Модель распознавания речи:
;   tiny / base / small / medium / large-v3 / large-v3-turbo.
; large-v3-turbo — быстрая и почти такая же точная, как large-v3 (по умолчанию).
; large-v3 — самая точная, но заметно медленнее.
; Перевод на английский делает отдельная модель quickmt-ru-en, поэтому
; модель распознавания на качество перевода почти не влияет.
model_size = large-v3-turbo

; Язык, на котором я говорю.
source_lang = ru

; На какой язык переводить мою речь: en (английский) или дополнительный язык
; собеседников (extra_lang ниже). Удобнее выбрать в окне «Настройки».
target_lang = en

; Показывать и оригинал, и перевод (да / нет).
show_original = да

; Копировать мой перевод в буфер обмена, чтобы вставить в чат Ctrl+V (да / нет).
copy_to_clipboard = да

; Микрофон. Пусто = микрофон по умолчанию в Windows.
; Удобнее выбрать в окне «Настройки». Вручную: номер из списка в log.txt
; (например 3) или часть названия (например HyperX).
mic_device =

[собеседники]
; Собеседники: английская речь из наушников -> перевод на русский.

; Клавиша: нажать, чтобы перевести последние слова собеседников.
hotkey = f10

; Сколько последних секунд звука переводить.
seconds = 10

; Дополнительный язык собеседников (кроме английского): код языка, например
; de (немецкий), es (испанский), ja (японский). Пусто — только английский.
; Удобнее выбрать в окне «Настройки» — там же скачаются модели (~700 МБ).
extra_lang =

; Живой перевод: Kat сам слушает собеседников и переводит их прямо по ходу
; речи (без нажатия клавиши), как субтитры. Русскую речь пропускает.
; Клавиша включения/выключения (одна клавиша, без сочетаний):
live_hotkey = f11
; Включён ли живой перевод (да / нет). Запоминается при переключении.
live = нет

; Устройство вывода звука, которое слушаем. Пусто = устройство по умолчанию.
; Если Discord звучит через другие наушники — выберите их в окне «Настройки».
loopback_device =

[внешний_вид]
; Сочетание клавиш для выхода из программы.
exit_hotkey = ctrl+shift+f9

; Сколько секунд висит перевод на экране.
display_seconds = 8

; Шрифт: название, размер, жирный (да / нет).
font_name = Segoe UI
font_size = 22
font_bold = да

; Прозрачность текста: 1.0 = непрозрачный, 0.3 = еле видно.
text_opacity = 0.6

; Цвета в формате #RRGGBB.
; Мой перевод:
text_color = #FFFFFF
; Перевод собеседника:
them_color = #80D8FF
; Служебные сообщения («Слушаю...», «Перевожу...»):
status_color = #FFD54F
; Обводка текста:
outline_color = #000000

; Мой перевод: отступ от низа экрана (в пикселях).
y_from_bottom = 230

; Перевод собеседника: отступ от верха экрана (в пикселях).
them_y_from_top = 120

; Нажатия F9 короче этого (в секундах) игнорируются.
min_seconds = 0.3

[запуск]
; Запускать программу от имени администратора (да / нет).
; Нужно, если игра сама запущена от администратора (например Genshin Impact):
; иначе Windows не передаёт программе нажатия F9/F10 внутри игры.
; При каждом запуске Windows будет спрашивать разрешение (окно UAC).
; Автозапуск вместе с Windows включается в окне «Настройки».
run_as_admin = нет

; При запуске проверять, не вышла ли новая версия Kat на GitHub (да / нет).
check_updates = да
"""

# Какая строка settings.ini в какую переменную программы попадает:
# (переменная, раздел, ключ, тип)
SPEC = [
    ("HOTKEY", "моя_речь", "hotkey", "str"),
    ("MODEL_SIZE", "моя_речь", "model_size", "str"),
    ("SOURCE_LANG", "моя_речь", "source_lang", "str"),
    ("TARGET_LANG", "моя_речь", "target_lang", "str"),
    ("SHOW_ORIGINAL", "моя_речь", "show_original", "bool"),
    ("COPY_TO_CLIPBOARD", "моя_речь", "copy_to_clipboard", "bool"),
    ("MIC_DEVICE", "моя_речь", "mic_device", "device"),
    ("THEM_HOTKEY", "собеседники", "hotkey", "str"),
    ("THEM_SECONDS", "собеседники", "seconds", "float"),
    ("EXTRA_LANG", "собеседники", "extra_lang", "str"),
    ("LIVE_HOTKEY", "собеседники", "live_hotkey", "str"),
    ("LIVE", "собеседники", "live", "bool"),
    ("LOOPBACK_DEVICE", "собеседники", "loopback_device", "device"),
    ("EXIT_HOTKEY", "внешний_вид", "exit_hotkey", "str"),
    ("DISPLAY_SECONDS", "внешний_вид", "display_seconds", "float"),
    ("FONT_NAME", "внешний_вид", "font_name", "str"),
    ("FONT_SIZE", "внешний_вид", "font_size", "int"),
    ("FONT_BOLD", "внешний_вид", "font_bold", "bool"),
    ("TEXT_OPACITY", "внешний_вид", "text_opacity", "float"),
    ("TEXT_COLOR", "внешний_вид", "text_color", "str"),
    ("THEM_COLOR", "внешний_вид", "them_color", "str"),
    ("STATUS_COLOR", "внешний_вид", "status_color", "str"),
    ("OUTLINE_COLOR", "внешний_вид", "outline_color", "str"),
    ("Y_FROM_BOTTOM", "внешний_вид", "y_from_bottom", "int"),
    ("THEM_Y_FROM_TOP", "внешний_вид", "them_y_from_top", "int"),
    ("MIN_SECONDS", "внешний_вид", "min_seconds", "float"),
    ("RUN_AS_ADMIN", "запуск", "run_as_admin", "bool"),
    ("CHECK_UPDATES", "запуск", "check_updates", "bool"),
]


def _device(value):
    """Пусто/none -> None, число -> номер устройства, иначе название."""
    value = value.strip().strip('"\'')
    if not value or value.lower() in ("none", "нет"):
        return None
    return int(value) if value.isdigit() else value


def _parse(section, key, typ):
    if typ == "bool":
        return section.getboolean(key)
    if typ == "int":
        return section.getint(key)
    if typ == "float":
        return section.getfloat(key)
    value = section.get(key)
    return _device(value) if typ == "device" else value.strip()


def _format(value, typ):
    if typ == "bool":
        return "да" if value else "нет"
    if typ == "device":
        return "" if value is None else str(value)
    if typ == "float":
        return f"{value:g}"
    return str(value)


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write(DEFAULT_SETTINGS)
        print(f"Создан файл настроек: {SETTINGS_FILE}")

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.BOOLEAN_STATES = {**cfg.BOOLEAN_STATES, "да": True, "нет": False}
    cfg.read_string(DEFAULT_SETTINGS)
    try:
        # utf-8-sig: Блокнот иногда сохраняет файл с BOM
        with open(SETTINGS_FILE, encoding="utf-8-sig") as f:
            cfg.read_file(f)
        return {var: _parse(cfg[sec], key, typ) for var, sec, key, typ in SPEC}
    except (configparser.Error, ValueError) as e:
        raise RuntimeError(f"Ошибка в {SETTINGS_FILE}:\n{e}\n\n"
                           "Исправьте файл или удалите его — он создастся заново.") from None


def set_settings(values):
    """Меняет настройки в работающей программе (переменные модуля)."""
    global FONT
    globals().update(values)
    FONT = (FONT_NAME, FONT_SIZE, "bold" if FONT_BOLD else "normal")


def save_settings(values):
    """Записывает изменённые значения в settings.ini, сохраняя комментарии."""
    updates = {(sec, key): _format(values[var], typ)
               for var, sec, key, typ in SPEC if var in values}
    try:
        with open(SETTINGS_FILE, encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError:
        lines = DEFAULT_SETTINGS.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    out, done, section = [], set(), None

    def add_missing(sec):  # ключи, которых не было в разделе, — в его конец
        for (s, k), v in updates.items():
            if s == sec and (s, k) not in done:
                out.append(f"{k} = {v}".rstrip() + "\n")
                done.add((s, k))

    for line in lines:
        m = re.match(r"\s*\[(.+?)\]\s*$", line)
        if m:
            if section is not None:
                add_missing(section)
            section = m.group(1)
        else:
            m = re.match(r"\s*([^;#\s=][^=]*?)\s*=", line)
            if m and (section, m.group(1).lower()) in updates:
                key = m.group(1).lower()
                line = f"{key} = {updates[(section, key)]}".rstrip() + "\n"
                done.add((section, key))
        out.append(line)
    if section is not None:
        add_missing(section)
    for sec in dict.fromkeys(s for s, _ in updates):  # разделов не было вовсе
        if any((sec, k) not in done for s, k in updates if s == sec):
            out.append(f"\n[{sec}]\n")
            add_missing(sec)

    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, SETTINGS_FILE)


set_settings(load_settings())
# =========================================================


_cuda_dirs_added = set()


def add_cuda_dlls():
    """Подключает DLL CUDA: из pip-пакетов nvidia-cublas / nvidia-cudnn (запуск из
    исходников) или из папки cuda рядом с Kat.exe (их докачивает ensure_cuda_libs)."""
    if os.name != "nt":
        return
    dirs = [d for sp in list(sys.path) for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin"))]
    if os.path.isdir(CUDA_DIR):
        dirs.append(CUDA_DIR)
    for d in dirs:
        if d in _cuda_dirs_added:
            continue
        _cuda_dirs_added.add(d)
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


add_cuda_dlls()

import numpy as np            # noqa: E402
import sounddevice as sd      # noqa: E402
import keyboard               # noqa: E402
import tkinter as tk          # noqa: E402


MODELS_DIR = os.path.join(BASE_DIR, "models")
LAST_WAV = os.path.join(BASE_DIR, "last.wav")
WORDS_FILE = os.path.join(BASE_DIR, "words.txt")
HINT_TOKENS = 223  # faster-whisper обрезает hotwords до 448 // 2 - 1 токенов
ENDPOINTS = ["https://huggingface.co", "https://hf-mirror.com"]  # основной + зеркало


# ======================= ЗАГРУЗКА МОДЕЛЕЙ =======================

_download_hook = None  # функция(текст): показать ход загрузки на экране


def download_file(url, dest):
    """Обычная HTTP-загрузка с докачкой и шкалой. Возвращает False, если файла нет (404)."""
    import urllib.request
    import urllib.error
    from tqdm import tqdm
    tmp = dest + ".part"
    pos = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    headers = {"User-Agent": "voice-translator/1.0"}
    if pos:
        headers["Range"] = f"bytes={pos}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=30)  # 30 сек без данных = обрыв
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        if e.code == 416:  # уже скачан целиком
            os.replace(tmp, dest)
            return True
        raise
    label = f"{os.path.basename(os.path.dirname(dest))}/{os.path.basename(dest)}"
    console = sys.__stderr__ is not None and sys.__stderr__.isatty()
    with r:
        if pos and r.status != 206:  # сервер не умеет докачку
            pos = 0
        total = int(r.headers.get("Content-Length") or 0) + pos
        done = pos
        shown = 0.0
        with open(tmp, "ab" if pos else "wb") as f, tqdm(
            total=total or None, initial=pos, unit="B", unit_scale=True,
            unit_divisor=1024, desc=os.path.basename(dest), ascii=True,
            mininterval=0.5 if console else 30,  # в log.txt — не чаще раза в 30 с
        ) as bar:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                bar.update(len(chunk))
                if _download_hook and time.monotonic() - shown > 1:
                    shown = time.monotonic()
                    pct = f"{done / total:.0%}" if total else f"{done / 2**20:.0f} МБ"
                    _download_hook(f"Скачиваю {label}: {pct}")
    if total and done < total:  # соединение оборвалось — .part остаётся для докачки
        raise IOError(f"обрыв на {done / 2**20:.0f} из {total / 2**20:.0f} МБ")
    os.replace(tmp, dest)
    return True


def download_repo(repo, name, required, optional=()):
    folder = os.path.join(MODELS_DIR, name)
    os.makedirs(folder, exist_ok=True)
    print(f"Модель {name}: проверяю/скачиваю в {folder}")
    for fname in list(required) + list(optional):
        dest = os.path.join(folder, fname)
        if os.path.exists(dest):
            continue
        result = None
        for attempt in range(30):
            ep = ENDPOINTS[attempt % len(ENDPOINTS)]
            try:
                result = download_file(f"{ep}/{repo}/resolve/main/{fname}", dest)
                break
            except Exception as e:
                nxt = ENDPOINTS[(attempt + 1) % len(ENDPOINTS)]
                print(f"[!] {fname}: {type(e).__name__}: {e}. Пробую снова через {nxt}...")
                time.sleep(2)
        if not result and fname in required:
            raise RuntimeError(f"Не удалось скачать {fname}")
    return folder


# Библиотеки NVIDIA для видеокарты весят ~2 ГБ, поэтому в установщик не входят:
# Kat.exe докачивает их с PyPI при первом запуске, если в системе есть драйвер NVIDIA.
# Версии — те же, с которыми программа проверена (как в requirements.txt).
CUDA_WHEELS = [("nvidia-cublas-cu12", "12.9.2.10"),
               ("nvidia-cuda-nvrtc-cu12", "12.9.86"),
               ("nvidia-cudnn-cu12", "9.28.1.4")]
CUDA_SKIP = ("nvblas",)  # эти DLL программе не нужны


def has_nvidia_gpu():
    """Есть ли драйвер NVIDIA (nvcuda.dll ставится вместе с ним)."""
    try:
        ctypes.WinDLL("nvcuda.dll")
        return True
    except OSError:
        return False


def ensure_cuda_libs():
    """Kat.exe + видеокарта NVIDIA: скачать DLL CUDA в папку cuda, если их ещё нет.
    Ничего не делает при запуске из исходников (там DLL ставит pip)."""
    import json
    import urllib.request
    import zipfile
    done_mark = os.path.join(CUDA_DIR, "ok.txt")
    if not FROZEN or os.path.exists(done_mark) or not has_nvidia_gpu():
        return
    os.makedirs(CUDA_DIR, exist_ok=True)
    print("Видеокарта NVIDIA: докачиваю библиотеки CUDA (~2 ГБ, один раз)...")
    for pkg, ver in CUDA_WHEELS:
        req = urllib.request.Request(f"https://pypi.org/pypi/{pkg}/{ver}/json",
                                     headers={"User-Agent": f"Kat/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            files = json.load(resp)["urls"]
        url = next(f["url"] for f in files if f["filename"].endswith("win_amd64.whl"))
        whl = os.path.join(CUDA_DIR, url.rsplit("/", 1)[1])
        for attempt in range(10):
            try:
                download_file(url, whl)
                break
            except Exception as e:
                if attempt == 9:
                    raise
                print(f"[!] {pkg}: {type(e).__name__}: {e}. Пробую снова...")
                time.sleep(3)
        if _download_hook:
            _download_hook(f"Распаковываю {pkg}...")
        with zipfile.ZipFile(whl) as z:  # из колеса нужны только DLL из nvidia/*/bin
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1]
                if "/bin/" in name and base.lower().endswith(".dll") \
                        and not base.lower().startswith(CUDA_SKIP):
                    with z.open(name) as src, open(os.path.join(CUDA_DIR, base), "wb") as dst:
                        while True:
                            chunk = src.read(1 << 20)
                            if not chunk:
                                break
                            dst.write(chunk)
        os.remove(whl)
    with open(done_mark, "w", encoding="utf-8") as f:
        f.write(" ".join(f"{p}=={v}" for p, v in CUDA_WHEELS))
    add_cuda_dlls()
    print("Библиотеки CUDA готовы.")


# У turbo нет сборки от Systran; репозиторий mobiuslabsgmbh переехал в dropbox-dash
WHISPER_REPOS = {
    "large-v3-turbo": "dropbox-dash/faster-whisper-large-v3-turbo",
    "turbo": "dropbox-dash/faster-whisper-large-v3-turbo",
}


def get_whisper_path(size):
    if size == "turbo":
        size = "large-v3-turbo"
    return download_repo(
        WHISPER_REPOS.get(size, f"Systran/faster-whisper-{size}"), size,
        ["config.json", "model.bin", "tokenizer.json"],
        ["preprocessor_config.json", "vocabulary.json", "vocabulary.txt"],
    )


def get_mt_path(name):
    return download_repo(
        f"quickmt/{name}", name,
        ["config.json", "model.bin", "source_vocabulary.json",
         "target_vocabulary.json", "src.spm.model", "tgt.spm.model"],
    )


def load_whisper():
    from faster_whisper import WhisperModel

    def warmup(m):
        segs, _ = m.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language=SOURCE_LANG)
        list(segs)

    def load_gpu(path):
        m = WhisperModel(path, device="cuda", compute_type="int8_float16")
        warmup(m)
        return m

    path = get_whisper_path(MODEL_SIZE)
    try:
        try:
            m = load_gpu(path)
        except Exception as e:
            if "payload" not in str(e):
                raise
            print(f"[!] Файл модели повреждён ({e}). Удаляю и скачиваю заново...")
            os.remove(os.path.join(path, "model.bin"))
            path = get_whisper_path(MODEL_SIZE)
            m = load_gpu(path)
        return m, f"GPU, {MODEL_SIZE}"
    except Exception as e:
        print(f"[!] GPU не завёлся ({e}). Переключаюсь на процессор (модель small).")
        m = WhisperModel(get_whisper_path("small"), device="cpu", compute_type="int8")
        warmup(m)
        return m, "CPU, small"


class Hints:
    """Подсказки для Whisper (hotwords) из words.txt.

    Файл перечитывается, когда меняется. Разделы [ru], [en] и т.д. — по языку
    текста, который должен получиться; слова до первого раздела идут во все.
    Слова, не влезающие в лимит токенов Whisper, отбрасываются с конца.
    """

    def __init__(self, tokenizer):
        self.tok = tokenizer
        self.mtime = -1   # «ещё не читали» (None = файла нет)
        self.words = {}   # язык (или None — общие) -> список слов
        self.cache = {}   # язык -> готовая строка подсказки

    def _reload(self):
        try:
            mtime = os.path.getmtime(WORDS_FILE)
        except OSError:
            mtime = None
        if mtime == self.mtime:
            return
        self.mtime, self.words, self.cache = mtime, {}, {}
        if mtime is None:
            print(f"[i] {os.path.basename(WORDS_FILE)} не найден — работаю без подсказок.")
            return
        lang = None
        with open(WORDS_FILE, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.fullmatch(r"\[(\w+)\]", line)
                if m:
                    lang = m.group(1).lower()
                    continue
                self.words.setdefault(lang, []).append(line)
        for lg in self.words:
            if lg is not None:
                self.get(lg)  # сразу сообщить в лог, сколько слов вошло

    def get(self, lang):
        """Строка для hotwords (или None, если подсказок нет)."""
        try:
            self._reload()
        except Exception as e:
            print(f"[!] Не удалось прочитать {WORDS_FILE}: {e}")
            return None
        if lang in self.cache:
            return self.cache[lang]
        words = self.words.get(None, []) + self.words.get(lang, [])
        fit = []
        for w in words:
            if len(self.tok.encode(" " + ", ".join(fit + [w])).ids) > HINT_TOKENS:
                break
            fit.append(w)
        if words:
            msg = f"Подсказки [{lang}]: вошло {len(fit)} из {len(words)} слов"
            if len(fit) < len(words):
                msg += f" (не влезли: {', '.join(words[len(fit):])})"
            print(msg)
        self.cache[lang] = ", ".join(fit) or None
        return self.cache[lang]


GLOSSARY_FILE = os.path.join(BASE_DIR, "glossary.txt")
DEFAULT_GLOSSARY = """\
# Словарь для перевода: сленг, имена, короткие фразы.
# Файл перечитывается автоматически, перезапуск не нужен.
#
# Формат: что = на что. Строки с # — комментарии. Регистр и ё/е не важны.
#
# [фразы ru-en] — если ВСЯ фраза (предложение) совпадает с левой частью
#   (без учёта знаков препинания), перевод берётся отсюда, модель не нужна.
#   Модель перевода плохо справляется с одним-двумя словами («Нет.» она
#   переводит как «No, no, no, no...»), поэтому короткие фразы лучше держать тут.
# [слова ru-en] — заменяются внутри фразы ДО перевода. Пишите замену
#   по-английски: модель оставляет английские слова как есть.
#   * в конце слова — любое окончание: «крутк*» = крутка, крутки, круток...
# [фразы en-ru], [слова en-ru] — то же для речи собеседников.
#   В [слова en-ru] заменяйте сленг на ОБЫЧНЫЙ АНГЛИЙСКИЙ (русские слова
#   модель EN->RU портит), например: gg = good game.

[фразы ru-en]
да = Yes
нет = No
ок = OK
окей = OK
хорошо = OK
ладно = Fine
привет = Hi
всем привет = Hi all
спасибо = Thanks
спасибо большое = Thank you so much
помогите = Help!
помоги = Help me!
иду = Coming
уже иду = On my way
жди = Wait
подожди = Wait
секунду = One sec
понял = Got it
поняла = Got it
сорри = Sorry
извини = Sorry
пока = Bye
го = Let's go
гг = GG
афк = AFK

[слова ru-en]
у тебя не лагает = are you lagging
у тебя лагает = are you lagging
у меня лагает = I'm lagging
лагает = is lagging
лагать = lag
лаги = lags
крутк* = pulls
круток = pulls
гарант* = pity
хилер* = healer
хилк* = healing
хил = healing
ганкни = gank
го = let's go
мид = mid
керри = carry
саппорт = support
байбэк = buyback
бкб = BKB
афк = AFK
нуб* = noob
имба = OP
ульт = ult
ульту = ult
кд = cooldown
хп = HP

[фразы en-ru]
gg = GG (хорошая игра)
gg wp = GG, хорошо сыграли
wp = Хорошо сыграно
thanks = Спасибо
thank you = Спасибо
ty = Спасибо
np = Без проблем
no problem = Без проблем
brb = Сейчас вернусь
on my way = Иду
omw = Иду
yes = Да
yeah = Да
no = Нет
ok = Ок
okay = Ок
nice = Отлично
help = Помогите!
lol = Ха-ха
sorry = Извини

[слова en-ru]
lagging = having lag
laggy = having lag
feeding = dying all the time
gg = good game
wp = well played
"""


def _norm_phrase(s):
    """Для сравнения фраз: без регистра, ё=е, без знаков препинания."""
    s = s.lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


class Glossary:
    """Словарь замен из glossary.txt (см. DEFAULT_GLOSSARY). Перечитывается, когда меняется."""

    def __init__(self):
        self.mtime = -1
        self.phrases = {}  # "ru-en" -> {норм. фраза: перевод}
        self.words = {}    # "ru-en" -> [(regex, замена)]

    def _reload(self):
        if not os.path.exists(GLOSSARY_FILE):
            with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
                f.write(DEFAULT_GLOSSARY)
            print(f"Создан словарь: {GLOSSARY_FILE}")
        mtime = os.path.getmtime(GLOSSARY_FILE)
        if mtime == self.mtime:
            return
        phrases, words, section = {}, {}, None
        with open(GLOSSARY_FILE, encoding="utf-8-sig") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.fullmatch(r"\[\s*(фразы|слова)\s+(\w+-\w+)\s*\]", line, re.I)
                if m:
                    section = (m.group(1).lower(), m.group(2).lower())
                    continue
                key, sep, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if section is None or not sep or not key or not val:
                    print(f"[!] {os.path.basename(GLOSSARY_FILE)}, строка {n}: не понял «{line}»")
                    continue
                kind, direction = section
                if kind == "фразы":
                    phrases.setdefault(direction, {})[_norm_phrase(key)] = val
                else:
                    words.setdefault(direction, []).append((key, val))
        for direction, pairs in words.items():
            pairs.sort(key=lambda kv: -len(kv[0]))  # длинные фразы — раньше коротких
            words[direction] = [(self._pattern(k), v) for k, v in pairs]
        self.mtime, self.phrases, self.words = mtime, phrases, words
        print(f"Словарь: {sum(map(len, phrases.values()))} фраз, "
              f"{sum(map(len, words.values()))} замен")

    @staticmethod
    def _pattern(key):
        parts = []
        for w in key.lower().split():
            p = re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w)
            parts.append(re.sub("[её]", "[её]", p))
        return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.I)

    def _safe_reload(self):
        try:
            self._reload()
        except Exception as e:
            print(f"[!] Не удалось прочитать {GLOSSARY_FILE}: {e}")

    def phrase(self, direction, sentence):
        """Готовый перевод всей фразы или None."""
        self._safe_reload()
        out = self.phrases.get(direction, {}).get(_norm_phrase(sentence))
        if out and out[-1] not in ".!?…)" and sentence.rstrip()[-1:] in ("!", "?"):
            out += sentence.rstrip()[-1]  # сохранить интонацию: «Иду!» -> «Coming!»
        return out

    def replace(self, direction, sentence):
        self._safe_reload()

        def sub(m, v):
            return v[:1].upper() + v[1:] if m.group(0)[:1].isupper() else v
        for rx, v in self.words.get(direction, []):
            sentence = rx.sub(lambda m, v=v: sub(m, v), sentence)
        return sentence


# Языки, для которых у quickmt есть модели перевода в обе стороны с английским.
# Перевод с русского и на русский для них идёт через английский (ru -> en -> de).
LANGUAGES = {
    "ar": "Арабский", "bn": "Бенгальский", "cs": "Чешский", "da": "Датский",
    "de": "Немецкий", "el": "Греческий", "es": "Испанский", "fa": "Персидский",
    "fr": "Французский", "he": "Иврит", "hi": "Хинди", "hu": "Венгерский",
    "id": "Индонезийский", "is": "Исландский", "it": "Итальянский", "ja": "Японский",
    "ko": "Корейский", "lv": "Латышский", "pl": "Польский", "pt": "Португальский",
    "ro": "Румынский", "sv": "Шведский", "th": "Тайский", "tr": "Турецкий",
    "uk": "Украинский", "ur": "Урду", "vi": "Вьетнамский", "zh": "Китайский",
}


def lang_name(code):
    return {"en": "Английский", "ru": "Русский"}.get(code) or LANGUAGES.get(code, code)


def lang_downloaded(code):
    """Скачаны ли обе модели перевода для языка (code->en и en->code)."""
    return all(os.path.exists(os.path.join(MODELS_DIR, f"quickmt-{d}", "model.bin"))
               for d in (f"{code}-en", f"en-{code}"))


class TextTranslator:
    """Офлайн-перевод текста моделью quickmt (~400 МБ): quickmt-en-ru, quickmt-ru-en."""

    # Модель на коротких фразах любит дописывать лишнее: «Помогите!» -> «Help me! please»,
    # «Привет!» -> «Hello! I am very happy!». Предложения переводятся по одному, поэтому
    # всё после конца первого предложения в переводе — мусор, его отрезаем.
    EXTRA_TAIL = re.compile(r"(?<=\w\w)[.!?…]+(?=\s+\S)")

    def __init__(self, name, glossary=None):
        import ctranslate2
        import sentencepiece as spm
        self.direction = name.split("-", 1)[1]  # "quickmt-ru-en" -> "ru-en"
        self.glossary = glossary
        path = get_mt_path(name)
        self.src = spm.SentencePieceProcessor(model_file=os.path.join(path, "src.spm.model"))
        self.tgt = spm.SentencePieceProcessor(model_file=os.path.join(path, "tgt.spm.model"))
        try:
            self.tr = ctranslate2.Translator(path, device="cuda", compute_type="int8_float16")
            self("ok")  # прогрев и проверка, что GPU работает
        except Exception as e:
            print(f"[!] Переводчик {name} на GPU не завёлся ({e}), работаю на процессоре.")
            self.tr = ctranslate2.Translator(path, device="cpu", compute_type="int8")

    def __call__(self, text):
        sents = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
        out = [None] * len(sents)
        todo = []
        for i, s in enumerate(sents):
            if self.glossary is not None:
                out[i] = self.glossary.phrase(self.direction, s)
            if out[i] is None:
                todo.append(i)
        if todo:
            src = [sents[i] if self.glossary is None
                   else self.glossary.replace(self.direction, sents[i]) for i in todo]
            toks = [self.src.encode(s, out_type=str) + ["</s>"] for s in src]
            # перевод не длиннее 2x исходного (+запас): иначе на коротких фразах
            # модель уходит в повторы «No, no, no, no...»
            res = self.tr.translate_batch(
                toks, beam_size=4, no_repeat_ngram_size=3,
                max_decoding_length=min(256, 2 * max(map(len, toks)) + 8))
            for i, tk, r in zip(todo, toks, res):
                o = self.tgt.decode(r.hypotheses[0][: 2 * len(tk) + 8]).strip()
                m = self.EXTRA_TAIL.search(o)
                out[i] = o[: m.end()] if m else o
        return " ".join(out).strip()


# ======================= ЗВУК =======================

def save_wav(audio, path):
    import wave
    data = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(data.tobytes())


def to_16k(a, rate):
    if rate == SAMPLE_RATE or len(a) == 0:
        return a.astype(np.float32)
    if rate % SAMPLE_RATE == 0:  # 48000 -> 16000: усредняем по 3 отсчёта
        f = rate // SAMPLE_RATE
        a = a[: len(a) - len(a) % f]
        return a.reshape(-1, f).mean(axis=1).astype(np.float32)
    n = int(len(a) * SAMPLE_RATE / rate)
    return np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)


def list_mics():
    print("Микрофоны (номер — название):")
    try:
        default_in = sd.default.device[0]
        hostapis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                api = hostapis[d["hostapi"]]["name"]
                mark = "  <-- по умолчанию" if i == default_in else ""
                print(f"  {i}: {d['name']} [{api}]{mark}")
    except Exception as e:
        print(f"  не удалось получить список: {e}")


def input_devices():
    """Микрофоны для окна настроек: [(подпись, значение для settings.ini)].

    Значение — «название, API»: так sounddevice находит устройство однозначно,
    даже если номера устройств поменялись. WDM-KS пропускаем — с ним часто сбои.
    """
    hostapis = sd.query_hostapis()
    out = []
    for d in sd.query_devices():
        api = hostapis[d["hostapi"]]["name"]
        if d["max_input_channels"] > 0 and "WDM-KS" not in api:
            out.append((f"{d['name'].strip()} [{api}]", f"{d['name']}, {api}"))
    return out


def loopback_devices():
    """Названия устройств вывода, звук которых можно слушать (WASAPI loopback)."""
    import pyaudiowpatch as pyaudio
    pa = pyaudio.PyAudio()
    try:
        return [d["name"] for d in pa.get_loopback_device_info_generator()]
    finally:
        pa.terminate()


class Recorder:
    """Микрофон: пишет, пока зажата клавиша."""

    def __init__(self, device):
        self.frames = []
        self.recording = False
        try:
            self.rate = SAMPLE_RATE
            self.stream = self._open(device)
        except sd.PortAudioError:
            # WASAPI и некоторые драйверы работают только на «родной» частоте
            # устройства (обычно 48 кГц) — пишем на ней и пересчитываем в 16 кГц
            self.rate = int(sd.query_devices(device, "input")["default_samplerate"])
            self.stream = self._open(device)
        name = sd.query_devices(self.stream.device)["name"]
        print(f"Использую микрофон: {self.stream.device}: {name} ({self.rate} Гц)")
        self.stream.start()

    def _open(self, device):
        return sd.InputStream(samplerate=self.rate, channels=1, dtype="float32",
                              callback=self._cb, device=device)

    def _cb(self, indata, frames, t, status):
        if self.recording:
            self.frames.append(indata.copy())

    def start(self):
        self.frames = []
        self.recording = True

    def stop(self):
        """Дописывает хвост фразы (0.15 с) и отдаёт запись. Ждёт — не вызывать из UI-потока."""
        time.sleep(0.15)
        self.recording = False
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        return to_16k(np.concatenate(self.frames).flatten(), self.rate)

    def cancel(self):
        """Прервать запись без ожидания (запись выбрасывается)."""
        self.recording = False
        self.frames = []

    def close(self):
        self.recording = False
        self.stream.stop()
        self.stream.close()


def open_mic(device):
    """Открывает микрофон; если выбранный не открылся — микрофон по умолчанию."""
    try:
        return Recorder(device)
    except Exception as e:
        if device is None:
            raise
        print(f"[!] Не удалось открыть микрофон {device!r}: {e}. Беру микрофон по умолчанию.")
        return Recorder(None)


class LoopbackRecorder:
    """Звук компьютера (то, что слышно в наушниках): всегда держит последние N секунд."""

    def __init__(self, seconds, device):
        import pyaudiowpatch as pyaudio
        self.pa = pyaudio.PyAudio()
        try:
            self._continue = pyaudio.paContinue
            dev = self._find_device(pyaudio, device)
            self.channels = max(1, int(dev["maxInputChannels"]))
            self.rate = int(dev["defaultSampleRate"])
            self.max_samples = int(seconds * self.rate)
            self.buf = collections.deque()
            self.size = 0
            self.lock = threading.Lock()
            self.taps = []  # очереди, куда копируется весь звук (для живого перевода)
            self.stream = self.pa.open(
                format=pyaudio.paInt16, channels=self.channels, rate=self.rate,
                input=True, input_device_index=dev["index"], frames_per_buffer=1024,
                stream_callback=self._cb,
            )
        except Exception:
            self.pa.terminate()
            raise
        print(f"Слушаю звук собеседников с: {dev['index']}: {dev['name']}")

    def _find_device(self, pyaudio, device):
        loopbacks = list(self.pa.get_loopback_device_info_generator())
        print("Устройства вывода звука:")
        for d in loopbacks:
            print(f"  {d['index']}: {d['name']}")
        if not loopbacks:
            raise RuntimeError("не найдено ни одного устройства вывода")
        if device is not None:
            for d in loopbacks:
                if (isinstance(device, int) and d["index"] == device) or \
                   (isinstance(device, str) and device.lower() in d["name"].lower()):
                    return d
            print(f"[!] Устройство вывода {device!r} не найдено, беру по умолчанию.")
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for d in loopbacks:
            if default_out["name"] in d["name"]:
                return d
        return loopbacks[0]

    def _cb(self, in_data, frame_count, time_info, status):
        a = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
        a = a.reshape(-1, self.channels).mean(axis=1)
        for q in self.taps:
            q.put(a)
        with self.lock:
            self.buf.append(a)
            self.size += len(a)
            while self.size > self.max_samples and len(self.buf) > 1:
                self.size -= len(self.buf.popleft())
        return (None, self._continue)

    def set_seconds(self, seconds):
        with self.lock:
            self.max_samples = int(seconds * self.rate)

    def take(self):
        """Забирает накопленный звук (в 16 кГц) и очищает буфер."""
        with self.lock:
            if not self.buf:
                return np.zeros(0, dtype=np.float32)
            a = np.concatenate(list(self.buf))
            self.buf.clear()
            self.size = 0
        return to_16k(a[-self.max_samples:], self.rate)

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.pa.terminate()


# ======================= ОВЕРЛЕЙ =======================

def make_click_through(win):
    """Окно не ловит клики/фокус и не появляется на панели задач (Windows)."""
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    hwnd = user32.GetParent(win.winfo_id())
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


class Overlay:
    KEY = "#010101"  # этот цвет становится прозрачным

    def __init__(self, win, y_from_top=None, y_from_bottom=None):
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.config(bg=self.KEY)
        win.attributes("-transparentcolor", self.KEY)
        self.canvas = tk.Canvas(win, bg=self.KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        # размер окна задаём сами; иначе pack подгоняет окно под холст и
        # сбрасывает его положение при изменении высоты
        win.pack_propagate(False)
        self.current = None  # (текст, цвет), что сейчас на экране
        self.hide_job = None
        self.place(y_from_top, y_from_bottom)
        win.update_idletasks()
        make_click_through(win)

    def place(self, y_from_top=None, y_from_bottom=None):
        """Положение, размер и прозрачность из текущих настроек (можно вызывать на ходу)."""
        if y_from_top is not None or y_from_bottom is not None:
            self.anchor = (y_from_top, y_from_bottom)
        win = self.win
        line = FONT_SIZE * win.winfo_fpixels("1p") * 1.4  # высота строки в пикселях
        self.w = int(win.winfo_screenwidth() * 0.8)
        self.base_h = max(180, int(line * 3 + 20))  # 3 строки; длинный текст — окно растёт
        self._resize(self.base_h)
        win.attributes("-alpha", TEXT_OPACITY)
        if self.current:
            self._draw(*self.current)

    def _resize(self, h):
        """Высота окна; снизу привязанное окно растёт вверх, сверху — вниз."""
        self.h = h
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        y_from_top, y_from_bottom = self.anchor
        y = y_from_top if y_from_top is not None else sh - y_from_bottom - h
        self.canvas.config(width=self.w, height=h)
        self.win.geometry(f"{self.w}x{h}+{(sw - self.w) // 2}+{max(0, y)}")

    def _draw(self, text, color):
        c = self.canvas
        c.delete("all")
        opts = dict(text=text, font=FONT, width=self.w - 40, justify="center")
        probe = c.create_text(0, 0, anchor="nw", **opts)  # измеряем высоту текста
        _, y1, _, y2 = c.bbox(probe)
        c.delete(probe)
        need = max(self.base_h, y2 - y1 + 24)
        if need != self.h:
            self._resize(need)
        cx, cy = self.w // 2, self.h // 2
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                if dx or dy:
                    c.create_text(cx + dx, cy + dy, fill=OUTLINE_COLOR, **opts)
        c.create_text(cx, cy, fill=color, **opts)

    def show(self, text, timeout=None, color=None):
        self.current = (text, color or TEXT_COLOR)
        # сначала поверх всех, потом рисуем: -topmost после смены геометрии
        # возвращает окно на прежнее место
        self.win.attributes("-topmost", True)
        self.win.lift()
        self._draw(*self.current)
        if self.hide_job:
            self.win.after_cancel(self.hide_job)
            self.hide_job = None
        if timeout:
            self.hide_job = self.win.after(int(timeout * 1000), self.hide)

    def hide(self):
        self.canvas.delete("all")
        self.current = None
        if self.hide_job:
            self.win.after_cancel(self.hide_job)
        self.hide_job = None


# ======================= ТРЕЙ =======================

def draw_icon(size, paused=False):
    """Иконка программы в стиле логотипа Kat: тёмный скруглённый квадрат, белый
    контур кошачьей головы с фиолетовыми ушками и буква «K». Рисуется в 4 раза
    крупнее и уменьшается — так края гладкие. paused=True — серая (пауза в трее).
    """
    from PIL import Image, ImageDraw, ImageFont
    S = size * 4
    u = S / 100  # одна сотая стороны

    def P(points):
        return [(x * u, y * u) for x, y in points]

    def curve(p0, p1, p2, n=12):  # квадратичная кривая Безье
        return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                 (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1])
                for t in (i / n for i in range(n + 1))]

    purple = (150, 150, 150, 255) if paused else (139, 92, 246, 255)
    white = (225, 225, 225, 255) if paused else (244, 238, 255, 255)
    top, bottom = ((40, 40, 44), (18, 18, 20)) if paused else ((34, 22, 58), (11, 8, 20))

    # --- фон: тёмный градиент сверху вниз в скруглённом квадрате ---
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((2 * u, 2 * u, 98 * u, 98 * u), radius=22 * u, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)

    # --- кошачья голова: фиолетовые ушки + белый контур ---
    left = (curve((15, 50), (14, 24), (21, 13)) + curve((21, 13), (24, 10), (28, 13))
            + curve((28, 13), (38, 20), (43, 27)))
    head = curve((43, 27), (50, 24), (57, 27))
    right = [(100 - x, y) for x, y in reversed(left)]
    outline = left + head[1:] + right[1:]
    d.polygon(P(curve((24, 22), (25, 34), (27, 42)) + [(40, 31)]), fill=purple)
    d.polygon(P(curve((76, 22), (75, 34), (73, 42)) + [(60, 31)]), fill=purple)
    d.line(P(outline), fill=white, width=round(6.5 * u), joint="curve")
    r = 3.25 * u  # скруглённые концы линии
    for x, y in (outline[0], outline[-1]):
        d.ellipse((x * u - r, y * u - r, x * u + r, y * u + r), fill=white)

    # --- буква K ---
    font = None
    for name in ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf"):
        try:
            font = ImageFont.truetype(name, round(54 * u))
            break
        except OSError:
            pass
    d.text((50 * u, 64 * u), "K", font=font or ImageFont.load_default(), anchor="mm", fill=white)

    # --- фиолетовая дуга снизу: на мелких размерах только мешает ---
    if size >= 32:
        d.arc((12 * u, 28 * u, 88 * u, 93 * u), 30, 150, fill=purple, width=round(5 * u))

    return img.resize((size, size), Image.LANCZOS)


def tray_image(paused):
    """Значок для трея (на паузе — серый)."""
    return draw_icon(64, paused)


# ======================= ЗАПУСК В WINDOWS =======================

SHORTCUT_NAME = APP_NAME + ".lnk"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "VoiceTranslator"
TASK_NAME = "VoiceTranslator"  # задача планировщика — для автозапуска от администратора
MUTEX_NAME = "voice-translator-single-instance"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: служебные команды без мелькающей консоли


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def launcher():
    """Чем запускать программу без консоли: Kat.exe или pythonw.exe из venv."""
    if FROZEN:
        return sys.executable
    if os.path.exists(PYTHONW):
        return PYTHONW
    return os.path.join(os.path.dirname(sys.executable), "pythonw.exe")


def launch_args(*extra):
    """Аргументы для launcher(): у Kat.exe скрипта нет, у pythonw — путь к translator.py."""
    return " ".join(([] if FROZEN else [f'"{SCRIPT}"']) + list(extra))


def make_icon_file():
    """Файл иконки для ярлыков и окна настроек: каждый размер нарисован отдельно."""
    if not os.path.exists(ICON_FILE):
        sizes = [256, 128, 64, 48, 40, 32, 24, 20, 16]
        images = [draw_icon(s) for s in sizes]
        images[0].save(ICON_FILE, format="ICO", sizes=[(s, s) for s in sizes],
                       append_images=images[1:])
    # старые версии иконки больше не нужны
    for old in glob.glob(os.path.join(BASE_DIR, "icon*.ico*")):
        if os.path.abspath(old) != ICON_FILE:
            try:
                os.remove(old)
            except OSError:
                pass
    return ICON_FILE


def create_shortcuts(only_existing=False):
    """Ярлыки на рабочем столе и в меню «Пуск» (пересоздаются — если папку перенесли).

    only_existing=True — только обновить уже существующие (новые не создавать).
    """
    make_icon_file()
    ps = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
        "$sh = New-Object -ComObject WScript.Shell;"
        "foreach ($dir in @([Environment]::GetFolderPath('Desktop'),"
        "                   [Environment]::GetFolderPath('Programs'))) {"
        "  $path = Join-Path $dir $env:VT_NAME; $had = Test-Path $path;"
        "  foreach ($old in $env:VT_OLD.Split('|')) { if (-not $old) { continue };"
        "    $op = Join-Path $dir $old; if (Test-Path $op) { Remove-Item $op -Force; $had = $true } };"
        "  if ($env:VT_ONLY -eq '1' -and -not $had) { continue };"
        "  $lnk = $sh.CreateShortcut($path);"
        "  $lnk.TargetPath = $env:VT_TARGET; $lnk.Arguments = $env:VT_ARGS;"
        "  $lnk.WorkingDirectory = $env:VT_DIR; $lnk.IconLocation = $env:VT_ICON;"
        "  $lnk.Description = $env:VT_DESC; $lnk.Save(); Write-Output $lnk.FullName }"
    )
    # пути передаём через переменные окружения — так не мешают пробелы и кириллица
    env = dict(os.environ, VT_NAME=SHORTCUT_NAME, VT_TARGET=launcher(),
               VT_ARGS=launch_args(), VT_DIR=BASE_DIR, VT_ICON=f"{ICON_FILE},0",
               VT_DESC="Kat — голосовой переводчик для игр (F9 — говорить, F10 — перевести собеседника)",
               VT_OLD="|".join(n + ".lnk" for n in OLD_APP_NAMES),
               VT_ONLY="1" if only_existing else "0")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       env=env, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"не удалось создать ярлыки: {r.stderr.strip()}")
    for path in r.stdout.split("\n"):
        if path.strip():
            print(f"Ярлык: {path.strip()}")
    try:  # Windows кэширует иконки — просим обновить, чтобы новая показалась сразу
        subprocess.run(["ie4uinit.exe", "-show"], creationflags=NO_WINDOW, timeout=20)
    except Exception:
        pass


def requirements_changed():
    """True, если requirements.txt поменялся после последней установки через start.bat."""
    if FROZEN:
        return False  # у Kat.exe всё нужное уже внутри
    try:
        with open(REQUIREMENTS, "rb") as f:
            want = hashlib.sha256(f.read()).hexdigest()
        with open(REQ_STAMP, encoding="utf-8-sig", errors="replace") as f:
            have = f.read().strip()
    except OSError:
        return False  # нет отметки — не мешаем (старая установка или запуск без venv)
    return want != have


def relaunch(admin=False):
    """Запускает новую копию (она дождётся закрытия этой). True — если запустилась."""
    params = launch_args("--restart", *(["--elevated"] if admin else []))
    shell = ctypes.windll.shell32
    shell.ShellExecuteW.restype = ctypes.c_ssize_t
    r = shell.ShellExecuteW(None, "runas" if admin else "open", launcher(), params, BASE_DIR, 1)
    return r > 32  # 32 и меньше — ошибка (например, в окне UAC нажали «Нет»)


def acquire_single_instance(wait):
    """Именованный мьютекс: пока он у нас, вторая копия не запустится.

    wait=True — при перезапуске ждём до 15 секунд, пока старая копия закроется.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    deadline = time.monotonic() + (15 if wait else 0)
    while True:
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()
        # 183 — мьютекс уже есть; 5 (нет доступа) — его держит копия, запущенная от админа
        if h and err != 183:
            return h
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


# --- автозапуск ---
# Обычный — запись в реестре HKCU\...\Run. От администратора так нельзя (Windows
# не поднимает права при входе), поэтому тогда — задача планировщика с наивысшими правами.

def _schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True,
                          errors="replace", creationflags=NO_WINDOW)


def _run_value():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_VALUE)[0]
    except OSError:
        return None


def _set_run_value(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{launcher()}" {launch_args()}'.strip())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


def _task_exists():
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0


def _task_xml(run_level="HighestAvailable"):
    from xml.sax.saxutils import escape as e
    user = e(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}")
    # от батареи не зависим и не ограничиваем время работы (по умолчанию — 3 дня)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{e(APP_NAME)}: запуск при входе в Windows</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT5S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>{run_level}</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{e(launcher())}</Command><Arguments>{e(launch_args("--elevated"))}</Arguments><WorkingDirectory>{e(BASE_DIR)}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _create_task(run_level="HighestAvailable", name=TASK_NAME):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(_task_xml(run_level))
        r = _schtasks("/Create", "/TN", name, "/XML", path, "/F")
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
    finally:
        os.remove(path)


def autostart_enabled():
    return _run_value() is not None or _task_exists()


def set_autostart(on):
    if on:
        if is_admin():
            _create_task()
            _set_run_value(False)
            print("[автозапуск] включён (задача планировщика, от администратора)")
        else:
            _set_run_value(True)
            print("[автозапуск] включён (реестр, обычные права)")
        return
    _set_run_value(False)
    if _task_exists():
        if not is_admin():
            raise RuntimeError("он был включён в режиме администратора — выключить его можно "
                               "только из программы, запущенной от администратора.")
        r = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
    print("[автозапуск] выключен")


# ======================= ОБНОВЛЕНИЯ =======================

def _version_tuple(v):
    """«v1.2.3» / «1.2» -> (1, 2, 3) / (1, 2, 0) — для сравнения версий."""
    nums = [int(x) for x in re.findall(r"\d+", v)[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def check_for_update():
    """Последний релиз на GitHub: (версия, ссылка, что нового, ссылка на KatSetup.exe или None),
    если он новее этой версии, иначе None. Ошибки сети — исключением (их ловит вызывающий)."""
    import json
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers={"User-Agent": f"Kat/{APP_VERSION}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    tag = data.get("tag_name") or ""
    if not tag or _version_tuple(tag) <= _version_tuple(APP_VERSION):
        return None
    url = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest"
    setup = next((a["browser_download_url"] for a in data.get("assets", [])
                  if re.fullmatch(r"KatSetup.*\.exe", a.get("name", ""), re.I)), None)
    return tag.lstrip("vV"), url, (data.get("body") or "").strip(), setup


# ======================= ОКНО НАСТРОЕК =======================

KEY_CHOICES = [f"f{i}" for i in range(1, 13)] + [
    "insert", "home", "end", "page up", "page down", "pause", "scroll lock",
    "right ctrl", "right alt", "right shift", "caps lock",
]
EXIT_CHOICES = ["ctrl+shift+f9", "ctrl+shift+q", "ctrl+alt+f12", "ctrl+shift+end"]
DEFAULT_DEVICE = "По умолчанию (как в Windows)"


def check_single_key(name, what):
    if not name or "+" in name:
        raise ValueError(f"«{what}»: нужна одна клавиша без сочетаний, например f9.")
    try:
        keyboard.key_to_scan_codes(name)
    except Exception:
        raise ValueError(f"«{what}»: не знаю клавишу «{name}».") from None


class SettingsWindow:
    """Окно «Настройки»: клавиши, устройства и вид текста, с применением на ходу.

    Вид текста (прозрачность, шрифт, положение) показывается сразу, пока
    двигаешь ползунки; «Отмена» возвращает как было.
    """

    VISUAL = ("TEXT_OPACITY", "FONT_SIZE", "DISPLAY_SECONDS", "Y_FROM_BOTTOM", "THEM_Y_FROM_TOP",
              "THEM_SECONDS")

    def __init__(self, app):
        from tkinter import ttk
        self.app = app
        self.saved_visual = {k: globals()[k] for k in self.VISUAL}
        self.win = win = tk.Toplevel(app.root)
        win.title(f"Настройки — {APP_NAME} {APP_VERSION}")
        win.resizable(False, False)
        win.attributes("-topmost", True)  # не прятаться за игрой и оверлеем
        try:
            win.iconbitmap(make_icon_file())
        except Exception:
            pass
        win.protocol("WM_DELETE_WINDOW", self.cancel)

        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)

        # --- горячие клавиши ---
        box = ttk.LabelFrame(frm, text="Горячие клавиши", padding=10)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.keys = {}
        rows = [("HOTKEY", "Говорить (держать):", HOTKEY, False),
                ("THEM_HOTKEY", "Перевести собеседника:", THEM_HOTKEY, False),
                ("LIVE_HOTKEY", "Живой перевод (вкл/выкл):", LIVE_HOTKEY, False),
                ("EXIT_HOTKEY", "Выход из программы:", EXIT_HOTKEY, True)]
        for r, (var_name, label, value, combo) in enumerate(rows):
            ttk.Label(box, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=3)
            var = tk.StringVar(value=value)
            ttk.Combobox(box, textvariable=var, width=20,
                         values=EXIT_CHOICES if combo else KEY_CHOICES
                         ).grid(row=r, column=1, sticky="w", pady=3)
            btn = ttk.Button(box, text="Записать", width=11)
            btn.config(command=lambda v=var, b=btn, c=combo: self.capture(v, b, c))
            btn.grid(row=r, column=2, padx=(8, 0), pady=3)
            self.keys[var_name] = var

        # --- звук ---
        box = ttk.LabelFrame(frm, text="Звук", padding=10)
        box.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        try:
            mics = input_devices()
        except Exception as e:
            print(f"[!] Не удалось получить список микрофонов: {e}")
            mics = []
        try:
            outs = [(n, n) for n in loopback_devices()]
        except Exception as e:
            print(f"[!] Не удалось получить список устройств вывода: {e}")
            outs = []
        self.mic_var, self.mic_map = self._device_row(box, 0, "Микрофон:", mics, MIC_DEVICE)
        self.out_var, self.out_map = self._device_row(
            box, 1, "Звук собеседников\n(устройство вывода):", outs, LOOPBACK_DEVICE)
        self.nums = {}
        self._num_row(box, 2, "THEM_SECONDS", "Собеседник: сколько\nпоследних секунд переводить:",
                      2, 30, 1)

        # --- текст на экране ---
        # --- языки ---
        box = ttk.LabelFrame(frm, text="Языки", padding=10)
        box.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.NO_EXTRA = "Нет — только английский"
        self.lang_map = {self.NO_EXTRA: ""}
        for code in sorted(LANGUAGES, key=LANGUAGES.get):
            self.lang_map[LANGUAGES[code] + ("  ✓" if lang_downloaded(code) else "")] = code
        current = next((k for k, c in self.lang_map.items() if c == EXTRA_LANG), self.NO_EXTRA)
        ttk.Label(box, text="Ещё один язык собеседников\n(кроме английского):"
                  ).grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        self.extra_var = tk.StringVar(value=current)
        ttk.Combobox(box, textvariable=self.extra_var, values=list(self.lang_map), width=28,
                     state="readonly", height=15).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(box, text="Мою речь (F9) переводить на:"
                  ).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        self.target_var = tk.StringVar(
            value=lang_name(TARGET_LANG) if TARGET_LANG == EXTRA_LANG and EXTRA_LANG else "Английский")
        self.target_box = ttk.Combobox(box, textvariable=self.target_var, width=28, state="readonly")
        self.target_box.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(box, foreground="#666", justify="left",
                  text="✓ — уже скачан. Новый язык скачается после «Применить» (~700 МБ).\n"
                       "С русского и на русский переводится через английский."
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.extra_var.trace_add("write", lambda *_: self._update_targets())
        self._update_targets()

        box = ttk.LabelFrame(frm, text="Текст на экране", padding=10)
        box.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(box, text="Прозрачность:").grid(row=0, column=0, sticky="w", pady=3)
        self.opacity = tk.DoubleVar(value=TEXT_OPACITY)
        ttk.Scale(box, from_=0.2, to=1.0, variable=self.opacity, length=220
                  ).grid(row=0, column=1, sticky="w", pady=3)
        self.opacity_label = ttk.Label(box, width=5)
        self.opacity_label.grid(row=0, column=2, sticky="w", padx=(8, 0))
        sh = win.winfo_screenheight()
        spins = [("FONT_SIZE", "Размер шрифта:", 8, 96, 1),
                 ("DISPLAY_SECONDS", "Время показа, секунд:", 1, 60, 1),
                 ("Y_FROM_BOTTOM", "Мой перевод — отступ снизу, пикс.:", 0, sh, 10),
                 ("THEM_Y_FROM_TOP", "Перевод собеседника — отступ сверху, пикс.:", 0, sh, 10)]
        for r, spin in enumerate(spins, start=1):
            self._num_row(box, r, *spin)
        ttk.Label(box, text="Пока окно открыто, на экране показан пример текста.",
                  foreground="#666").grid(row=len(spins) + 1, column=0, columnspan=3,
                                          sticky="w", pady=(6, 0))

        # --- запуск ---
        box = ttk.LabelFrame(frm, text="Запуск", padding=10)
        box.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        try:
            self.autostart_was = autostart_enabled()
        except Exception as e:
            print(f"[!] Не удалось узнать состояние автозапуска: {e}")
            self.autostart_was = False
        self.autostart = tk.BooleanVar(value=self.autostart_was)
        ttk.Checkbutton(box, text="Запускать вместе с Windows", variable=self.autostart
                        ).grid(row=0, column=0, sticky="w", pady=2)
        self.admin = tk.BooleanVar(value=RUN_AS_ADMIN)
        ttk.Checkbutton(box, text="Запускать от имени администратора", variable=self.admin
                        ).grid(row=1, column=0, sticky="w", pady=2)
        now = "сейчас запущен от администратора" if is_admin() else "сейчас запущен с обычными правами"
        ttk.Label(box, foreground="#666", justify="left",
                  text="Нужно, если игра запущена от администратора (например Genshin Impact) —\n"
                       f"иначе F9/F10 внутри игры не срабатывают. ({now})"
                  ).grid(row=2, column=0, sticky="w", padx=(22, 0))
        self.updates = tk.BooleanVar(value=CHECK_UPDATES)
        ttk.Checkbutton(box, text="Проверять обновления при запуске", variable=self.updates
                        ).grid(row=3, column=0, sticky="w", pady=(6, 2))

        # --- кнопки ---
        bar = ttk.Frame(frm)
        bar.grid(row=5, column=0, sticky="ew")
        self.status = ttk.Label(bar, foreground="#2E7D32")
        self.status.pack(side="left")
        ttk.Button(bar, text="Применить", command=lambda: self.apply(False)).pack(side="right")
        ttk.Button(bar, text="Отмена", command=self.cancel).pack(side="right", padx=6)
        ttk.Button(bar, text="ОК", command=lambda: self.apply(True)).pack(side="right")

        for var in [self.opacity] + [v for v, *_ in self.nums.values()]:
            var.trace_add("write", lambda *_: self.preview())
        self.preview()

        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_width()) // 2
        y = (win.winfo_screenheight() - win.winfo_height()) // 3
        win.geometry(f"+{x}+{y}")
        self.focus()

    def focus(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _update_targets(self):
        """Список «переводить мою речь на»: английский + выбранный доп. язык."""
        extra = self.lang_map.get(self.extra_var.get(), "")
        values = ["Английский"] + ([lang_name(extra)] if extra else [])
        self.target_box.config(values=values)
        if self.target_var.get() not in values:
            self.target_var.set("Английский")

    def _num_row(self, box, row, var_name, label, lo, hi, step):
        from tkinter import ttk
        ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        var = tk.StringVar(value=_format(globals()[var_name], "float"))
        ttk.Spinbox(box, from_=lo, to=hi, increment=step, textvariable=var, width=8
                    ).grid(row=row, column=1, sticky="w", pady=3)
        self.nums[var_name] = (var, lo, hi, " ".join(label.rstrip(":").split()))

    def _device_row(self, box, row, label, options, current):
        from tkinter import ttk
        options = [(DEFAULT_DEVICE, None)] + options
        labels = [lbl for lbl, _ in options]
        selected = next((lbl for lbl, v in options if v == current), None)
        if selected is None:  # в settings.ini номер или часть названия, введённые вручную
            selected = f"{current} (из settings.ini)"
            options.append((selected, current))
            labels.append(selected)
        var = tk.StringVar(value=selected)
        ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        ttk.Combobox(box, textvariable=var, values=labels, state="readonly", width=52
                     ).grid(row=row, column=1, sticky="w", pady=3)
        return var, dict(options)

    # ---------- запись горячей клавиши ----------
    def capture(self, var, btn, combo):
        app = self.app
        if app.state["capturing"]:
            return
        app.state["capturing"] = True  # пока ждём клавишу, F9/F10/выход не срабатывают
        btn.config(text="Нажмите…", state="disabled")

        def worker():
            name = None
            try:
                if combo:
                    name = keyboard.read_hotkey(suppress=False)
                else:
                    while True:
                        ev = keyboard.read_event(suppress=False)
                        if ev.event_type == keyboard.KEY_DOWN:
                            name = ev.name
                            break
            except Exception as e:
                print(f"[!] Не удалось записать клавишу: {e}")

            def done():
                app.state["capturing"] = False
                if btn.winfo_exists():
                    btn.config(text="Записать", state="normal")
                    if name:
                        var.set(name.lower())
            app.call_in_ui(done)

        threading.Thread(target=worker, daemon=True, name="capture-key").start()

    # ---------- вид текста ----------
    def read_visual(self):
        """Значения вида текста из полей окна; ValueError — если что-то не так."""
        v = {"TEXT_OPACITY": round(float(self.opacity.get()), 2)}
        for var_name, (var, lo, hi, label) in self.nums.items():
            text = var.get().strip().replace(",", ".")
            try:
                num = float(text)
            except ValueError:
                raise ValueError(f"«{label}»: нужно число.") from None
            if not lo <= num <= hi:
                raise ValueError(f"«{label}»: число от {lo} до {hi}.")
            v[var_name] = num if var_name in ("DISPLAY_SECONDS", "THEM_SECONDS") else int(num)
        return v

    def show_visual(self, values):
        set_settings(values)
        ov = self.app.overlays
        ov["me"].place(y_from_bottom=Y_FROM_BOTTOM)
        ov["them"].place(y_from_top=THEM_Y_FROM_TOP)

    def preview(self):
        self.opacity_label.config(text=f"{self.opacity.get():.2f}")
        try:
            values = self.read_visual()
        except ValueError:
            return  # число ещё набирают — подождём
        self.show_visual(values)
        ov = self.app.overlays
        ov["me"].show("Пример: мой перевод\nExample: my translation", None, TEXT_COLOR)
        ov["them"].show("Пример: перевод собеседника\nExample: teammate", None, THEM_COLOR)

    # ---------- кнопки ----------
    def apply(self, close):
        from tkinter import messagebox
        app = self.app
        try:
            visual = self.read_visual()
            hk = self.keys["HOTKEY"].get().strip().lower()
            th = self.keys["THEM_HOTKEY"].get().strip().lower()
            lv = self.keys["LIVE_HOTKEY"].get().strip().lower()
            ex = self.keys["EXIT_HOTKEY"].get().strip().lower()
            check_single_key(hk, "Говорить")
            check_single_key(th, "Перевести собеседника")
            check_single_key(lv, "Живой перевод")
            if len({hk, th, lv}) < 3:
                raise ValueError("Клавиши «Говорить», «Перевести собеседника» и "
                                 "«Живой перевод» должны быть разными.")
            try:
                keyboard.parse_hotkey(ex)
            except Exception:
                raise ValueError(f"Не понимаю сочетание для выхода: «{ex}».") from None
        except ValueError as e:
            messagebox.showerror("Настройки", str(e), parent=self.win)
            return

        problems = []
        extra = self.lang_map.get(self.extra_var.get(), "")
        if extra and extra != EXTRA_LANG and not lang_downloaded(extra) and not messagebox.askyesno(
                "Настройки", f"Скачать модели перевода для языка «{lang_name(extra)}»?\n\n"
                             "Это около 700 МБ, займёт несколько минут. Kat продолжит работать, "
                             "ход загрузки будет виден на экране.", parent=self.win):
            extra = EXTRA_LANG  # отказались — язык не меняем
            self.extra_var.set(next(k for k, c in self.lang_map.items() if c == extra))
        target = "en" if not extra or self.target_var.get() == "Английский" else extra
        extra_changed = extra != EXTRA_LANG
        mic = self.mic_map[self.mic_var.get()]
        out = self.out_map[self.out_var.get()]
        if not app.switch_mic(mic, problems):
            mic = MIC_DEVICE
        if not app.switch_loopback(out, problems):
            out = LOOPBACK_DEVICE

        old_keys = {"HOTKEY": HOTKEY, "THEM_HOTKEY": THEM_HOTKEY, "LIVE_HOTKEY": LIVE_HOTKEY,
                    "EXIT_HOTKEY": EXIT_HOTKEY}
        set_settings({"HOTKEY": hk, "THEM_HOTKEY": th, "LIVE_HOTKEY": lv, "EXIT_HOTKEY": ex})
        try:
            app.register_hotkeys()
        except Exception as e:
            problems.append(f"Горячие клавиши не применились ({e}), оставил прежние.")
            set_settings(old_keys)
            app.register_hotkeys()

        if self.autostart.get() != self.autostart_was:
            try:
                set_autostart(self.autostart.get())
                self.autostart_was = self.autostart.get()
            except Exception as e:
                problems.append(f"Автозапуск не изменён: {e}")
                self.autostart.set(self.autostart_was)
        want_admin, admin_was = self.admin.get(), RUN_AS_ADMIN

        values = {**visual, **{k: globals()[k] for k in old_keys},
                  "MIC_DEVICE": mic, "LOOPBACK_DEVICE": out, "RUN_AS_ADMIN": want_admin,
                  "CHECK_UPDATES": self.updates.get(),
                  "EXTRA_LANG": extra, "TARGET_LANG": target}
        self.show_visual(values)
        self.saved_visual = {k: globals()[k] for k in self.VISUAL}
        app.update_tray()
        if extra_changed:
            app.set_extra_language(extra)  # в меню трея указана клавиша живого перевода
        if app.state["loop"] is not None:
            app.state["loop"].set_seconds(THEM_SECONDS)
        try:
            save_settings(values)
            print(f"[настройки] сохранены: {', '.join(f'{k}={v!r}' for k, v in values.items())}")
        except OSError as e:
            problems.append(f"Не удалось сохранить {SETTINGS_FILE}: {e}")

        if problems:
            messagebox.showwarning("Настройки", "\n\n".join(problems), parent=self.win)
        if want_admin and not admin_was and not is_admin() and messagebox.askyesno(
                "Настройки", "Перезапустить программу от имени администратора сейчас?",
                parent=self.win):
            self.close()
            app.restart(admin=True)
            return
        if close:
            self.close()
        else:
            self.status.config(text="Сохранено и применено")
            self.win.after(3000, lambda: self.win.winfo_exists() and self.status.config(text=""))

    def cancel(self):
        self.show_visual(self.saved_visual)
        self.close()

    def close(self):
        self.app.overlays["me"].hide()
        self.app.overlays["them"].hide()
        self.win.destroy()
        self.app.settings_window = None


# ======================= ГЛАВНОЕ =======================

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.overlays = {
            "me": Overlay(self.root, y_from_bottom=Y_FROM_BOTTOM),
            "them": Overlay(tk.Toplevel(self.root), y_from_top=THEM_Y_FROM_TOP),
        }
        self.ui_q = queue.Queue()
        self.state = {"model": None, "mt": {}, "hints": None,
                      "rec": None, "loop": None, "busy_me": False, "busy_them": False,
                      "paused": False, "capturing": False, "live": LIVE}
        self.model_lock = threading.Lock()  # видеокарта считает одну фразу за раз
        self.live_lines = []  # живой перевод: (время, текст) последних фраз на экране
        self.live_key_down = False  # клавиша живого перевода нажата (защита от автоповтора)
        self.them_hold_until = 0.0  # до этого момента на экране результат F10 — не перекрывать
        self.live_phrase_lang = None  # язык текущей фразы (определяется один раз на фразу)
        self.update_info = None  # (версия, ссылка, что нового), если на GitHub есть новее
        self.glossary = None  # словарь glossary.txt — общий для всех моделей перевода
        self.settings_window = None
        self.tray = None

    # ---------- связь с окном (tkinter можно трогать только из главного потока) ----------
    def show(self, who, text, timeout=None, color=None):
        self.ui_q.put(("show", who, text, timeout, color or STATUS_COLOR))

    def call_in_ui(self, fn):
        self.ui_q.put(("call", fn))

    def poll(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                if msg[0] == "show":
                    self.overlays[msg[1]].show(msg[2], msg[3], msg[4])
                elif msg[0] == "hide":
                    self.overlays[msg[1]].hide()
                elif msg[0] == "clip":
                    self.root.clipboard_clear()
                    self.root.clipboard_append(msg[1])
                    self.root.update()
                elif msg[0] == "call":
                    msg[1]()
                elif msg[0] == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
        self.root.after(40, self.poll)

    # ---------- запуск ----------
    def run(self):
        global _download_hook
        self.show("me", "Загружаю модели...")
        print("Загружаю модели (первый запуск скачает их, ~3.5 ГБ)...")
        _download_hook = lambda text: self.show("me", text)  # noqa: E731
        threading.Thread(target=self.loader, daemon=True, name="loader").start()

        list_mics()
        try:
            self.state["rec"] = open_mic(MIC_DEVICE)
        except Exception as e:
            print(f"[!] Микрофон не открылся: {e}. {HOTKEY.upper()} отключена — "
                  "выберите микрофон в настройках.")
        try:
            self.state["loop"] = LoopbackRecorder(THEM_SECONDS, LOOPBACK_DEVICE)
        except Exception as e:
            print(f"[!] Не удалось слушать звук компьютера: {e}. "
                  f"{THEM_HOTKEY.upper()} отключена.")

        self.register_hotkeys()
        self.start_tray()
        threading.Thread(target=self.live_worker, daemon=True, name="live").start()
        self.poll()
        self.root.mainloop()

    def loader(self):
        global _download_hook
        st = self.state
        try:
            try:
                ensure_cuda_libs()
            except Exception as e:
                print(f"[!] Не удалось скачать библиотеки CUDA ({e}) — работаю на процессоре.")
            model, desc = load_whisper()
            st["hints"] = Hints(model.hf_tokenizer)
            st["hints"].get(SOURCE_LANG)  # прочитать words.txt и отчитаться в логе
            glossary = Glossary()
            glossary._safe_reload()  # создать glossary.txt и отчитаться в логе
            self.glossary = glossary
            if SOURCE_LANG != "en":
                try:
                    self.load_mt(f"{SOURCE_LANG}-en")
                except Exception as e:
                    print(f"[!] Переводчик {SOURCE_LANG.upper()}->EN не загрузился: {e}. "
                          "Перевожу свою речь через Whisper (медленнее).")
            st["model"] = model
            try:
                if SOURCE_LANG != "en":
                    self.load_mt(f"en-{SOURCE_LANG}")
                them_info = f", {THEM_HOTKEY.upper()} — перевести собеседника"
            except Exception as e:
                print(f"[!] Переводчик EN->{SOURCE_LANG.upper()} не загрузился: {e}")
                them_info = ""
            if EXTRA_LANG in LANGUAGES:
                try:
                    for d in (f"{EXTRA_LANG}-en", f"en-{EXTRA_LANG}"):
                        self.load_mt(d)
                    print(f"Дополнительный язык: {lang_name(EXTRA_LANG)}")
                except Exception as e:
                    print(f"[!] Модели для языка {lang_name(EXTRA_LANG)} не загрузились: {e}")
            print(f"Готово ({desc}). Держи {HOTKEY.upper()} и говори{them_info}, "
                  f"{LIVE_HOTKEY.upper()} — живой перевод. "
                  f"Выход: {EXIT_HOTKEY} или через иконку в трее.")
            if st["live"]:
                print("[живой] живой перевод включён")
            self.show("me", f"Готово ({desc}). {HOTKEY.upper()} — говорить{them_info}", 5)
            if CHECK_UPDATES:
                threading.Thread(target=self.update_checker, daemon=True, name="updates").start()
        except Exception as e:
            traceback.print_exc()
            self.show("me", f"Не удалось загрузить модели: {e}\nПодробности в log.txt", 20)
        finally:
            _download_hook = None

    # ---------- горячие клавиши ----------
    def register_hotkeys(self):
        keyboard.unhook_all()
        keyboard.on_press_key(HOTKEY, self.on_press)
        keyboard.on_release_key(HOTKEY, self.on_release)
        keyboard.on_press_key(THEM_HOTKEY, self.on_them)
        keyboard.on_press_key(LIVE_HOTKEY, self.on_live)
        keyboard.on_release_key(LIVE_HOTKEY, self.on_live_release)
        keyboard.add_hotkey(EXIT_HOTKEY, self.on_exit_hotkey)

    def on_exit_hotkey(self):
        if not self.state["capturing"]:
            self.ui_q.put(("quit",))

    # ---------- распознавание и перевод ----------
    # Фразы, которые Whisper «слышит» в тишине и шуме (выучил из субтитров к видео).
    HALLUCINATION_MARKS = ("субтитр", "dimatorzok", "amara.org", "продолжение следует",
                           "подписывайтесь на канал", "спасибо за просмотр",
                           "thanks for watching", "thank you for watching",
                           "please subscribe")
    # А эти бывают и настоящими — выкидываем, только если VAD речи не нашёл.
    HALLUCINATION_NO_VAD = {"thank you", "thanks", "you", "bye", "the end", "спасибо",
                            "пока", "до свидания", "угу", "see you next time",
                            "i'll see you next time", "see you"}

    def _keep_segment(self, seg, vad):
        text = seg.text.strip()
        low = text.lower()
        if not _norm_phrase(text) or any(m in low for m in self.HALLUCINATION_MARKS):
            return False
        # зацикливание («Pudge, Pudge, Pudge...») — текст сильно сжимается; порог как в Whisper
        if seg.compression_ratio > 2.4:
            return False
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:  # правило из самого Whisper
            return False
        # без VAD — строже: речи VAD не нашёл, а turbo в тишине выдумывает уверенно
        # (и no_speech_prob у turbo всегда около 0, на него не опереться)
        if not vad and (seg.avg_logprob < -0.7 or seg.compression_ratio > 2.0
                        or _norm_phrase(text) in self.HALLUCINATION_NO_VAD):
            return False
        return True

    def transcribe(self, audio, lang, task, beam=5):
        model = self.state["model"]
        # подсказки берём на языке результата: при переводе Whisper'ом это английский
        hint_lang = "en" if task == "translate" else lang
        for vad in (True, False):  # если VAD всё отрезал — пробуем без него
            with self.model_lock:
                # без VAD (речь не найдена) — без подсказок: на шуме Whisper начинает
                # выдумывать слова из них («Pudge, Pudge...»)
                hotwords = self.state["hints"].get(hint_lang) if vad else None
                segs, _ = model.transcribe(audio, language=lang, task=task, beam_size=beam,
                                           vad_filter=vad, condition_on_previous_text=False,
                                           hotwords=hotwords)
                segs = list(segs)
            kept = [s for s in segs if self._keep_segment(s, vad)]
            if len(kept) < len(segs):
                dropped = " | ".join(s.text.strip() for s in segs if s not in kept)
                print(f"[i] отброшено как шум/выдумка Whisper: {dropped}")
            t = " ".join(s.text.strip() for s in kept).strip()
            if t:
                return t
        return ""

    def detect_lang(self, audio, choices):
        """Какой из языков choices звучит в записи (по вероятностям Whisper)."""
        model = self.state["model"]
        probs = None
        with self.model_lock:
            for vad in (True, False):  # если VAD речи не нашёл — по всей записи
                try:
                    _, _, probs = model.detect_language(audio, vad_filter=vad)
                    break
                except Exception:
                    continue
        if not probs:
            return choices[0]
        p = dict(probs)
        return max(choices, key=lambda lg: p.get(lg, 0.0))

    def translate(self, mt, text):
        with self.model_lock:
            return mt(text)

    # ---------- языки ----------
    def load_mt(self, direction):
        """Загрузить модель перевода quickmt-<direction> (скачать, если её нет)."""
        if direction not in self.state["mt"]:
            self.state["mt"][direction] = TextTranslator(f"quickmt-{direction}", self.glossary)

    @staticmethod
    def _chain(src, dst):
        """Шаги перевода: напрямую, если одна из сторон английский, иначе через него."""
        path = [src, dst] if "en" in (src, dst) else [src, "en", dst]
        return [f"{a}-{b}" for a, b in zip(path, path[1:])]

    def has_chain(self, src, dst):
        return src == dst or all(d in self.state["mt"] for d in self._chain(src, dst))

    def translate_chain(self, text, src, dst):
        if src == dst or not text:
            return text
        for d in self._chain(src, dst):
            text = self.translate(self.state["mt"][d], text)
        return text

    def them_langs(self):
        """На каких языках могут говорить собеседники (для определения языка):
        английский, дополнительный (если его модели готовы) и мой собственный."""
        langs = ["en"]
        if EXTRA_LANG in LANGUAGES and self.has_chain(EXTRA_LANG, SOURCE_LANG):
            langs.append(EXTRA_LANG)
        if SOURCE_LANG not in langs:
            langs.append(SOURCE_LANG)
        return langs

    def them_lang(self, audio):
        langs = self.them_langs()
        return langs[0] if len(langs) == 1 else self.detect_lang(audio, tuple(langs))

    def them_ready(self):
        return self.state["model"] is not None and self.has_chain("en", SOURCE_LANG)

    def my_target(self):
        """Язык перевода моей речи: выбранный, если его модели готовы, иначе английский."""
        t = TARGET_LANG
        if t != "en" and t == EXTRA_LANG and self.has_chain(SOURCE_LANG, t):
            return t
        return "en"

    def set_extra_language(self, code):
        """Сменить дополнительный язык (из окна настроек): старые модели выгрузить,
        новые — скачать и загрузить в фоне."""
        mt = self.state["mt"]
        for d in list(mt):
            if any(c not in ("en", SOURCE_LANG, code) for c in d.split("-")):
                del mt[d]
        if code:
            threading.Thread(target=self._install_language, args=(code,),
                             daemon=True, name="language").start()

    def _install_language(self, code):
        global _download_hook
        if self.state["model"] is None:  # модели ещё грузятся — язык подхватит loader
            return
        name = lang_name(code)
        self.show("me", f"{name}: загружаю модели перевода...")
        _download_hook = lambda text: self.show("me", f"{name}: {text}")  # noqa: E731
        try:
            for d in (f"{code}-en", f"en-{code}"):
                self.load_mt(d)
            print(f"Дополнительный язык: {name} — готово")
            self.show("me", f"{name}: готово! Собеседников на этом языке Kat теперь переводит", 5)
        except Exception as e:
            traceback.print_exc()
            self.show("me", f"{name}: не удалось скачать модели ({e}).\n"
                            "Проверьте интернет и выберите язык в настройках ещё раз", 10)
        finally:
            _download_hook = None

    @staticmethod
    def normalize(audio):
        peak = float(np.abs(audio).max()) if len(audio) else 0.0
        if 0 < peak < 0.5:
            audio = audio * (0.9 / peak)
        return audio, peak

    # ---------- моя речь: RU -> EN ----------
    def process_me(self, rec):
        st = self.state
        try:
            audio = rec.stop()  # ждёт хвост фразы — поэтому здесь, а не в потоке клавиатуры
            if len(audio) < MIN_SECONDS * SAMPLE_RATE or st["paused"]:
                self.ui_q.put(("hide", "me"))
                return
            self.show("me", "Перевожу...")
            save_wav(audio, LAST_WAV)
            audio, peak = self.normalize(audio)
            print(f"[я] {len(audio) / SAMPLE_RATE:.1f} с, громкость {peak:.3f} (last.wav)")
            if peak < 0.005:
                print("[!] Микрофон пишет тишину. Выберите другой микрофон в настройках.")
                self.show("me", "Микрофон молчит — выберите другой в настройках", 4)
                return
            target = self.my_target()
            t0 = time.perf_counter()
            if SOURCE_LANG != "en" and self.has_chain(SOURCE_LANG, target):
                # быстрый путь: Whisper распознаёт один раз, перевод — отдельной моделью
                # (на дополнительный язык — через английский)
                ru = self.transcribe(audio, SOURCE_LANG, "transcribe")
                t1 = time.perf_counter()
                en = self.translate_chain(ru, SOURCE_LANG, target) if ru else ""
            else:
                # запасной путь (нет quickmt-ru-en или язык не русский): перевод Whisper'ом
                en = self.transcribe(audio, SOURCE_LANG, "translate")
                t1 = time.perf_counter()
                ru = (self.transcribe(audio, SOURCE_LANG, "transcribe")
                      if en and SHOW_ORIGINAL else "")
            t2 = time.perf_counter()
            print(f"[я] время: распознавание {t1 - t0:.2f} с + перевод {t2 - t1:.2f} с"
                  f" = {t2 - t0:.2f} с")
            if not en:
                self.show("me", "(ничего не расслышал)", 2)
                return
            text = f"{ru}\n{en}" if SHOW_ORIGINAL else en
            print(f"[я] {text}")
            self.show("me", text, DISPLAY_SECONDS, TEXT_COLOR)
            if COPY_TO_CLIPBOARD:
                self.ui_q.put(("clip", en))
        except Exception as e:
            traceback.print_exc()
            self.show("me", f"Ошибка: {e}", 4)
        finally:
            st["busy_me"] = False

    def on_press(self, _):
        st, rec = self.state, self.state["rec"]
        if rec is None or rec.recording or st["model"] is None or st["busy_me"] \
                or st["paused"] or st["capturing"]:
            return
        rec.start()
        self.show("me", "● Слушаю...")

    def on_release(self, _):
        rec = self.state["rec"]
        if rec is None or not rec.recording or self.state["busy_me"]:
            return
        self.state["busy_me"] = True
        threading.Thread(target=self.process_me, args=(rec,), daemon=True).start()

    # ---------- собеседники: EN -> RU ----------
    def process_them(self):
        try:
            audio = self.state["loop"].take()
            audio, peak = self.normalize(audio)
            print(f"[собеседник] {len(audio) / SAMPLE_RATE:.1f} с, громкость {peak:.3f}")
            if peak < 0.005 or len(audio) < MIN_SECONDS * SAMPLE_RATE:
                self.show("them", "(тишина)", 2)
                return
            t0 = time.perf_counter()
            # собеседники бывают и русскоязычные: такую речь показываем как есть,
            # а не прогоняем через перевод EN->RU (он её только портит)
            lang = self.them_lang(audio)
            heard = self.transcribe(audio, lang, "transcribe")
            t1 = time.perf_counter()
            if lang != SOURCE_LANG:
                ru = self.translate_chain(heard, lang, SOURCE_LANG)
            t2 = time.perf_counter()
            print(f"[собеседник] язык {lang}, время: распознавание {t1 - t0:.2f} с"
                  f" + перевод {t2 - t1:.2f} с = {t2 - t0:.2f} с")
            if not heard:
                self.show("them", "(речи не слышно)", 2)
                return
            if lang == SOURCE_LANG:
                text = heard  # уже на моём языке
            else:
                text = f"{ru}\n{heard}" if SHOW_ORIGINAL else ru
            print(f"[собеседник] {text}")
            self.them_hold_until = time.monotonic() + DISPLAY_SECONDS
            self.show("them", text, DISPLAY_SECONDS, THEM_COLOR)
        except Exception as e:
            traceback.print_exc()
            self.show("them", f"Ошибка: {e}", 4)
        finally:
            self.state["busy_them"] = False

    def on_them(self, _):
        st = self.state
        if st["loop"] is None or not self.them_ready() \
                or st["busy_them"] or st["paused"] or st["capturing"]:
            return
        st["busy_them"] = True
        self.show("them", "Перевожу собеседника...")
        threading.Thread(target=self.process_them, daemon=True).start()

    # ---------- живой перевод собеседников ----------
    LIVE_TICK = 0.2          # как часто проверяем звук, с
    LIVE_END_SILENCE = 0.45  # столько тишины после речи = фраза закончилась, с
    LIVE_MAX_PHRASE = 12.0   # длиннее — закрываем фразу, не дожидаясь паузы, с
    LIVE_MIN_SPEECH = 0.4    # короче — не речь (щелчки, выстрелы), с
    LIVE_PARTIAL_START = 0.8  # с такой длины речи показываем перевод по ходу фразы, с
    LIVE_PARTIAL_EVERY = 0.8  # и обновляем его не чаще, чем раз в столько секунд

    def on_live(self, _):
        # пока клавишу держат, Windows повторяет нажатие — переключаем только первое
        if self.live_key_down or self.state["capturing"]:
            return
        self.live_key_down = True
        self.call_in_ui(self.toggle_live)

    def on_live_release(self, _):
        self.live_key_down = False

    def toggle_live(self):
        st = self.state
        st["live"] = not st["live"]
        print("[живой] включён" if st["live"] else "[живой] выключен")
        if st["live"] and st["loop"] is None:
            msg = "Живой перевод: нет звука собеседников — выберите устройство в настройках"
        elif st["live"]:
            msg = "Живой перевод включён"
            if st["paused"]:
                msg += " (но сейчас пауза)"
        else:
            msg = "Живой перевод выключен"
        self.overlays["them"].show(msg, 2.5, STATUS_COLOR)
        self.update_tray()
        set_settings({"LIVE": st["live"]})
        try:
            save_settings({"LIVE": st["live"]})
        except OSError as e:
            print(f"[!] Не удалось сохранить настройку живого перевода: {e}")

    def live_worker(self):
        """Слушает звук собеседников и отдаёт на перевод каждую законченную фразу.

        Конец фразы ищем VAD'ом Silero (он же внутри faster-whisper): речь была,
        а потом LIVE_END_SILENCE секунд тишины. Звук копируется сюда из
        LoopbackRecorder и не мешает F10 — у него свой буфер.
        """
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        vad = VadOptions(threshold=0.5, min_speech_duration_ms=int(self.LIVE_MIN_SPEECH * 1000),
                         min_silence_duration_ms=int(self.LIVE_END_SILENCE * 1000),
                         speech_pad_ms=150)
        st = self.state
        attached, q = None, None
        pending = np.zeros(0, dtype=np.float32)  # ещё не переведённый звук, 16 кГц
        sr = SAMPLE_RATE
        last_partial = 0.0  # когда показали промежуточный перевод текущей фразы

        def detach():
            if attached is not None and q in attached.taps:
                attached.taps.remove(q)

        while True:
            try:
                loop = st["loop"]
                ready = self.them_ready()
                if not st["live"] or st["paused"] or loop is None or not ready:
                    detach()
                    attached, pending = None, pending[:0]
                    time.sleep(0.2)
                    continue
                if attached is not loop:  # включили или сменили устройство в настройках
                    detach()
                    q, attached, pending = queue.Queue(), loop, pending[:0]
                    loop.taps.append(q)
                time.sleep(self.LIVE_TICK)
                chunks = []
                while True:
                    try:
                        chunks.append(q.get_nowait())
                    except queue.Empty:
                        break
                if chunks:
                    pending = np.concatenate([pending, to_16k(np.concatenate(chunks), loop.rate)])
                if len(pending) < self.LIVE_MIN_SPEECH * sr:
                    continue
                speech = get_speech_timestamps(pending, vad)
                if not speech:
                    pending = pending[-int(0.5 * sr):]  # тишина/шум: держим только хвост
                    continue
                start, end = speech[0]["start"], speech[-1]["end"]
                begin = max(0, start - int(0.2 * sr))
                if len(pending) - end >= self.LIVE_END_SILENCE * sr:
                    cut = end  # фраза закончилась
                elif len(pending) - start >= self.LIVE_MAX_PHRASE * sr:
                    cut = len(pending)  # говорят без пауз — закрываем фразу
                else:
                    # фраза ещё идёт — показываем перевод того, что уже сказано
                    # (пауза считается от КОНЦА прошлого черновика: на длинной фразе
                    # черновик считается ~1 с, и без передышки перевод отстаёт от речи)
                    if (len(pending) - start >= self.LIVE_PARTIAL_START * sr
                            and time.monotonic() - last_partial >= self.LIVE_PARTIAL_EVERY):
                        self.process_live(pending[begin:], final=False)
                        last_partial = time.monotonic()
                    continue
                phrase = pending[begin:cut]
                pending = pending[cut:]
                last_partial = 0.0
                self.process_live(phrase, final=True)
            except Exception:
                traceback.print_exc()
                time.sleep(1)

    def process_live(self, audio, final=True):
        """Перевод фразы собеседника. final=False — фраза ещё идёт: быстрый черновой
        перевод (луч 1), он заменится точным, когда фраза закончится."""
        st = self.state
        audio, peak = self.normalize(audio)
        if peak < 0.005 or len(audio) < self.LIVE_MIN_SPEECH * SAMPLE_RATE:
            return
        t0 = time.perf_counter()
        # язык определяем на первом черновике и на всей фразе в конце (так надёжнее)
        lang = self.live_phrase_lang
        if final or lang is None:
            lang = self.them_lang(audio)
        self.live_phrase_lang = None if final else lang
        if lang == SOURCE_LANG:
            # речь уже на моём языке (русскоязычные собеседники) — переводить нечего
            if final:
                print(f"[живой] {len(audio) / SAMPLE_RATE:.1f} с, язык {lang} — пропускаю")
                self.show_live(None, final=True)  # убрать черновик, если он был
            return
        heard = self.transcribe(audio, lang, "transcribe", beam=5 if final else 1)
        if not st["live"] or st["paused"]:
            return
        if not heard:
            if final:
                self.show_live(None, final=True)
            return
        t1 = time.perf_counter()
        ru = self.translate_chain(heard, lang, SOURCE_LANG)
        t2 = time.perf_counter()
        if final:
            print(f"[живой] {len(audio) / SAMPLE_RATE:.1f} с, распознавание {t1 - t0:.2f} с"
                  f" + перевод {t2 - t1:.2f} с")
            print(f"[живой] {heard}")
            print(f"[живой] {ru}")
        text = f"{ru}\n{heard}" if SHOW_ORIGINAL else ru
        self.show_live(text if final else text + " …", final)

    def show_live(self, text, final):
        """Субтитры: две последние фразы. Черновик текущей фразы заменяется
        следующим черновиком или окончательным переводом; text=None — убрать черновик."""
        if self.state["busy_them"] or time.monotonic() < self.them_hold_until:
            return  # сейчас на экране результат F10 — не перебиваем
        if text is None and all(f for _, _, f in self.live_lines):
            return  # черновика на экране нет — убирать нечего
        now = time.monotonic()
        lines = [(t, x, f) for t, x, f in self.live_lines if f and now - t < DISPLAY_SECONDS]
        if text is not None:
            lines.append((now, text, final))
        self.live_lines = lines[-2:]
        if not self.live_lines:
            if text is None:
                self.ui_q.put(("hide", "them"))
            return
        sep = "\n\n" if SHOW_ORIGINAL else "\n"
        self.show("them", sep.join(x for _, x, _ in self.live_lines), DISPLAY_SECONDS, THEM_COLOR)

    # ---------- смена устройств на ходу (из окна настроек) ----------
    def switch_mic(self, device, problems):
        old = self.state["rec"]
        if old is not None and device == MIC_DEVICE:
            return True
        if old is not None and old.recording:
            problems.append(f"Микрофон не сменён: отпустите {HOTKEY.upper()} и нажмите «Применить».")
            return False
        try:
            new = Recorder(device)
        except Exception as e:
            problems.append(f"Микрофон не открылся ({e}), оставил прежний.")
            return False
        self.state["rec"] = new
        if old is not None:
            old.close()
        return True

    def switch_loopback(self, device, problems):
        old = self.state["loop"]
        if old is not None and device == LOOPBACK_DEVICE:
            return True
        try:
            new = LoopbackRecorder(THEM_SECONDS, device)
        except Exception as e:
            problems.append(f"Устройство вывода не открылось ({e}), оставил прежнее.")
            return False
        self.state["loop"] = new
        if old is not None:
            old.close()
        return True

    # ---------- обновления ----------
    def update_checker(self):
        time.sleep(6)  # пусть сначала погаснет сообщение «Готово»
        try:
            info = check_for_update()
        except Exception as e:
            print(f"[i] Не удалось проверить обновления: {e}")
            return
        if info is None:
            print(f"[i] Обновлений нет: {APP_NAME} {APP_VERSION} — последняя версия.")
            return
        version, url, notes, _setup = info
        self.update_info = info
        print(f"[обновление] Вышла версия {version} (у вас {APP_VERSION}): {url}")
        if notes:
            print("[обновление] Что нового:\n" + notes)
        self.call_in_ui(self.update_tray)
        how = "Обновить" if self.can_self_update() else "Скачать"
        self.show("me", f"Вышла новая версия {APP_NAME} {version}!\n"
                        f"{how} — в меню иконки в трее", 10)

    def can_self_update(self):
        """Обновиться сами можем только установленной версией (Kat.exe) и если в релизе
        есть установщик; из исходников — только открыть страницу релиза."""
        return FROZEN and self.update_info is not None and self.update_info[3] is not None

    def open_update_page(self):
        import webbrowser
        if self.update_info is None:
            return
        if self.can_self_update():
            if not ask_yes_no(f"Обновить {APP_NAME} до версии {self.update_info[0]}?\n\n"
                              "Kat скачает установщик, закроется на минуту и запустится "
                              "уже новым. Настройки и модели сохранятся."):
                return
            threading.Thread(target=self._self_update, daemon=True, name="self-update").start()
        else:
            webbrowser.open(self.update_info[1])

    def _self_update(self):
        """Скачать KatSetup.exe из релиза и запустить его тихо; он закроет Kat,
        обновит файлы и запустит новую версию."""
        import tempfile
        global _download_hook
        version, _url, _notes, setup_url = self.update_info
        dest = os.path.join(tempfile.gettempdir(), f"KatSetup-{version}.exe")
        _download_hook = lambda text: self.show("me", f"Обновление: {text}")  # noqa: E731
        try:
            if os.path.exists(dest):
                os.remove(dest)
            if not download_file(setup_url, dest):
                raise RuntimeError("установщик не найден в релизе")
            print(f"[обновление] запускаю {dest}")
            self.show("me", f"Устанавливаю {APP_NAME} {version}...", 30)
            subprocess.Popen([dest, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LAUNCH=1",
                              f"/DIR={BASE_DIR}"], close_fds=True)
            self.ui_q.put(("quit",))
        except Exception as e:
            traceback.print_exc()
            self.show("me", f"Не удалось обновиться: {e}", 8)
        finally:
            _download_hook = None

    # ---------- трей ----------
    def start_tray(self):
        try:
            import pystray
        except Exception as e:
            print(f"[!] Иконка в трее недоступна ({e}). Выход — {EXIT_HOTKEY}.")
            return
        item = pystray.MenuItem
        menu = pystray.Menu(
            item(lambda _: (f"⬇ {'Обновить до' if self.can_self_update() else 'Скачать обновление'}"
                            f" {self.update_info[0]}") if self.update_info else "",
                 lambda: self.call_in_ui(self.open_update_page),
                 visible=lambda _: self.update_info is not None),
            item("Настройки", lambda: self.call_in_ui(self.open_settings), default=True),
            item(lambda _: f"Живой перевод ({LIVE_HOTKEY.upper()})",
                 lambda: self.call_in_ui(self.toggle_live),
                 checked=lambda _: self.state["live"]),
            item("Пауза", lambda: self.call_in_ui(self.toggle_pause),
                 checked=lambda _: self.state["paused"]),
            item("Перезапустить", lambda: self.call_in_ui(self.restart)),
            item("Открыть папку", lambda: os.startfile(BASE_DIR)),
            pystray.Menu.SEPARATOR,
            item(f"{APP_NAME} {APP_VERSION}", lambda: None, enabled=False),
            item("Выход", lambda: self.ui_q.put(("quit",))),
        )
        self.tray = pystray.Icon("voice_translator", tray_image(False), self.tray_title(), menu)
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()

    def tray_title(self):
        st = self.state
        if st["paused"]:
            title = APP_NAME + " — пауза"
        else:
            title = APP_NAME + (" — живой перевод" if st["live"] else "")
        if self.update_info is not None:
            title += f"\nДоступна версия {self.update_info[0]}"
        return title

    def update_tray(self):
        if self.tray is not None:
            self.tray.icon = tray_image(self.state["paused"])
            self.tray.title = self.tray_title()
            self.tray.update_menu()

    def toggle_pause(self):
        st = self.state
        st["paused"] = not st["paused"]
        rec = st["rec"]
        if st["paused"] and rec is not None and rec.recording:
            rec.cancel()
        self.update_tray()
        print("[пауза] включена" if st["paused"] else "[пауза] выключена")
        self.overlays["me"].show(
            f"Пауза: {HOTKEY.upper()} и {THEM_HOTKEY.upper()} не работают" if st["paused"]
            else "Пауза снята", 2, STATUS_COLOR)

    def open_settings(self):
        if self.settings_window is not None and self.settings_window.win.winfo_exists():
            self.settings_window.focus()
            return
        self.settings_window = SettingsWindow(self)

    def restart(self, admin=False):
        admin = admin or (RUN_AS_ADMIN and not is_admin())
        print("Перезапуск" + (" от имени администратора..." if admin else "..."))
        if relaunch(admin):
            self.quit()  # новая копия ждёт, пока эта закроется
        else:
            print("[!] Перезапуск отменён.")
            self.overlays["me"].show("Перезапуск отменён", 3, STATUS_COLOR)

    def quit(self):
        print("Выход.")
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


def selftest(args):
    """Kat.exe --selftest [файл.wav]: проверка сборки — всё ли внутри грузится и работает.
    Пишет в log.txt и заканчивается строкой «SELFTEST OK». Окон и горячих клавиш не трогает."""
    import wave
    import pystray  # noqa: F401  (трей)
    import pyaudiowpatch  # noqa: F401  (звук собеседников)
    print(f"selftest: {APP_NAME} {APP_VERSION}, frozen={FROZEN}, папка {BASE_DIR}")
    root = tk.Tk()
    root.withdraw()
    root.destroy()
    print("selftest: tkinter ok; микрофонов:", sum(1 for d in sd.query_devices()
                                                  if d["max_input_channels"] > 0))
    ensure_cuda_libs()
    model, desc = load_whisper()
    print(f"selftest: whisper ok ({desc})")
    from faster_whisper.vad import get_speech_timestamps
    get_speech_timestamps(np.zeros(SAMPLE_RATE, dtype=np.float32))
    print("selftest: vad ok")
    glossary = Glossary()
    for d, text in (("en-ru", "Can you heal me? I'm almost dead."),
                    ("ru-en", "Иди на левую сторону, там босс.")):
        print(f"selftest: {d}: {text} -> {TextTranslator(f'quickmt-{d}', glossary)(text)}")
    for path in (a for a in args if a.lower().endswith(".wav")):
        with wave.open(path) as w:
            sr, ch = w.getframerate(), w.getnchannels()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        a = to_16k(a.reshape(-1, ch).mean(axis=1) / 32768.0, sr)
        segs, info = model.transcribe(a, vad_filter=True)
        print(f"selftest: {os.path.basename(path)} [{info.language}]:",
              " ".join(x.text.strip() for x in segs))
    print("SELFTEST OK")


def main():
    args = sys.argv[1:]
    if "--setup" in args:  # вызывает start.bat после установки
        create_shortcuts()
        return
    if "--selftest" in args:
        selftest(args)
        return
    if os.name == "nt":
        if requirements_changed():
            if ask_yes_no("Программа обновилась, и ей нужны новые компоненты.\n\n"
                          "Установить их сейчас? Откроется окно установки, "
                          "после неё программа запустится сама."):
                os.startfile(os.path.join(BASE_DIR, "start.bat"))
            return
        if RUN_AS_ADMIN and not is_admin() and "--elevated" not in args:
            if relaunch(admin=True):
                print("Перезапускаюсь от имени администратора...")
                return
            print("[!] Права администратора не получены — работаю с обычными правами.")
        # только одна копия: вторая (например, повторный щелчок по ярлыку) сообщает и выходит
        global _single_instance
        _single_instance = acquire_single_instance(wait="--restart" in args)
        if not _single_instance:
            print("Уже запущена другая копия — выхожу.")
            message_box(f"{APP_NAME} уже запущен — его иконка в трее, возле часов.", error=False)
            return
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        if not os.path.exists(ICON_FILE):
            # иконка обновилась — перенаправляем на неё уже существующие ярлыки
            def refresh_shortcuts():
                try:
                    create_shortcuts(only_existing=True)
                except Exception as e:
                    print(f"[!] Не удалось обновить ярлыки: {e}")
            threading.Thread(target=refresh_shortcuts, daemon=True, name="shortcuts").start()
    App().run()
    os._exit(0)


if __name__ == "__main__":
    main()
