"""Run once as the tool account on the Toolforge bastion; never prints secrets."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    home = Path.home()
    result = subprocess.run(['toolforge', 'envvars', 'list', '--json'],
                            capture_output=True, text=True, check=True)
    rows = json.loads(result.stdout)
    # Envvars CLI returns a list of name/value entries.
    if isinstance(rows, dict):
        rows = rows['envvars']
    environment = {row['name']: row['value'] for row in rows if row['name'].startswith('TOOLFORGE_')}
    for key in ('DATABASE_URL', 'SECRET_KEY', 'PUBLIC_URL', 'ADMIN_USERNAME', 'OAUTH_KEY', 'OAUTH_SECRET'):
        if not environment.get('TOOLFORGE_' + key):
            raise ValueError('Required server configuration is missing')
    if not environment['TOOLFORGE_PUBLIC_URL'].startswith('https://') or environment['TOOLFORGE_DATABASE_URL'].startswith('sqlite'):
        raise ValueError('Production configuration is required')
    result = subprocess.run(['kubectl', 'config', 'view', '--raw', '-o', 'json'],
                            capture_output=True, text=True, check=True)
    kube = json.loads(result.stdout)
    context = next(c['context'] for c in kube['contexts'] if c['name'] == kube['current-context'])
    if context['namespace'] != 'tool-' + home.name:
        raise ValueError('Tool account does not match the Kubernetes namespace')
    cluster = next(c['cluster'] for c in kube['clusters'] if c['name'] == context['cluster'])
    if cluster['server'] != 'https://k8s.tools.eqiad1.wikimedia.cloud:6443':
        raise ValueError('Unexpected cluster')
    data = {'version': 1, 'environment': environment, 'control': {
        'server': cluster['server'],
        'certificate_authority': base64.b64decode(cluster['certificate-authority-data']).decode()}}
    directory = home / '.config/ruwiki-works'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / 'recovery.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print('Private startup configuration and process control configured. No passwords or access permissions were changed.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Recovery setup failed. No secret values were printed.') from None
