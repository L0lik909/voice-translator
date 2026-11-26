"""Рисует Kat.ico для Kat.exe и установщика (тем же кодом, что иконку в трее).
Запуск: python installer/make_icon.py installer/Kat.ico  (вызывает build.bat)."""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "translator.py"), encoding="utf-8").read()
# берём из translator.py только функцию draw_icon, чтобы не запускать всю программу
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "draw_icon")
ns = {}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "translator.py", "exec"), ns)
sizes = [256, 128, 64, 48, 40, 32, 24, 20, 16]
images = [ns["draw_icon"](s) for s in sizes]
images[0].save(sys.argv[1], format="ICO", sizes=[(s, s) for s in sizes], append_images=images[1:])
print("иконка:", sys.argv[1])
