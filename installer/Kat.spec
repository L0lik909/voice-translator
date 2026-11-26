# Сборка Kat.exe (PyInstaller) — папка dist/Kat. Запускается из build.bat.
# Библиотеки NVIDIA (CUDA) сюда не входят: Kat.exe докачивает их при первом
# запуске, если есть видеокарта NVIDIA (см. ensure_cuda_libs в translator.py).
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

import re
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                 StringTable, VarFileInfo, VarStruct,
                                                 VSVersionInfo)

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# Описание внутри Kat.exe: его показывают окно «Разрешить этому приложению вносить
# изменения» (запуск от администратора), диспетчер задач и свойства файла.
_src = open(os.path.join(ROOT, "translator.py"), encoding="utf-8").read()
VERSION = re.search(r'^APP_VERSION = "([^"]+)"', _src, re.M).group(1)
AUTHOR = re.search(r'^APP_AUTHOR = "([^"]+)"', _src, re.M).group(1)
_v = tuple(int(x) for x in (VERSION.split(".") + ["0"] * 4)[:4])
VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_v, prodvers=_v),
    kids=[
        StringFileInfo([StringTable("041904B0", [
            StringStruct("CompanyName", AUTHOR),
            StringStruct("FileDescription", "Kat"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "Kat"),
            StringStruct("LegalCopyright", f"By {AUTHOR}"),
            StringStruct("OriginalFilename", "Kat.exe"),
            StringStruct("ProductName", "Kat — голосовой переводчик"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0419, 1200])]),
    ],
)

a = Analysis(
    [os.path.join(ROOT, "translator.py")],
    pathex=[ROOT],
    binaries=collect_dynamic_libs("ctranslate2"),
    datas=collect_data_files("faster_whisper")  # модель VAD Silero (assets/*.onnx)
          + collect_data_files("sv_ttk"),  # тёмная тема окон (tcl + картинки)
    hiddenimports=["pystray._win32", "PIL._tkinter_finder"],
    excludes=["matplotlib", "IPython", "pytest", "tkinter.test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Kat",
    icon=os.path.join(SPECPATH, "Kat.ico"),
    version=VERSION_INFO,
    console=False,  # без чёрного окна; всё пишется в log.txt
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Kat", upx=False)
