"""Deploy/update the existing ECS, preserving the explicitly selected runtime mode.
New environments default to STANDBY. No DNS changes, purchases or data deletion.
"""
import argparse
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path
from package_cloud import ROOT, package

BASE = '/opt/english-edge'
COMMAND = ['python', 'scripts/run_cloud.py', '--env-file', '/run/secrets/cloud.env',
           '--devices-file', '/run/secrets/devices.json', '--admin-file',
           '/run/secrets/admin.json', '--host', '0.0.0.0']


def mode_override(mode):
    if mode not in ('live', 'standby'):
        raise ValueError('Invalid runtime mode')
    command = COMMAND + (['--live'] if mode == 'live' else [])
    return '# english-edge-mode: ' + mode + '\n' + json.dumps(
        {'services': {'voice': {'command': command}}}, indent=2) + '\n'


def runtime_setup(base=BASE):
    """Single persisted Compose override, shared by new and legacy releases.

    The file itself is the source of truth, avoiding disagreement between a
    separate mode marker and the command that the container actually receives.
    A host lock serializes mode changes with release activation/rollback.
    """
    return f'''runtime={shlex.quote(base + '/runtime')}
install -d -m 700 "$runtime"
exec 9>"$runtime/operation.lock"
flock -x 9
write_mode() {{
    case "$1" in
    live) cat > "$2" <<'LIVE_MODE'
{mode_override('live')}LIVE_MODE
        ;;
    standby) cat > "$2" <<'STANDBY_MODE'
{mode_override('standby')}STANDBY_MODE
        ;;
    *) echo 'Invalid runtime mode' >&2; return 1;;
    esac
    chmod 600 "$2"
}}
active_override="$runtime/compose-mode.yaml"
if ! test -f "$active_override"; then
    write_mode standby "$active_override.new"
    mv "$active_override.new" "$active_override"
fi
selected_mode=$(sed -n '1s/^# english-edge-mode: //p' "$active_override")
case "$selected_mode" in live|standby) ;; *) echo 'Invalid saved runtime mode' >&2; exit 1;; esac
compose() {{
    compose_release=$1
    shift
    docker compose -f "$compose_release/deploy/compose.yaml" -f "$active_override" --env-file "$compose_release/deploy/.env" "$@"
}}
'''


def set_mode_script(mode, base=BASE):
    # Validate before embedding this value in a remote shell script.
    mode_override(mode)
    return f'''set -eu
umask 077
{runtime_setup(base)}
target=$(readlink {shlex.quote(base + '/current')})
case "$target" in {shlex.quote(base)}/releases/*) ;; *) echo 'No valid current release' >&2; exit 1;; esac
saved_override="$active_override"
candidate="$runtime/compose-mode.candidate.yaml"
write_mode {mode} "$candidate"
active_override="$candidate"
if ! compose "$target" config --quiet; then rm -f "$candidate"; exit 1; fi
if ! compose "$target" up -d --wait --wait-timeout 90; then
    active_override="$saved_override"
    compose "$target" up -d --wait --wait-timeout 90 || true
    rm -f "$candidate"
    echo 'Mode change failed; previous runtime mode retained' >&2
    exit 1
fi
mv "$candidate" "$saved_override"
active_override="$saved_override"
printf 'Selected runtime mode: %s\\n' {mode}
compose "$target" ps
'''


def rollback_script(base=BASE):
    return f'''set -eu
umask 077
{runtime_setup(base)}
base={shlex.quote(base)}
test -L "$base/previous"
target=$(readlink "$base/previous")
case "$target" in "$base"/releases/*) ;; *) exit 1;; esac
old=$(readlink "$base/current")
compose "$target" config --quiet
if ! compose "$target" up -d --wait --wait-timeout 90; then
    compose "$old" up -d --wait --wait-timeout 90 || true
    exit 1
fi
ln -sfn "$target" "$base/current"
ln -sfn "$old" "$base/previous"
printf 'Selected runtime mode: %s\\n' "$selected_mode"
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('deploy','rollback','status','backup','mode'))
    parser.add_argument('--mode', choices=('live','standby'))
    parser.add_argument('--release')
    parser.add_argument('--host',required=True)
    parser.add_argument('--sync-secrets',action='store_true')
    a=parser.parse_args()
    if (a.action == 'mode') != (a.mode is not None):
        parser.error('--mode is required only with the mode action')
    if a.release and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,60}',a.release):
        raise ValueError('Invalid release name')
    if not re.fullmatch(r'[a-zA-Z0-9.-]+',a.host):raise ValueError('Invalid host')
    opts=['-i',str(ROOT/'.secrets/ecs_ed25519'),'-o','UserKnownHostsFile='+str(ROOT/'.secrets/ecs_known_hosts'),
          '-o','StrictHostKeyChecking=yes','-o','BatchMode=yes','-o','ConnectTimeout=10',
          '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=4']
    dest='root@'+a.host
    def remote(script):
        subprocess.run(['ssh',*opts,dest,'sh -s'],input=script,text=True,check=True)
    def copy(source,target):
        subprocess.run(['scp','-q',*opts,str(source),dest+':'+target],check=True)
    if a.action=='deploy':
        if not a.release:raise ValueError('--release required')
        bundle=ROOT/'artifacts/releases'/f'{a.release}.tar.gz'
        package(bundle)
        checksum=hashlib.sha256(bundle.read_bytes()).hexdigest()
        base=BASE
        release=base+'/releases/'+a.release
        remote(f'''set -eu
umask 077
install -d -m 700 {base} {base}/releases {base}/secrets {base}/backups
install -d -m 700 -o 10001 -g 10001 {base}/state
test ! -e {release}
mkdir -m 700 {release}
''')
        copy(bundle,release+'/source.tar.gz')
        if a.sync_secrets:
            for name in ('cloud.env','devices.json','cloud-client.json','admin.json'):
                # Secret transfer is encrypted and never part of the build context.
                copy(ROOT/'.secrets'/name,base+'/secrets/'+name+'.new')
            remote(f'''set -eu
chmod 600 {base}/secrets/*.new
chown -R 10001:10001 {base}/secrets
for name in cloud.env devices.json cloud-client.json admin.json; do
    if test -f {base}/secrets/$name; then cp -p {base}/secrets/$name {base}/backups/$name.pre-{a.release}; fi
    mv {base}/secrets/$name.new {base}/secrets/$name
done
''')
        remote(f'''set -eu
umask 077
cd {release}
printf '%s\\n' '{checksum}  source.tar.gz' | sha256sum -c -
tar -xzf source.tar.gz
docker build -f deploy/Dockerfile -t english-edge:{a.release} . > build.log 2>&1 || {{ tail -n 35 build.log; exit 1; }}
printf '%s\\n' 'VOICE_IMAGE=english-edge:{a.release}' > deploy/.env
# Preflight imports using the exact Linux image before replacing an earlier release.
docker run --rm --entrypoint python english-edge:{a.release} -c 'import sys; sys.path.insert(0,"/app/upstream/backend/main/xiaozhi-server"); import httpx, websockets, opuslib_next, numpy, volcenginesdkarkruntime, core.utils.cache.manager'
{runtime_setup()}
compose {release} config --quiet
previous=$(readlink {base}/current || true)
if ! compose {release} up -d --wait --wait-timeout 90; then
    if test -n "$previous"; then
        compose "$previous" up -d --wait --wait-timeout 90
    fi
    exit 1
fi
if test -n "$previous"; then ln -sfn "$previous" {base}/previous; fi
ln -sfn {release} {base}/current
compose {release} ps
printf 'Selected runtime mode: %s\\n' "$selected_mode"
''')
    elif a.action=='mode':
        remote(set_mode_script(a.mode))
    elif a.action=='status':
        remote('''set -eu
cd /opt/english-edge/current
docker compose -f deploy/compose.yaml --env-file deploy/.env ps
curl -fsS http://127.0.0.1:18000/healthz
if test -f /opt/english-edge/runtime/compose-mode.yaml; then
    sed -n '1p' /opt/english-edge/runtime/compose-mode.yaml
else
    echo 'Runtime preference not yet saved (new deployments default to standby)'
fi
''')
    elif a.action=='rollback':
        remote(rollback_script())
    else:
        remote('''set -eu
umask 077
python3 - <<'PY'
import datetime,sqlite3,tarfile
from pathlib import Path
base=Path('/opt/english-edge')
dest=base/'backups'/datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
dest.mkdir(mode=0o700)
source=sqlite3.connect(f'file:{base}/state/usage.sqlite?mode=ro',uri=True)
target=sqlite3.connect(dest/'usage.sqlite');source.backup(target);target.close();source.close()
with tarfile.open(dest/'private-config.tar.gz','w:gz') as archive:
    archive.add(base/'secrets',arcname='secrets')
    # Includes TLS private keys; keep this archive private like API credentials.
    if (base/'gateway').is_dir():
        archive.add(base/'gateway',arcname='gateway')
    if (base/'runtime'/'compose-mode.yaml').is_file():
        archive.add(base/'runtime'/'compose-mode.yaml',arcname='runtime/compose-mode.yaml')
    # Preserve remembered browser sessions across a restore; this key is private.
    if (base/'state'/'admin-session.key').is_file():
        archive.add(base/'state'/'admin-session.key',arcname='state/admin-session.key')
(dest/'release.txt').write_text(str((base/'current').resolve()))
print('Protected backup created:',dest.name)
PY
''')


if __name__=='__main__':main()
