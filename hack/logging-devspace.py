#!/usr/bin/env python3
"""Prepare/restore opt-in MCP logging using the COO development bridge."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / '.devspace' / 'logging'
STATE = STATE_DIR / 'state.json'
NS = 'openshift-cluster-observability-operator'
MCP = 'monitoring-console-plugin'
LEGACY = 'logging-view-plugin'
CONSOLE = 'console.operator.openshift.io/cluster'
OPERATOR = 'deployment/observability-operator'
MONITORING_CR = 'uiplugin/monitoring'
LOGGING_CR = 'uiplugin/logging'
EXPERIMENTAL_LOGGING = 'observability.openshift.io/ui-plugin-experimental-logging'
CONSOLE_DISABLED = 'observability.openshift.io/ui-plugin-console-disabled'
SESSION_MARKER = 'monitoring.openshift.io/logging-dev-session'
CONFIG_HASH = 'observability.openshift.io/ui-plugin-config-hash'
PLUGIN_CONFIG_ARG = '-plugin-config-path=/etc/plugin/config/config.yaml'
BRIDGE_TIMEOUT = int(os.environ.get('LOGGING_BRIDGE_TIMEOUT', '300'))
POLL_INTERVAL = 5


def kubectl(*args, data=None):
    result = subprocess.run(['kubectl', *args], input=json.dumps(data) if data is not None else None,
                            text=True, stdout=subprocess.PIPE, check=True)
    return result.stdout


def get(resource, namespace=None):
    args = ['get', resource, '--ignore-not-found', '-o', 'json']
    if namespace:
        args += ['-n', namespace]
    result = kubectl(*args)
    return json.loads(result) if result.strip() else None


def save(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + '\n')
    STATE.chmod(0o600)


def patch(resource, operations, namespace=None):
    args = ['patch', resource, '--type=json', '-p', json.dumps(operations)]
    if namespace:
        args += ['-n', namespace]
    kubectl(*args)


def merge_patch(resource, patch_data):
    kubectl('patch', resource, '--type=merge', '-p', json.dumps(patch_data))


def annotate(resource, key, value):
    """Set the annotation to value, or remove it when value is None."""
    if value is None:
        kubectl('annotate', resource, '--overwrite', f'{key}-')
    else:
        kubectl('annotate', resource, '--overwrite', f'{key}={value}')


def deployment_replicas(deployment):
    spec = (deployment or {}).get('spec', {}) or {}
    replicas = spec.get('replicas')
    return 1 if replicas is None else int(replicas)


def console_plugins():
    console = get(CONSOLE)
    return console.get('spec', {}).get('plugins', []) if console else []


def wait_for(description, check):
    deadline = time.monotonic() + BRIDGE_TIMEOUT
    while True:
        if check():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'timed out waiting for {description}')
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Bridge mode: COO provisions MCP; this helper only flips CR annotations.
# ---------------------------------------------------------------------------

def bridge_logging_spec(loki_ns, loki_name):
    """CR logging settings for the bridge; the documented development defaults."""
    schema = os.environ.get('LOGGING_SCHEMA', 'select')
    if schema not in ('viaq', 'otel', 'select'):
        raise RuntimeError('LOGGING_SCHEMA must be viaq, otel or select')
    return {
        'lokiStack': {'name': loki_name, 'namespace': loki_ns},
        'logsLimit': int(os.environ.get('LOGGING_LOGS_LIMIT', '100')),
        'timeout': os.environ.get('LOGGING_TIMEOUT', '30s'),
        'schema': schema,
        'showTimezoneSelector': os.environ.get(
            'LOGGING_SHOW_TIMEZONE_SELECTOR', 'true').lower() == 'true',
    }


def check_bridge_prerequisites(loki_ns, loki_name):
    service = get(f'service/{loki_name}-gateway-http', loki_ns)
    if not service or not any(p['port'] == 8080 for p in service['spec']['ports']):
        raise RuntimeError(f'Expected Loki gateway {loki_ns}/{loki_name}-gateway-http on port 8080')
    operator = get(OPERATOR, NS)
    if not operator or deployment_replicas(operator) < 1:
        raise RuntimeError(f'Logging development needs a running COO deployment {NS}/{OPERATOR} '
                           'that contains the logging bridge; install the development bundle '
                           'and scale COO up first')


def create_bridge_monitoring_cr(state, loki_ns, loki_name):
    cr = {
        'apiVersion': 'observability.openshift.io/v1alpha1',
        'kind': 'UIPlugin',
        'metadata': {
            'name': 'monitoring',
            'annotations': {SESSION_MARKER: state['session'], EXPERIMENTAL_LOGGING: 'true'},
        },
        'spec': {
            'type': 'Monitoring',
            'logging': bridge_logging_spec(loki_ns, loki_name),
        },
    }
    # Record intent before mutation so a failed setup remains recoverable.
    state['created'].append({'resource': MONITORING_CR, 'namespace': None})
    save(state)
    kubectl('create', '-f', '-', data=cr)


def bridge_deployment_ready(deployment):
    if not deployment:
        return False
    template = deployment['spec']['template']
    annotations = template['metadata'].get('annotations', {}) or {}
    container = template['spec']['containers'][0]
    args = container.get('args', [])
    features = next((arg[len('-features='):] for arg in args if arg.startswith('-features=')), '')
    return ('logging' in features.split(',')
            and PLUGIN_CONFIG_ARG in args
            and any(mount.get('name') == 'plugin-config' for mount in container.get('volumeMounts', []))
            and CONFIG_HASH in annotations)


def nested_logging_config(configmap):
    content = (configmap or {}).get('data', {}).get('config.yaml', '')
    return any(line.startswith('logging:') for line in content.splitlines())


def bridge_loki_proxy(consoleplugin, loki_ns, loki_name):
    for proxy in ((consoleplugin or {}).get('spec', {}) or {}).get('proxy', []):
        if proxy.get('alias') != 'loki':
            continue
        service = (proxy.get('endpoint', {}) or {}).get('service', {}) or {}
        return (proxy.get('authorization') == 'UserToken'
                and service.get('name') == f'{loki_name}-gateway-http'
                and service.get('namespace') == loki_ns
                and service.get('port') == 8080)
    return False


def loki_proxy_present(consoleplugin):
    proxies = ((consoleplugin or {}).get('spec', {}) or {}).get('proxy', [])
    return any(proxy.get('alias') == 'loki' for proxy in proxies)


def verify_bridge(loki_ns, loki_name):
    """Wait until the bridged COO actually provisioned merged logging."""
    def bridge_ready():
        return (bridge_deployment_ready(get('deployment/monitoring', NS))
                and nested_logging_config(get('configmap/monitoring', NS))
                and bridge_loki_proxy(get(f'consoleplugin/{MCP}'), loki_ns, loki_name))
    wait_for('the COO logging bridge to provision merged logging '
             '(is the running COO built with the bridge?)', bridge_ready)
    wait_for('the MCP serving certificate', lambda: bool(get('secret/monitoring', NS)))
    kubectl('rollout', 'status', 'deployment/monitoring', '-n', NS,
            f'--timeout={BRIDGE_TIMEOUT}s')


def setup_bridge():
    loki_ns = os.environ.get('LOKI_NAMESPACE', 'openshift-logging')
    loki_name = os.environ.get('LOKI_STACK', 'logging-loki')
    features = os.environ.get('LOGGING_DEV_FEATURES', 'logging')
    allowed = {'logging', 'cluster-health-analyzer', 'perses-dashboards', 'perses-ui-customization', 'acm-alerting'}
    if not features or not set(features.split(',')) <= allowed:
        raise RuntimeError(f'LOGGING_DEV_FEATURES must be a comma-separated subset of {sorted(allowed)}')
    context = kubectl('config', 'current-context').strip()
    check_bridge_prerequisites(loki_ns, loki_name)
    monitoring = get(MONITORING_CR)
    logging_cr = get(LOGGING_CR)
    plugins = console_plugins()
    # Record intent before mutation: original replicas, CR fields and
    # annotation values, and whether a Monitoring CR already exists.
    state = {
        'context': context,
        'session': str(uuid.uuid4()),
        'mode': 'bridge',
        'operator_replicas': deployment_replicas(get(OPERATOR, NS)),
        'created': [],
        'monitoring_cr': {
            'existed': bool(monitoring),
            'annotation': (monitoring or {}).get('metadata', {}).get('annotations', {}).get(EXPERIMENTAL_LOGGING),
            'spec_logging': (monitoring or {}).get('spec', {}).get('logging'),
        },
        'logging_cr': {
            'existed': bool(logging_cr),
            'annotation': (logging_cr or {}).get('metadata', {}).get('annotations', {}).get(CONSOLE_DISABLED),
        },
        'plugins': {LEGACY: LEGACY in plugins, MCP: MCP in plugins},
    }
    save(state)
    (STATE_DIR / 'features').write_text(features)
    config_path = os.environ.get('LOGGING_CONFIG')
    if config_path:
        # Local override for settings not exposed by COO and quick iteration;
        # it overrides the operator-generated config only for the dev runtime.
        (STATE_DIR / 'config.yaml').write_text(Path(config_path).read_text())
    else:
        # Without an override the dev runtime reads the COO-generated
        # ConfigMap; remove a stale override from an earlier session.
        (STATE_DIR / 'config.yaml').unlink(missing_ok=True)

    # Disable the standalone Console registration first and confirm it is
    # gone, so duplicate routes and alert-source IDs are never loaded together.
    if logging_cr:
        annotate(LOGGING_CR, CONSOLE_DISABLED, 'true')
        wait_for(f'{LEGACY} deregistration from the Console',
                 lambda: LEGACY not in console_plugins())

    # Apply the opted-in Monitoring CR/config while COO is running.
    if monitoring:
        merge_patch(MONITORING_CR, {
            'metadata': {'annotations': {EXPERIMENTAL_LOGGING: 'true'}},
            'spec': {'logging': bridge_logging_spec(loki_ns, loki_name)},
        })
    else:
        create_bridge_monitoring_cr(state, loki_ns, loki_name)

    verify_bridge(loki_ns, loki_name)

    # DevSpace replaces the managed Deployment, so COO must stay paused during
    # development. The helper also re-enables MCP in Console after COO's
    # shutdown deregistration.
    subprocess.run([str(ROOT / 'hack/scale-down-coo.sh')], check=True)
    print(f'Logging development prepared in {context} (bridge); features={features}. '
          'Cleanup: devspace purge --var PLUGIN=mcp-logging')


def delete_session_created(state):
    for item in reversed(state['created']):
        if item['resource'].startswith('Namespace/'):
            print(f"Leaving development namespace {NS}; remove it separately when empty")
            continue
        resource = get(item['resource'], item['namespace'])
        if not resource:
            continue
        marker = resource['metadata'].get('annotations', {}).get(SESSION_MARKER)
        if marker != state['session']:
            raise RuntimeError(f"Refusing to remove {item['resource']}: development ownership changed")
        args = ['delete', item['resource'], '--ignore-not-found']
        if item['namespace']:
            args += ['-n', item['namespace']]
        kubectl(*args)


def verify_bridge_restoration(state):
    monitoring = state['monitoring_cr']
    if monitoring['existed']:
        enabled = monitoring['annotation'] == 'true'
        wait_for('the original MCP logging feature state',
                 lambda: bridge_deployment_ready(get('deployment/monitoring', NS)) == enabled)
        wait_for('the original ConsolePlugin loki proxy state',
                 lambda: loki_proxy_present(get(f'consoleplugin/{MCP}')) == enabled)
        if get('deployment/monitoring', NS):
            kubectl('rollout', 'status', 'deployment/monitoring', '-n', NS,
                    f'--timeout={BRIDGE_TIMEOUT}s')
    else:
        wait_for('MCP resources removed',
                 lambda: not get('deployment/monitoring', NS) and not get(f'consoleplugin/{MCP}'))
        wait_for('MCP deregistered from the Console', lambda: MCP not in console_plugins())


def cleanup_bridge(state):
    monitoring = state['monitoring_cr']
    logging_state = state['logging_cr']
    running = state['operator_replicas'] is not None and int(state['operator_replicas']) > 0
    if not running and state['created']:
        raise RuntimeError('refusing to delete session-created resources while COO is paused')

    # Keep standalone extensions disabled until MCP has reached its restored state.
    if logging_state['existed']:
        annotate(LOGGING_CR, CONSOLE_DISABLED, 'true')
    if monitoring['existed']:
        current = get(MONITORING_CR)
        if not current:
            raise RuntimeError('Monitoring UIPlugin disappeared; refusing to discard saved state')
        operations = []
        if monitoring['spec_logging'] is not None:
            operations.append({'op': 'add', 'path': '/spec/logging', 'value': monitoring['spec_logging']})
        elif 'logging' in current.get('spec', {}):
            operations.append({'op': 'remove', 'path': '/spec/logging'})
        if operations:
            patch(MONITORING_CR, [
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
                *operations,
            ])
        annotate(MONITORING_CR, EXPERIMENTAL_LOGGING, monitoring['annotation'])

    if running:
        env = dict(os.environ, COO_REPLICAS=str(state['operator_replicas']))
        subprocess.run([str(ROOT / 'hack/scale-up-coo.sh')], env=env, check=True)
        # COO must be running to process deletion and deregister session-created MCP.
        delete_session_created(state)
        verify_bridge_restoration(state)
    else:
        set_plugins({MCP: state['plugins'].get(MCP, False)})

    if logging_state['existed']:
        annotate(LOGGING_CR, CONSOLE_DISABLED, logging_state['annotation'])
        if running and state['plugins'].get(LEGACY) and logging_state['annotation'] != 'true':
            wait_for(f'{LEGACY} re-registration', lambda: LEGACY in console_plugins())
    set_plugins({LEGACY: state['plugins'].get(LEGACY, False)})
    STATE.unlink()
    print('Bridge session restored: CR settings, Console registrations and COO replicas')


# ---------------------------------------------------------------------------
# Console updates and recovery of sessions created by the old fallback setup.
# ---------------------------------------------------------------------------

def set_plugins(wanted):
    # Read-modify-write with a resourceVersion test: preserve unrelated registrations.
    console = get(CONSOLE)
    plugins = console.get('spec', {}).get('plugins', [])
    for name, enabled in wanted.items():
        if enabled and name not in plugins:
            plugins.append(name)
        elif not enabled:
            plugins = [p for p in plugins if p != name]
    patch(CONSOLE, [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': console['metadata']['resourceVersion']},
        {'op': 'add', 'path': '/spec/plugins', 'value': plugins},
    ])


def set_loki(value):
    resource = f'consoleplugin/{MCP}'
    plugin = get(resource)
    proxies = [p for p in plugin['spec'].get('proxy', []) if p['alias'] != 'loki']
    if value is not None:
        proxies.append(value)
    patch(resource, [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': plugin['metadata']['resourceVersion']},
        {'op': 'add', 'path': '/spec/proxy', 'value': proxies},
    ])


def cleanup_fallback(state):
    # shortcut: recovery only for old saved sessions; remove after those sessions are purged.
    if state['had_plugin'] and get(f'consoleplugin/{MCP}'):
        set_loki(state['loki'])
    set_plugins(state['plugins'])
    delete_session_created(state)
    if state['operator_replicas'] is not None:
        env = dict(os.environ, COO_REPLICAS=str(state['operator_replicas']))
        subprocess.run([str(ROOT / 'hack/scale-up-coo.sh')], env=env, check=True)
    STATE.unlink()
    print('Original COO replicas, Loki proxy and affected Console registrations restored')


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def setup():
    if STATE.exists():
        raise RuntimeError('Logging dev state already exists; run devspace purge with mcp-logging first')
    if os.environ.get('LOGGING_DEVSPACE_MODE', 'bridge') != 'bridge':
        raise RuntimeError('Fallback startup is no longer supported; install the COO development bridge')
    setup_bridge()


def check_context():
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if kubectl('config', 'current-context').strip() != state['context']:
            raise RuntimeError(f"Switch back to saved context {state['context']} before cleanup")


def cleanup():
    if not STATE.exists():
        print('No logging dev state to restore')
        return
    state = json.loads(STATE.read_text())
    check_context()
    if state.get('mode', 'fallback') == 'bridge':
        cleanup_bridge(state)
    else:
        cleanup_fallback(state)


if __name__ == '__main__':
    try:
        {'setup': setup, 'cleanup': cleanup, 'check-context': check_context}[sys.argv[1]]()
    except (KeyError, IndexError):
        sys.exit('Usage: logging-devspace.py setup|cleanup|check-context')
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f'{error}\nIf setup partially completed, run devspace purge --var PLUGIN=mcp-logging')
