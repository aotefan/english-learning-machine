"""Create fresh local credentials for a STANDBY server; no network or paid calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def initialize(folder, device_id, url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('ws', 'wss') or not parsed.netloc or parsed.path != '/xiaozhi/v1/' or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Use a WebSocket endpoint ending in /xiaozhi/v1/')
    if parsed.scheme == 'ws' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Non-loopback endpoints require wss://')
    if not device_id or len(device_id) > 128 or any(ord(c) < 33 for c in device_id):
        raise ValueError('Invalid device identifier')
    folder = Path(folder)
    if folder.exists():
        raise FileExistsError('Refusing to replace an existing private directory')
    token, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    content = {
        'cloud.env': (ROOT / 'cloud.env.example').read_text(),
        'devices.json': json.dumps({device_id: hashlib.sha256(token.encode()).hexdigest()}, indent=2),
        'cloud-client.json': json.dumps({'device_id': device_id, 'token': token, 'url': url}, indent=2),
        'admin.json': json.dumps({'username': 'admin', 'password_sha256': hashlib.sha256(password.encode()).hexdigest()}, indent=2),
        'admin-login.txt': 'Username: admin\nPassword: ' + password + '\n',
    }
    folder.mkdir(mode=0o700, parents=True)
    for name, text in content.items():
        fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text.rstrip() + '\n')
    return folder


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=ROOT / '.secrets')
    p.add_argument('--device-id', default='local-test-device')
    p.add_argument('--url', default='ws://127.0.0.1:18000/xiaozhi/v1/')
    args = p.parse_args()
    initialize(args.output, args.device_id, args.url)
    print('Created private configuration. Read admin-login.txt locally. Paid calls remain disabled.')
