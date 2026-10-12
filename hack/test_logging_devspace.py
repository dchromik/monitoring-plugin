"""Run with python3 -m unittest discover -s hack -p 'test_logging_devspace.py'."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('logging_devspace', Path(__file__).with_name('logging-devspace.py'))
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)

LOKI_SERVICE = {'spec': {'ports': [{'port': 8080}]}}
OPERATOR = {'spec': {'replicas': 2}}
DEPLOYMENT_BRIDGE = {
    'spec': {'template': {'metadata': {'annotations': {dev.CONFIG_HASH: 'abc'}},
                          'spec': {'containers': [{'args': ['-features=mcp-overview,logging',
                                                            dev.PLUGIN_CONFIG_ARG],
                                                   'volumeMounts': [{'name': 'plugin-config'}]}]}}}}
DEFAULT_CONSOLE = {'spec': {'plugins': [dev.LEGACY, dev.MCP]}}


def annotate_specs(calls, resource):
    """The annotation key specs kubectl received for a resource."""
    return [arg for call in calls if call[0] == 'kubectl' and call[1][:2] == ('annotate', resource)
            for arg in call[1][2:] if arg != '--overwrite']


def bridge_state(**overrides):
    state = {
        'context': 'test', 'session': 'sess-1', 'mode': 'bridge',
        'operator_replicas': 2, 'created': [],
        'monitoring_cr': {'existed': True, 'annotation': None, 'spec_logging': None},
        'logging_cr': {'existed': True, 'annotation': None},
        'plugins': {dev.LEGACY: True, dev.MCP: True},
    }
    state.update(overrides)
    return state


class LoggingDevspaceTest(unittest.TestCase):
    def test_registration_changes_preserve_unrelated_plugins(self):
        console = {'metadata': {'resourceVersion': '12'},
                   'spec': {'plugins': ['monitoring-plugin', dev.LEGACY, 'other-plugin']}}
        with patch.object(dev, 'get', return_value=console), patch.object(dev, 'patch') as write:
            dev.set_plugins({dev.MCP: True, dev.LEGACY: False})
        changes = write.call_args.args[1]
        self.assertEqual(changes[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '12'})
        self.assertEqual(changes[1]['value'], ['monitoring-plugin', 'other-plugin', dev.MCP])

    def test_proxy_restore_preserves_backend_and_new_proxies(self):
        backend = {'alias': 'backend'}
        unrelated = {'alias': 'added-during-development'}
        old_loki = {'alias': 'loki', 'authorization': 'UserToken', 'endpoint': {'old': True}}
        plugin = {'metadata': {'resourceVersion': '13'},
                  'spec': {'proxy': [backend, {'alias': 'loki'}, unrelated]}}
        with patch.object(dev, 'get', return_value=plugin), patch.object(dev, 'patch') as write:
            dev.set_loki(old_loki)
        self.assertEqual(write.call_args.args[1][1]['value'], [backend, unrelated, old_loki])

    def test_cleanup_restores_original_replicas_and_context_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            state.write_text(json.dumps({'context': 'test', 'operator_replicas': 3,
                                         'plugins': {dev.LEGACY: True, dev.MCP: True},
                                         'loki': None, 'created': [], 'had_plugin': False}))
            with patch.object(dev, 'STATE', state), patch.object(dev, 'kubectl', return_value='wrong'):
                with self.assertRaises(RuntimeError):
                    dev.cleanup()
                self.assertTrue(state.exists())
            with patch.object(dev, 'STATE', state), patch.object(dev, 'kubectl', return_value='test'), \
                    patch.object(dev, 'set_loki'), patch.object(dev, 'set_plugins'), \
                    patch.object(dev.subprocess, 'run') as run:
                dev.cleanup()
                self.assertEqual(run.call_args.kwargs['env']['COO_REPLICAS'], '3')
                self.assertFalse(state.exists())


class BridgeSetupTest(unittest.TestCase):
    def run_setup(self, gets, calls, bridge_wait_fails=False):
        """Run setup_bridge with canned cluster state, recording every call."""
        def wait_for(description, check):
            if bridge_wait_fails and 'COO logging bridge' in description:
                raise RuntimeError(f'timed out waiting for {description}')
            return check()

        real_save = dev.save

        def record_save(state):
            calls.append(('save',))
            real_save(state)

        def fake_kubectl(*args, data=None):
            calls.append(('kubectl', args, data))
            if args[:2] == ('config', 'current-context'):
                return 'test'
            return ''

        def fake_get(resource, namespace=None):
            calls.append(('get', resource))
            return gets.get(resource)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(dev, 'STATE', Path(directory) / 'state.json'), \
                patch.object(dev, 'STATE_DIR', Path(directory)), \
                patch.object(dev, 'save', record_save), \
                patch.object(dev, 'kubectl', side_effect=fake_kubectl), \
                patch.object(dev, 'get', side_effect=fake_get), \
                patch.object(dev, 'wait_for', wait_for), \
                patch.object(dev.subprocess, 'run', side_effect=lambda *a, **k: calls.append(('run', a, k))), \
                patch.dict('os.environ', {'LOKI_NAMESPACE': 'openshift-logging', 'LOKI_STACK': 'logging-loki'}):
            try:
                dev.setup_bridge()
                error = None
            except RuntimeError as failure:
                error = failure
            state_path = Path(directory) / 'state.json'
            state = json.loads(state_path.read_text()) if state_path.exists() else None
        return state, error

    def test_setup_order_state_first_standalone_before_merged(self):
        calls = []
        state, error = self.run_setup({
            'service/logging-loki-gateway-http': LOKI_SERVICE,
            'deployment/observability-operator': OPERATOR,
            dev.MONITORING_CR: {'metadata': {'annotations': {'unrelated': 'keep'}},
                                'spec': {'type': 'Monitoring', 'monitoring': {'perses': {'enabled': True}}}},
            dev.LOGGING_CR: {'metadata': {'annotations': {}}},
            dev.CONSOLE: {'spec': {'plugins': [dev.LEGACY, 'other']}},
        }, calls)
        self.assertIsNone(error)
        self.assertIsNotNone(state)
        self.assertEqual(state['mode'], 'bridge')
        self.assertEqual(state['operator_replicas'], 2)
        self.assertTrue(state['logging_cr']['existed'])
        self.assertTrue(state['monitoring_cr']['existed'])
        self.assertTrue(state['plugins'][dev.LEGACY])

        # Intent is recorded before the first mutation.
        first_mutation = next(i for i, call in enumerate(calls)
                              if call[0] == 'kubectl' and call[1][0] in ('annotate', 'patch', 'create'))
        self.assertLess(next(i for i, call in enumerate(calls) if call[0] == 'save'), first_mutation)
        # The standalone logging view is disabled before merged logging is enabled.
        standalone = next(i for i, call in enumerate(calls)
                          if call[0] == 'kubectl' and call[1][:2] == ('annotate', dev.LOGGING_CR))
        merged = next(i for i, call in enumerate(calls)
                      if call[0] == 'kubectl' and call[1][:2] == ('patch', dev.MONITORING_CR))
        self.assertLess(standalone, merged)
        self.assertIn(f'{dev.CONSOLE_DISABLED}=true', annotate_specs(calls, dev.LOGGING_CR))
        # The Monitoring CR patch only touches the bridge annotation and spec.logging.
        patch_call = calls[merged]
        payload = json.loads(patch_call[1][patch_call[1].index('-p') + 1])
        self.assertEqual(payload['metadata']['annotations'], {dev.EXPERIMENTAL_LOGGING: 'true'})
        self.assertIn('lokiStack', payload['spec']['logging'])
        self.assertNotIn('monitoring', payload['spec'])
        # COO is paused last, after the bridge effects were verified.
        self.assertEqual(calls[-1][0], 'run')
        self.assertIn('scale-down-coo.sh', str(calls[-1][1]))

    def test_setup_creates_monitoring_cr_when_absent(self):
        calls = []
        state, error = self.run_setup({
            'service/logging-loki-gateway-http': LOKI_SERVICE,
            'deployment/observability-operator': OPERATOR,
            dev.MONITORING_CR: None,
            dev.LOGGING_CR: {'metadata': {'annotations': {}}},
            dev.CONSOLE: {'spec': {'plugins': [dev.LEGACY]}},
        }, calls)
        self.assertIsNone(error)
        self.assertIsNotNone(state)
        create_call = next(call for call in calls if call[0] == 'kubectl' and call[1][0] == 'create')
        cr = create_call[2]
        self.assertEqual(cr['metadata']['name'], 'monitoring')
        self.assertEqual(cr['spec']['type'], 'Monitoring')
        self.assertEqual(cr['metadata']['annotations'][dev.EXPERIMENTAL_LOGGING], 'true')
        self.assertEqual(cr['metadata']['annotations'][dev.SESSION_MARKER], state['session'])
        self.assertEqual(state['created'], [{'resource': dev.MONITORING_CR, 'namespace': None}])

    def test_setup_rejects_old_operator_ignoring_annotations(self):
        calls = []
        state, error = self.run_setup({
            'service/logging-loki-gateway-http': LOKI_SERVICE,
            'deployment/observability-operator': OPERATOR,
            dev.MONITORING_CR: None,
            dev.LOGGING_CR: {'metadata': {'annotations': {}}},
            dev.CONSOLE: {'spec': {'plugins': [dev.MCP]}},
            'deployment/monitoring': {'spec': {'template': {'spec': {'containers': [{'args': []}]}}}},
        }, calls, bridge_wait_fails=True)
        self.assertIsNotNone(error)
        self.assertIn('COO logging bridge', str(error))
        # The partially mutated state remains for recovery via purge.
        self.assertIsNotNone(state)

    def test_setup_requires_running_coo(self):
        calls = []
        state, error = self.run_setup({
            'service/logging-loki-gateway-http': LOKI_SERVICE,
            'deployment/observability-operator': {'spec': {'replicas': 0}},
        }, calls)
        self.assertIsNotNone(error)
        self.assertIn('install the development bundle', str(error))
        self.assertIsNone(state)


class BridgeCleanupTest(unittest.TestCase):
    def run_cleanup(self, state, calls, gets=None, wait_error=None):
        def wait_for(description, check):
            calls.append(('wait', description))
            if wait_error is not None:
                raise wait_error
            if not check():
                raise RuntimeError(f'timed out waiting for {description}')

        gets = dict(gets) if gets is not None else {dev.CONSOLE: DEFAULT_CONSOLE}
        if state['monitoring_cr']['existed']:
            gets.setdefault(dev.MONITORING_CR, {
                'metadata': {'resourceVersion': '12'},
                'spec': {'logging': {'logsLimit': 100, 'timeout': '30s', 'schema': 'select'},
                         'monitoring': {'perses': {'enabled': True}}},
            })

        def fake_kubectl(*args, data=None):
            calls.append(('kubectl', args, data))
            if args[:2] == ('config', 'current-context'):
                return 'test'
            return ''

        def fake_get(resource, namespace=None):
            return gets.get(resource)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(dev, 'STATE', Path(directory) / 'state.json'), \
                patch.object(dev, 'kubectl', side_effect=fake_kubectl), \
                patch.object(dev, 'get', side_effect=fake_get), \
                patch.object(dev, 'wait_for', wait_for), \
                patch.object(dev, 'set_plugins', lambda wanted: calls.append(('set_plugins', wanted))), \
                patch.object(dev.subprocess, 'run', side_effect=lambda *a, **k: calls.append(('run', a, k))):
            Path(directory, 'state.json').write_text(json.dumps(state))
            try:
                dev.cleanup_bridge(state)
                error = None
            except RuntimeError as failure:
                error = failure
            removed = not Path(directory, 'state.json').exists()
        return error, removed

    def test_cleanup_restores_existing_cr_annotations_and_settings(self):
        calls = []
        state = bridge_state(
            monitoring_cr={'existed': True, 'annotation': 'false',
                           'spec_logging': {'logsLimit': 42, 'schema': 'viaq'}},
            logging_cr={'existed': True, 'annotation': 'false'})
        error, removed = self.run_cleanup(state, calls)
        self.assertIsNone(error)
        self.assertTrue(removed)
        self.assertIn(f'{dev.CONSOLE_DISABLED}=false', annotate_specs(calls, dev.LOGGING_CR))
        self.assertIn(f'{dev.EXPERIMENTAL_LOGGING}=false', annotate_specs(calls, dev.MONITORING_CR))
        patch_call = next(call for call in calls
                          if call[0] == 'kubectl' and call[1][:2] == ('patch', dev.MONITORING_CR))
        payload = json.loads(patch_call[1][patch_call[1].index('-p') + 1])
        self.assertIn('--type=json', patch_call[1])
        self.assertEqual(payload, [
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '12'},
            {'op': 'add', 'path': '/spec/logging', 'value': {'logsLimit': 42, 'schema': 'viaq'}},
        ])
        # Original replica count is restored and the standalone registration verified.
        run = next(call for call in calls if call[0] == 'run')
        self.assertEqual(run[2]['env']['COO_REPLICAS'], '2')
        self.assertIn({dev.LEGACY: True}, [call[1] for call in calls if call[0] == 'set_plugins'])

    def test_cleanup_removes_annotation_when_originally_absent(self):
        calls = []
        error, removed = self.run_cleanup(bridge_state(), calls)
        self.assertIsNone(error)
        self.assertTrue(removed)
        self.assertIn(f'{dev.CONSOLE_DISABLED}-', annotate_specs(calls, dev.LOGGING_CR))
        self.assertIn(f'{dev.EXPERIMENTAL_LOGGING}-', annotate_specs(calls, dev.MONITORING_CR))
        patch_call = next(call for call in calls
                          if call[0] == 'kubectl' and call[1][:2] == ('patch', dev.MONITORING_CR))
        payload = json.loads(patch_call[1][patch_call[1].index('-p') + 1])
        self.assertEqual(payload[-1], {'op': 'remove', 'path': '/spec/logging'})

    def test_cleanup_restores_originally_enabled_logging(self):
        state = bridge_state(
            monitoring_cr={'existed': True, 'annotation': 'true',
                           'spec_logging': {'logsLimit': 42}},
            logging_cr={'existed': True, 'annotation': 'true'},
            plugins={dev.LEGACY: False, dev.MCP: True})
        error, removed = self.run_cleanup(state, [], gets={
            'deployment/monitoring': DEPLOYMENT_BRIDGE,
            f'consoleplugin/{dev.MCP}': {'spec': {'proxy': [{'alias': 'loki'}]}},
            dev.CONSOLE: {'spec': {'plugins': [dev.MCP]}},
        })
        self.assertIsNone(error)
        self.assertTrue(removed)

    def test_cleanup_does_not_wait_for_previously_unregistered_standalone(self):
        state = bridge_state(plugins={dev.LEGACY: False, dev.MCP: True})
        error, removed = self.run_cleanup(state, [], gets={
            dev.CONSOLE: {'spec': {'plugins': [dev.MCP]}},
        })
        self.assertIsNone(error)
        self.assertTrue(removed)

    def test_cleanup_restores_standalone_only_after_merged_logging_is_gone(self):
        calls = []
        error, removed = self.run_cleanup(bridge_state(), calls)
        self.assertIsNone(error)
        self.assertTrue(removed)
        merged_wait = next(i for i, call in enumerate(calls)
                           if call[0] == 'wait' and 'loki proxy' in call[1])
        legacy_restore = next(i for i, call in enumerate(calls)
                              if call[0] == 'kubectl'
                              and call[1][:2] == ('annotate', dev.LOGGING_CR)
                              and f'{dev.CONSOLE_DISABLED}-' in call[1])
        self.assertLess(merged_wait, legacy_restore)

    def test_cleanup_waits_for_rollout_before_restoring_standalone(self):
        calls = []
        restored = json.loads(json.dumps(DEPLOYMENT_BRIDGE))
        restored['spec']['template']['spec']['containers'][0]['args'][0] = '-features=perses-dashboards'
        error, removed = self.run_cleanup(bridge_state(), calls, gets={
            'deployment/monitoring': restored,
            dev.CONSOLE: DEFAULT_CONSOLE,
        })
        self.assertIsNone(error)
        self.assertTrue(removed)
        rollout = next(i for i, call in enumerate(calls)
                       if call[0] == 'kubectl' and call[1][:2] == ('rollout', 'status'))
        legacy_restore = next(i for i, call in enumerate(calls)
                              if call[0] == 'kubectl'
                              and call[1][:2] == ('annotate', dev.LOGGING_CR)
                              and f'{dev.CONSOLE_DISABLED}-' in call[1])
        self.assertLess(rollout, legacy_restore)

    def test_cleanup_can_retry_when_original_logging_field_is_already_removed(self):
        error, removed = self.run_cleanup(bridge_state(), [], gets={
            dev.MONITORING_CR: {'metadata': {'resourceVersion': '13'}, 'spec': {}},
            dev.CONSOLE: DEFAULT_CONSOLE,
        })
        self.assertIsNone(error)
        self.assertTrue(removed)

    def test_cleanup_deletes_session_created_cr_and_waits_for_removal(self):
        calls = []
        state = bridge_state(
            monitoring_cr={'existed': False, 'annotation': None, 'spec_logging': None},
            created=[{'resource': dev.MONITORING_CR, 'namespace': None}])
        error, removed = self.run_cleanup(state, calls, gets={
            dev.MONITORING_CR: {'metadata': {'annotations': {dev.SESSION_MARKER: 'sess-1'}}},
            dev.CONSOLE: {'spec': {'plugins': [dev.LEGACY]}},
        })
        self.assertIsNone(error)
        self.assertTrue(removed)
        delete = next(call for call in calls
                      if call[0] == 'kubectl' and call[1][0] == 'delete')
        self.assertEqual(delete[1][1], dev.MONITORING_CR)

    def test_cleanup_refuses_foreign_created_cr(self):
        state = bridge_state(created=[{'resource': dev.MONITORING_CR, 'namespace': None}])
        error, removed = self.run_cleanup(state, [], gets={
            dev.MONITORING_CR: {'metadata': {'annotations': {dev.SESSION_MARKER: 'someone-else'}}},
        })
        self.assertIsNotNone(error)
        self.assertIn('ownership changed', str(error))
        self.assertFalse(removed)

    def test_cleanup_preserves_paused_coo_and_restores_registrations(self):
        calls = []
        state = bridge_state(operator_replicas=0, plugins={dev.LEGACY: False, dev.MCP: False})
        error, removed = self.run_cleanup(state, calls)
        self.assertIsNone(error)
        self.assertTrue(removed)
        self.assertFalse(any(call[0] == 'run' for call in calls), 'COO must stay paused')
        registrations = [call[1] for call in calls if call[0] == 'set_plugins']
        self.assertIn({dev.MCP: False}, registrations)
        self.assertIn({dev.LEGACY: False}, registrations)

    def test_cleanup_refuses_session_created_cr_while_coo_paused(self):
        state = bridge_state(operator_replicas=0,
                             created=[{'resource': dev.MONITORING_CR, 'namespace': None}])
        error, removed = self.run_cleanup(state, [])
        self.assertIsNotNone(error)
        self.assertIn('while COO is paused', str(error))
        self.assertFalse(removed, 'state must survive for retry once COO runs again')

    def test_cleanup_failure_keeps_state_and_retry_succeeds(self):
        state = bridge_state()
        error, removed = self.run_cleanup(state, [], wait_error=RuntimeError('timed out'))
        self.assertIsNotNone(error)
        self.assertFalse(removed, 'state must survive a failed cleanup for retry')
        calls = []
        error, removed = self.run_cleanup(state, calls)
        self.assertIsNone(error)
        self.assertTrue(removed)

    def test_setup_rejects_retired_fallback_mode_without_mutating_resources(self):
        with patch.object(dev, 'STATE') as state, \
                patch.object(dev, 'setup_bridge') as bridge, \
                patch.dict('os.environ', {'LOGGING_DEVSPACE_MODE': 'fallback'}):
            state.exists.return_value = False
            with self.assertRaisesRegex(RuntimeError, 'Fallback startup is no longer supported'):
                dev.setup()
            bridge.assert_not_called()

    def test_cleanup_dispatches_by_saved_mode_not_environment(self):
        # A fallback state file (no mode key) keeps using the fallback path
        # even when the environment now selects bridge mode.
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            state_path.write_text(json.dumps({
                'context': 'test', 'had_plugin': False, 'operator_replicas': 1,
                'plugins': {dev.LEGACY: True}, 'loki': None, 'created': []}))
            with patch.object(dev, 'STATE', state_path), \
                    patch.object(dev, 'kubectl', return_value='test'), \
                    patch.object(dev, 'cleanup_fallback') as fallback, \
                    patch.object(dev, 'cleanup_bridge') as bridge, \
                    patch.dict('os.environ', {'LOGGING_DEVSPACE_MODE': 'bridge'}):
                dev.cleanup()
                fallback.assert_called_once()
                bridge.assert_not_called()


class BridgePredicatesTest(unittest.TestCase):
    def test_bridge_deployment_ready_requires_all_effects(self):
        self.assertTrue(dev.bridge_deployment_ready(DEPLOYMENT_BRIDGE))
        without_feature = json.loads(json.dumps(DEPLOYMENT_BRIDGE))
        without_feature['spec']['template']['spec']['containers'][0]['args'][0] = '-features=mcp-overview'
        self.assertFalse(dev.bridge_deployment_ready(without_feature))
        self.assertFalse(dev.bridge_deployment_ready(None))

    def test_nested_logging_config_detects_top_level_key_only(self):
        self.assertTrue(dev.nested_logging_config({'data': {'config.yaml': 'logging:\n    logsLimit: 1\n'}}))
        # A nested key (indented) is not a top-level logging section.
        self.assertFalse(dev.nested_logging_config({'data': {'config.yaml': 'other:\n    logging: true\n'}}))

    def test_bridge_loki_proxy_checks_identity(self):
        proxy = {'alias': 'loki', 'authorization': 'UserToken',
                 'endpoint': {'service': {'name': 'logging-loki-gateway-http',
                                          'namespace': 'openshift-logging', 'port': 8080}}}
        self.assertTrue(dev.bridge_loki_proxy({'spec': {'proxy': [proxy]}}, 'openshift-logging', 'logging-loki'))
        unauthenticated = json.loads(json.dumps(proxy))
        unauthenticated['authorization'] = 'None'
        self.assertFalse(dev.bridge_loki_proxy({'spec': {'proxy': [unauthenticated]}},
                                               'openshift-logging', 'logging-loki'))
        self.assertFalse(dev.bridge_loki_proxy({'spec': {'proxy': []}}, 'openshift-logging', 'logging-loki'))
        self.assertTrue(dev.loki_proxy_present({'spec': {'proxy': [proxy]}}))
        self.assertFalse(dev.loki_proxy_present({'spec': {'proxy': []}}))


if __name__ == '__main__':
    unittest.main()
