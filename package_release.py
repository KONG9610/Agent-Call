"""Build public archives from explicit allowlists. Never include local runtime data."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).parent
DIST = ROOT / 'dist' / 'PhoneService'
RELEASE = ROOT / 'release'
RELEASE.mkdir(exist_ok=True)
licenses = DIST / 'THIRD_PARTY_LICENSES'
licenses.mkdir(exist_ok=True)
inventory = []
for dist in importlib.metadata.distributions():
    name = dist.metadata.get('Name', 'unknown')
    inventory.append({'name': name, 'version': dist.version})
    for file in dist.files or []:
        if any(word in str(file).lower() for word in ('license', 'copying', 'notice')) and str(file).endswith(('.txt', '.md', '.rst', 'LICENSE', 'COPYING', 'NOTICE')):
            src = Path(dist.locate_file(file))
            if src.is_file() and src.stat().st_size < 5_000_000:
                dest = licenses / name / Path(str(file)).name
                dest.parent.mkdir(exist_ok=True)
                if dest.exists():
                    dest = dest.with_name(hashlib.sha256(str(file).encode()).hexdigest()[:8] + '-' + dest.name)
                shutil.copyfile(src, dest)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.exists():
    shutil.copyfile(python_license, licenses / 'Python-LICENSE.txt')
(licenses / 'PACKAGES.json').write_text(json.dumps(sorted(inventory, key=lambda x:x['name'].lower()), indent=2), encoding='utf-8')

docs = ['README.md', 'AI_SETUP.md', 'COMPATIBILITY.md', 'RELEASE.md', 'TEST_REPORT.md', 'LICENSE', 'setup-firewall.ps1']
for name in docs:
    if (ROOT / name).exists():
        shutil.copyfile(ROOT / name, DIST / name)
source = docs + ['DesktopWpf.cs', 'Desktop.xaml', 'build-desktop.ps1', 'app_service.py', 'monitor.py', 'paths.py', 'pbx.py', 'sipcore.py',
    'rtp.py', 'g711.py', 'voice.py', 'console.py', 'yealink_web.py', 'test_app.py', 'selftest.py',
    'test_packaged.py', '.gitignore', 'requirements.txt', 'requirements-build.txt', 'requirements-lock.txt',
    'build.ps1', 'package_release.py']
source_zip = RELEASE / 'AgentCall-0.1.1-source.zip'
with zipfile.ZipFile(source_zip, 'w', zipfile.ZIP_DEFLATED) as z:
    for name in source:
        p = ROOT / name
        if p.exists():
            z.write(p, 'agent-call/' + name)
binary_zip = RELEASE / 'AgentCall-0.1.1-windows-x64.zip'
with zipfile.ZipFile(binary_zip, 'w', zipfile.ZIP_DEFLATED) as z:
    for path in DIST.rglob('*'):
        rel = path.relative_to(DIST)
        if path.is_file() and path.name != 'CodexPhone.exe' and rel.parts[0] not in ('data', '__pycache__'):
            z.write(path, 'AgentCall/' + rel.as_posix())
hashes = '\n'.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name for p in [source_zip,binary_zip]) + '\n'
(RELEASE / 'SHA256SUMS.txt').write_text(hashes, encoding='ascii')
print(hashes)
