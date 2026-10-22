"""Run each test module in a fresh process to avoid legacy script-name collisions."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test_*.py')
    args = parser.parse_args()
    modules = sorted((ROOT / 'tests').glob(args.pattern))
    if not modules:
        raise SystemExit('No test modules matched')
    failed = []
    for path in modules:
        print('\nTEST MODULE: ' + path.name, flush=True)
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover',
                                 '-s', str(ROOT / 'tests'), '-p', path.name, '-v'], cwd=ROOT)
        if result.returncode:
            failed.append(path.name)
    print('\nModules: %d; failed: %d' % (len(modules), len(failed)), flush=True)
    if failed:
        print('\n'.join(failed))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
