"""Build an installable archive containing runtime files only."""

import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
package = json.loads((root/'manifest.json').read_text(encoding='utf-8'))['package']
target = root / (package+'.ankiaddon')
with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
    for name in ('__init__.py', 'addon.py', 'engine.py', 'planner.py', 'config.json', 'manifest.json'):
        archive.write(root/name, name)
print(target)
