"""Use pip's official PyPI downloads for the ECS Linux/CPython 3.11 target."""
import hashlib
from pathlib import Path
import re
import subprocess
import sys
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]
def main():
    folder=ROOT/'artifacts/wheels-linux';folder.mkdir(parents=True,exist_ok=True)
    # Full lock already includes transitive dependencies. --no-deps also avoids
    # pip evaluating conditional dependencies against the developer Mac's Python.
    subprocess.run([sys.executable,'-m','pip','download','--no-deps','--only-binary=:all:',
        '--platform','manylinux_2_28_x86_64','--platform','manylinux2014_x86_64',
        '--python-version','311','--implementation','cp','--abi','cp311','--abi','abi3','--abi','none',
        '-r',str(ROOT/'requirements-cloud.lock'),'--dest',str(folder)],check=True)
    expected={name.lower().replace('_','-'):version for name,version in re.findall(
        r'^([a-zA-Z0-9_-]+)(?:\[[^\]]+\])?==([^\s;]+)',(ROOT/'requirements-cloud.lock').read_text(),re.M)}
    entries={}
    for path in folder.glob('*.whl'):
        with ZipFile(path) as wheel:
            metadata=wheel.read(next(n for n in wheel.namelist() if n.endswith('.dist-info/METADATA'))).decode()
        name=re.search(r'^Name: (.+)$',metadata,re.M)[1].strip().lower().replace('_','-')
        version=re.search(r'^Version: (.+)$',metadata,re.M)[1].strip()
        if expected.get(name)!=version:raise ValueError('Wheel does not match project lock: '+name)
        if name in entries:raise ValueError('Ambiguous wheels; use a fresh target directory')
        entries[name]=f'{name}=={version} --hash=sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}'
    if set(entries)!=set(expected):raise ValueError('Wheel set differs from the project lock')
    (folder/'linux.lock').write_text('\n'.join(entries[name] for name in sorted(entries))+'\n')
    print(f'Validated {len(entries)} Linux wheels; runtime installs offline with SHA-256 checks.')

if __name__=='__main__':main()
