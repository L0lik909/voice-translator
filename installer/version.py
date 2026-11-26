"""Печатает APP_VERSION из translator.py (для build.bat)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "translator.py"), encoding="utf-8").read()
print(re.search(r'^APP_VERSION = "([^"]+)"', src, re.M).group(1))
