# Сборка Kat.exe (PyInstaller) — папка dist/Kat. Запускается из build.bat.
# Библиотеки NVIDIA (CUDA) сюда не входят: Kat.exe докачивает их при первом
# запуске, если есть видеокарта NVIDIA (см. ensure_cuda_libs в translator.py).
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

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
    console=False,  # без чёрного окна; всё пишется в log.txt
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Kat", upx=False)
