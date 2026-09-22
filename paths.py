"""Portable data stays next to the application, never in an implicit C: cache."""
import os
import sys
from pathlib import Path

ROOT = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
DATA = Path(os.environ.get('CODEX_PHONE_DATA', str(ROOT / 'data'))).resolve()

def prepare():
    for name in ('cache', 'requests', 'logs', 'tmp'):
        (DATA / name).mkdir(parents=True, exist_ok=True)
    os.environ['TEMP'] = str(DATA / 'tmp')
    os.environ['TMP'] = str(DATA / 'tmp')
