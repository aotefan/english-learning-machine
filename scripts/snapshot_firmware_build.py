"""Record an actual reference build and its dependency license evidence. Never flash."""
import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / 'upstream/firmware'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    if not args.name.replace('-', '').replace('_', '').isalnum():
        parser.error('name must contain only letters, digits, hyphens or underscores')
    evidence = ROOT / 'research/reference-builds' / args.name
    artifacts = ROOT / 'artifacts/firmware' / args.name
    evidence.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    for filename in ['dependencies.lock', 'sdkconfig']:
        shutil.copyfile(FIRMWARE / filename, evidence / filename)
    description = json.loads((FIRMWARE / 'build/project_description.json').read_text())
    sdk_version = re.search(r'^CONFIG_IDF_INIT_VERSION="([^"]+)"',
                            (FIRMWARE / 'sdkconfig').read_text(), re.MULTILINE)
    source_diff = subprocess.check_output(['git', 'diff', 'HEAD'], cwd=FIRMWARE)
    (evidence / 'source.patch').write_bytes(source_diff)
    report = {
        'recorded_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'reference_only': True, 'flashed': False,
        'target': description['target'], 'idf_version': sdk_version.group(1) if sdk_version else None,
        'firmware_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=FIRMWARE, text=True).strip(),
        'source_diff_sha256': hashlib.sha256(source_diff).hexdigest(),
        'binaries': {}, 'components': [],
    }
    for filename in ['xiaozhi.bin', 'merged-binary.bin']:
        path = FIRMWARE / 'build' / filename
        report['binaries'][filename] = {'bytes': path.stat().st_size, 'sha256': digest(path)}
        shutil.copyfile(path, artifacts / filename)
    lock = yaml.safe_load((FIRMWARE / 'dependencies.lock').read_text())
    for name, data in lock['dependencies'].items():
        component = FIRMWARE / 'managed_components' / name.replace('/', '__')
        manifest = component / 'idf_component.yml'
        metadata = yaml.safe_load(manifest.read_text()) if manifest.exists() else {}
        licenses = []
        if component.is_dir():
            for path in sorted(component.rglob('*')):
                if path.is_file() and path.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE')):
                    licenses.append({'path': str(path.relative_to(component)), 'sha256': digest(path)})
        report['components'].append({'name': name, 'version': data.get('version'),
                                     'manifest_license': metadata.get('license'),
                                     'license_files': licenses})
    (evidence / 'build.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
    print(evidence)


if __name__ == '__main__':
    main()
