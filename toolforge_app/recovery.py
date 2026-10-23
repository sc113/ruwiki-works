"""Private startup configuration and narrowly scoped Toolforge process restarts."""
import json
import os
import re
import ssl
import stat
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

NAMESPACE_FILE = Path('/var/run/secrets/kubernetes.io/serviceaccount/namespace')
TARGETS = {'web': '', 'executor': 'ruwiki-executor', 'monitor': 'ruwiki-monitor'}


class RecoveryError(ValueError):
    pass


def tool_directory():
    """Discover the account even when the envvars admission service was unavailable."""
    if not NAMESPACE_FILE.is_file():
        return None
    namespace = NAMESPACE_FILE.read_text().strip()
    if not re.fullmatch(r'tool-[a-z][a-z0-9-]{0,55}', namespace):
        raise RecoveryError('Invalid Toolforge namespace')
    return Path('/data/project') / namespace.removeprefix('tool-')


def private_configuration():
    directory = tool_directory()
    if directory is None:
        return None
    path = directory / '.config/ruwiki-works/recovery.json'
    if not path.exists():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RecoveryError('Recovery configuration permissions are invalid')
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('environment'), dict):
            raise ValueError()
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in data['environment'].items()):
            raise ValueError()
        return data
    except (ValueError, TypeError, UnicodeError):
        raise RecoveryError('Recovery configuration is unreadable') from None


def restart_available():
    try:
        data = private_configuration()
        return bool(data and data.get('control'))
    except (OSError, RecoveryError):
        return False


def restart_process(component):
    """Roll out exactly one known Deployment. Never retry an ambiguous response."""
    if component not in TARGETS:
        raise RecoveryError('Unknown process')
    try:
        directory = tool_directory()
        config = private_configuration()
        if not directory or not config or not config.get('control'):
            raise RecoveryError('Process control is not configured')
        control = config['control']
        server = control['server']
        if server != 'https://k8s.tools.eqiad1.wikimedia.cloud:6443':
            raise RecoveryError('Unexpected cluster')
        # Toolforge's internal CA omits AKI. Explicit TLS client policy keeps
        # CA/hostname/expiry verification without Python 3.13's extra RFC checks.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=control['certificate_authority'])
        context.load_cert_chain(directory / '.toolskube/client.crt', directory / '.toolskube/client.key')
        target = TARGETS[component] or directory.name
        namespace = 'tool-' + directory.name
        url = f'{server}/apis/apps/v1/namespaces/{namespace}/deployments/{target}'
        def read_json(address):
            with urlopen(address, context=context, timeout=5) as response:
                return json.load(response)
        deployment = read_json(url)
        selector = deployment['spec']['selector']['matchLabels']
        pod_url = f'{server}/api/v1/namespaces/{namespace}/pods?' + urlencode({
            'labelSelector': ','.join(f'{k}={v}' for k, v in sorted(selector.items()))})
        old_pods = {p['metadata']['uid'] for p in read_json(pod_url)['items']}
        request_id = uuid.uuid4().hex
        body = {'spec': {'template': {'metadata': {'annotations': {
            'ruwiki-works/restart-request': request_id}}}}}
        request = Request(url, data=json.dumps(body).encode(), method='PATCH',
                          headers={'Content-Type': 'application/strategic-merge-patch+json'})
        with urlopen(request, context=context, timeout=8) as response:
            if response.status != 200:
                raise RecoveryError('Restart was not acknowledged')
        if component == 'web':
            return request_id  # Return the response before this very web pod exits.
        # Wait for old processes to exit before allowing a new execution claim.
        deadline = time.monotonic() + 20
        while old_pods & {p['metadata']['uid'] for p in read_json(pod_url)['items']}:
            if time.monotonic() >= deadline:
                raise RecoveryError('Old processes are still stopping')
            time.sleep(1)
        return request_id
    except Exception:
        # TLS keys, API responses and configuration values must not enter web logs.
        raise RecoveryError('Не удалось подтвердить перезапуск. Обновите состояние; команда автоматически не повторяется.') from None
