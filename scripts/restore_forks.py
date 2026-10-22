"""Restore public upstream revisions and apply reviewed source overlays; no private history."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def restore():
    manifest = json.loads((ROOT / 'integrations/manifest.json').read_text())
    for name, info in manifest.items():
        repo = ROOT / 'upstream' / name
        overlay = ROOT / 'integrations' / name / 'files'
        for rel, digest in info['files'].items():
            path = overlay / rel
            if not path.resolve().is_relative_to(overlay.resolve()) or path.is_symlink():
                raise ValueError('Unsafe overlay path')
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Overlay checksum mismatch: ' + name + '/' + rel)
        if not repo.exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'clone', '--filter=blob:none', '--no-checkout',
                            info['url'], str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', info['base']], check=True)
        marker = repo / '.english-overlay.json'
        if marker.exists():
            if (json.loads(marker.read_text()) == info and git(repo, 'rev-parse', 'HEAD') == info['base']
                    and all((repo / rel).is_file() and hashlib.sha256((repo / rel).read_bytes()).hexdigest() == digest
                            for rel, digest in info['files'].items())):
                print(name + ': overlay already installed (other local edits are left intact)')
                continue
            raise SystemExit(str(repo) + ': overlay or local source changed; inspect manually')
        if git(repo, 'status', '--porcelain'):
            raise SystemExit(str(repo) + ': refusing to overwrite local changes')
        subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', info['base']], check=True)
        for rel in info['files']:
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(overlay / rel, target)
        marker.write_text(json.dumps(info, indent=2) + '\n')
        print(name + ': restored public base ' + info['base'] + ' with reviewed overlay')


if __name__ == '__main__':
    restore()
