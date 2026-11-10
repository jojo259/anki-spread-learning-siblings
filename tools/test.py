"""Run pure planning and real Anki tests, including both add-on load orders."""

import argparse
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--anki-path', required=True)
parser.add_argument('--true-random-path', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, str(root/'tests/test_planner.py')], check=True)
for order in ('native', 'tr-first', 'sls-first'):
    subprocess.run([sys.executable, str(root/'tests/test_integration.py'),
                    '--anki-path', args.anki_path,
                    '--true-random-path', args.true_random_path,
                    '--order', order], check=True)
