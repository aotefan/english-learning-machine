"""Compile the real board source with valid/invalid settings; never flash.

Requires a completed english-edge-s3 reference build and its compile_commands.
Overrides live only in a temporary include, not in generated SDK files.
"""
import hashlib
import json
import shlex
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / 'upstream/firmware'


def main():
    commands = json.loads((FIRMWARE / 'build/compile_commands.json').read_text())
    entries = [c for c in commands if c['file'].endswith(
        '/boards/english-edge-s3/english_edge_board.cc')]
    if len(entries) != 1:
        raise SystemExit('First build english-edge-s3-reference-only')
    entry = entries[0]
    original = entry.get('arguments') or shlex.split(entry['command'])
    command = []
    args = iter(original)
    for arg in args:
        if arg in ('-o', '-MF', '-MT', '-MQ'):
            next(args)
        elif arg not in ('-c', '-MD', '-MMD', '-MP'):
            command.append(arg)
    cases = [
        ('reference', {}, True),
        ('alternate_slots_and_button', {'MIC_SLOT': 2, 'SPK_SLOT': 2,
                                        'BUTTON_ACTIVE_HIGH': 1}, True),
        ('unset_pin', {'MIC_DIN': -1}, False),
        ('duplicate_clock', {'SPK_WS': 4}, False),
        ('nonexistent_gpio', {'MIC_DIN': 22}, False),
        ('unset_button_level', {'BUTTON_ACTIVE_HIGH': -1}, False),
        ('unset_mic_slot', {'MIC_SLOT': -1}, False),
        ('invalid_speaker_slot', {'SPK_SLOT': 0}, False),
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix='english-edge-config-') as tmp:
        override = Path(tmp) / 'override.h'
        for name, values, should_compile in cases:
            override.write_text('#include <sdkconfig.h>\n' + ''.join(
                f'#undef CONFIG_ENGLISH_EDGE_{key}\n'
                f'#define CONFIG_ENGLISH_EDGE_{key} {value}\n'
                for key, value in values.items()))
            result = subprocess.run(command + ['-fsyntax-only', '-include', str(override)],
                                    cwd=entry['directory'], capture_output=True,
                                    text=True, timeout=60)
            passed = (result.returncode == 0 if should_compile else
                      result.returncode != 0 and 'static assertion failed' in result.stderr)
            results.append({'case': name, 'overrides': values,
                            'expected_compile': should_compile,
                            'returncode': result.returncode, 'passed': passed,
                            'diagnostic': result.stderr})
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    print(json.dumps({'hardware': False, 'compiler': command[0],
                      'sdkconfig_sha256': digest(FIRMWARE / 'sdkconfig'),
                      'source_sha256': digest(Path(entry['file'])),
                      'config_header_sha256': digest(Path(entry['file']).with_name('config.h')),
                      'cases': results}, indent=2))
    if not all(r['passed'] for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
