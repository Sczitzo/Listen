"""Open Listen's window, or show the existing instance, without a console."""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
os.environ['DICTATE_SHOW'] = '1'
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
runpy.run_path(str(ROOT / 'dictate.py'), run_name='__main__')
