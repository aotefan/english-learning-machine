"""Create an allowlisted deployment archive: no keys, recordings or model files."""
import argparse
import hashlib
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = Path('upstream/backend/main/xiaozhi-server')


def package(output):
    files = [Path('requirements-cloud.lock'), Path('deploy/Dockerfile'),
             Path('deploy/compose.yaml'), Path('deploy/diagnostic-capture.compose.yaml'),
             Path('.dockerignore')]
    files += [Path('scripts')/name for name in (
        'cloud_setup.py', 'cloud_budget.py', 'cloud_learning.py', 'cloud_service.py', 'run_cloud.py',
        'cloud_smoke.py', 'cloud_report.py','debug_page.py','device_control.py')]
    files += [Path('debug')/name for name in
              ('index.html','app.js','admin.css','login.html','costs.html','costs.js',
               'learning.html','learning.js','monitor.html','monitor.js')]
    # Existing code only; no data, YAML secrets, sample audio or generic local runner.
    for folder in ('core', 'config', 'plugins_func'):
        files.extend((SERVER/folder/p.relative_to(ROOT/SERVER/folder))
                     for p in (ROOT/SERVER/folder).rglob('*.py') if '__pycache__' not in p.parts)
    files += [SERVER/'config.yaml', Path('upstream/backend/LICENSE')]
    files = sorted(set(files))
    wheels=ROOT/'artifacts/wheels-linux'
    if not (wheels/'linux.lock').exists():
        raise ValueError('Run scripts/prepare_cloud_wheels.py before deployment')
    # Refuse to package accidentally embedded values of this project's real keys.
    from dotenv import dotenv_values
    private=ROOT/'.secrets/cloud.env'
    values=dotenv_values(private,interpolate=False) if private.exists() else {}
    secrets=[values[k].encode() for k in ('DOUBAO_SPEECH_API_KEY','ARK_API_KEY') if values.get(k)]
    for rel in files:
        if any(secret in (ROOT/rel).read_bytes() for secret in secrets):
            raise ValueError('Secret scan failed; archive not created')
    if output.exists():
        raise FileExistsError('Use a new release archive path')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'w:gz') as archive:
        for rel in files:
            path = ROOT/rel
            if path.is_symlink() or not path.is_file():
                raise ValueError('Invalid package input: '+str(rel))
            archive.add(path, arcname=str(rel), recursive=False)
        for path in sorted(wheels.iterdir()):
            if path.suffix not in ('.whl','.lock'):continue
            if path.is_symlink() or not path.is_file():raise ValueError('Invalid wheel input')
            archive.add(path,arcname='wheels/'+path.name,recursive=False)
    print(json.dumps({'files': len(files), 'bytes': output.stat().st_size,
                      'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('output', type=Path)
    package(p.parse_args().output)
