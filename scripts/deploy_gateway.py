"""Deploy only the voice TLS proxy; never changes DNS, firewall, or API live mode.
Run after confirming ICP/access filing, voice DNS, and approved ports 80/443.
"""
import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = '/opt/english-edge/gateway'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('deploy', 'status', 'stop', 'rollback'))
    parser.add_argument('--host', required=True)
    args = parser.parse_args()
    opts = ['-i', str(ROOT / '.secrets/ecs_ed25519'),
            '-o', 'UserKnownHostsFile=' + str(ROOT / '.secrets/ecs_known_hosts'),
            '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15']
    dest = 'root@' + args.host

    def remote(script):
        subprocess.run(['ssh', *opts, dest, 'sh -s'], input='set -eu\numask 077\n' + script,
                       text=True, check=True)

    compose = f'docker compose -f {BASE}/gateway-compose.yaml'
    if args.action == 'deploy':
        remote(f'docker network inspect english-edge_default >/dev/null\n'
               f'install -d -m 700 {BASE} {BASE}/data {BASE}/config\n')
        for name in ('Caddyfile', 'gateway-compose.yaml'):
            subprocess.run(['scp', '-q', *opts, str(ROOT / 'deploy' / name),
                            dest + ':' + BASE + '/' + name + '.new'], check=True)
        remote(f'''
image=public.ecr.aws/docker/library/caddy:2.11.7-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f
docker run --rm --entrypoint caddy -v {BASE}/Caddyfile.new:/etc/caddy/Caddyfile:ro "$image" validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose -f {BASE}/gateway-compose.yaml.new config --quiet
for name in Caddyfile gateway-compose.yaml; do
  if test -f {BASE}/$name; then cp -p {BASE}/$name {BASE}/$name.previous; fi
  mv {BASE}/$name.new {BASE}/$name
done
if ! {compose} up -d --force-recreate --wait --wait-timeout 90; then
  if test -f {BASE}/Caddyfile.previous && test -f {BASE}/gateway-compose.yaml.previous; then
    cp -p {BASE}/Caddyfile.previous {BASE}/Caddyfile
    cp -p {BASE}/gateway-compose.yaml.previous {BASE}/gateway-compose.yaml
    {compose} up -d --force-recreate --wait
  else
    {compose} stop
  fi
  exit 1
fi
{compose} ps
''')
    elif args.action == 'rollback':
        remote(f'''test -f {BASE}/Caddyfile.previous
test -f {BASE}/gateway-compose.yaml.previous
cp -p {BASE}/Caddyfile.previous {BASE}/Caddyfile
cp -p {BASE}/gateway-compose.yaml.previous {BASE}/gateway-compose.yaml
{compose} up -d --force-recreate --wait
''')
    else:
        remote(compose + (' ps\n' if args.action == 'status' else ' stop\n'))


if __name__ == '__main__':
    main()
