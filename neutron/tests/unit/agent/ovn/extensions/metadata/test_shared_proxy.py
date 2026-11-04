#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json
import os
from unittest import mock

import fixtures
from neutron_lib import exceptions
from oslo_config import cfg

from neutron.agent.ovn.extensions.metadata import shared_proxy
from neutron.conf.agent.metadata import config as meta_conf
from neutron.tests import base

NET1 = '11111111-1111-4111-8111-111111111111'
NET2 = '22222222-2222-4222-8222-222222222222'


def entry(net, v6=True):
    return ('ovnmeta-' + net, '169.254.169.254', 80,
            'fe80::a9fe:a9fe' if v6 else None, 'tap' + net[:10] + '1')


class TestSharedMetadataProxy(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.conf = cfg.ConfigOpts()
        self.conf.register_opts(meta_conf.SHARED_OPTS)
        self.conf.register_opts(meta_conf.METADATA_HAPROXY_OPTS,
                                meta_conf.HAPROXY_GROUP)
        self.conf.register_opts(meta_conf.METADATA_RATE_LIMITING_OPTS,
                                meta_conf.RATE_LIMITING_GROUP)
        self.conf.register_opt(cfg.StrOpt('state_path', default='/state'))
        self.conf.register_opt(cfg.StrOpt('external_pids',
                                          default='/state/pids'))
        self.conf([], project='neutron', default_config_files=[])
        self.conf.set_override('metadata_proxy_user', 'root')
        self.conf.set_override('metadata_proxy_group', 'root')
        self.conf.set_override('metadata_proxy_socket', '/state/md')
        mock.patch('oslo_utils.fileutils.ensure_tree').start()
        self.pm = mock.Mock(active=True, pid=42, cmd_addl_env={'T': 'x'})
        self.pm.get_pid_file_name.return_value = '/state/pid'
        mock.patch.object(shared_proxy.SharedMetadataProxy, 'process_manager',
                          return_value=self.pm).start()
        self.proxy = shared_proxy.SharedMetadataProxy(self.conf)
        self.write = mock.patch.object(self.proxy, '_write_config').start()
        self.read = mock.patch.object(self.proxy, '_read_config',
                                      return_value='').start()
        self.reload = mock.patch.object(self.proxy, '_reload').start()
        mock.patch.object(self.proxy, '_write_state').start()

    def _render(self, networks):
        return self.proxy.render(networks, '/state/pid')

    def test_each_network_gets_its_name_on_its_sockets(self):
        text = self._render({NET1: entry(NET1), NET2: entry(NET2, v6=False)})
        self.assertIn(f'bind 169.254.169.254:80 namespace ovnmeta-{NET1} '
                      f'name {NET1} maxconn 256\n', text)
        self.assertIn(f'bind [fe80::a9fe:a9fe]:80 interface tap11111111-'
                      f'11 namespace ovnmeta-{NET1} name {NET1} maxconn 256\n',
                      text)
        self.assertIn(f'bind 169.254.169.254:80 namespace ovnmeta-{NET2} '
                      f'name {NET2} maxconn 256\n', text)
        self.assertEqual(3, text.count('    bind '))

    def test_client_header_is_replaced(self):
        text = self._render({NET1: entry(NET1)})
        self.assertIn('http-request set-header X-OVN-Network-ID %[so_name]\n',
                      text)
        self.assertNotIn('add-header', text)

    def test_render_does_not_depend_on_insertion_order(self):
        self.assertEqual(
            self._render({NET1: entry(NET1), NET2: entry(NET2)}),
            self._render({NET2: entry(NET2), NET1: entry(NET1)}))

    def test_rate_limit_is_keyed_on_network_and_source(self):
        self.assertNotIn('stick-table', self._render({NET1: entry(NET1)}))
        self.conf.set_override('rate_limit_enabled', True,
                               meta_conf.RATE_LIMITING_GROUP)
        text = self._render({NET1: entry(NET1)})
        self.assertIn('"%[so_name]-%[src]"', text)
        self.assertEqual(2, text.count('stick-table type string'))
        self.assertLess(text.index('deny_status'), text.index('    bind '))

    def test_add_refuses_what_is_not_a_uuid(self):
        self.assertRaises(exceptions.InvalidInput, self.proxy.add,
                          'x name y', 'ns', '169.254.169.254', 80)

    def test_add_same_network_is_not_a_change(self):
        self.proxy.add(NET1, *entry(NET1))
        self.assertTrue(self.proxy._dirty.is_set())
        self.proxy._dirty.clear()
        self.proxy.add(NET1, *entry(NET1))
        self.assertFalse(self.proxy._dirty.is_set())

    def test_add_changed_network_is_a_change(self):
        self.proxy.add(NET1, *entry(NET1))
        self.proxy._dirty.clear()
        self.proxy.add(NET1, *entry(NET1, v6=False))
        self.assertTrue(self.proxy._dirty.is_set())

    def test_remove_applies_before_returning(self):
        self.proxy.add(NET1, *entry(NET1))
        self.proxy.add(NET2, *entry(NET2))
        self.proxy.remove(NET1)
        self.reload.assert_called_once_with()
        self.assertNotIn(NET1, self.write.call_args[0][0])
        self.assertIn(NET2, self.write.call_args[0][0])

    def test_remove_unknown_network_does_nothing(self):
        self.proxy.remove(NET1)
        self.reload.assert_not_called()
        self.pm.disable.assert_not_called()

    def test_remove_last_network_stops_the_process(self):
        self.proxy.add(NET1, *entry(NET1))
        self.proxy.remove(NET1)
        self.pm.disable.assert_called_once_with(sig='15')
        self.reload.assert_not_called()

    def test_apply_same_config_does_not_reload(self):
        self.proxy.add(NET1, *entry(NET1))
        self.read.return_value = self._render({NET1: entry(NET1)})
        self.assertTrue(self.proxy._apply())
        self.write.assert_not_called()
        self.reload.assert_not_called()
        # After an upgrade the process runs with no state file yet.
        self.proxy._write_state.assert_called_once_with(self.proxy._networks)

    def test_apply_starts_a_dead_process(self):
        self.proxy.add(NET1, *entry(NET1))
        self.pm.active = False
        self.read.return_value = self._render({NET1: entry(NET1)})
        self.assertTrue(self.proxy._apply())
        self.pm.enable.assert_called_once_with()
        self.reload.assert_not_called()

    def _fail_while(self, bad):
        def reload_():
            if bad in self.write.call_args[0][0]:
                raise exceptions.ProcessExecutionError('bind', 1)
        self.reload.side_effect = reload_

    def test_broken_network_does_not_block_the_others(self):
        self.proxy.add(NET1, *entry(NET1))
        self.proxy.add(NET2, *entry(NET2))
        self._fail_while(NET1)
        with mock.patch.object(self.proxy, '_is_broken',
                               side_effect=lambda n, e: n == NET1):
            self.assertFalse(self.proxy._apply())
        self.assertEqual(2, self.reload.call_count)
        self.assertNotIn(NET1, self.write.call_args[0][0])
        self.assertIn(NET2, self.write.call_args[0][0])
        # It stays known, so a later pass serves it when it recovers.
        self.assertIn(NET1, self.proxy._networks)

    def test_failure_with_no_network_at_fault_is_reported(self):
        self.proxy.add(NET1, *entry(NET1))
        self._fail_while(NET1)
        with mock.patch.object(self.proxy, '_is_broken', return_value=False):
            self.assertFalse(self.proxy._apply())
        self.assertEqual(1, self.reload.call_count)

    def test_all_networks_broken_does_not_stop_the_process(self):
        self.proxy.add(NET1, *entry(NET1))
        self._fail_while(NET1)
        with mock.patch.object(self.proxy, '_is_broken', return_value=True):
            self.assertFalse(self.proxy._apply())
        self.pm.disable.assert_not_called()

    def test_failed_check_does_not_mark_a_network_broken(self):
        with mock.patch.object(shared_proxy.ip_lib,
                               'network_namespace_exists',
                               side_effect=RuntimeError):
            self.assertFalse(self.proxy._is_broken(NET1, entry(NET1)))

    def test_missing_namespace_and_address_are_broken(self):
        with mock.patch.object(shared_proxy.ip_lib,
                               'network_namespace_exists',
                               return_value=False):
            self.assertTrue(self.proxy._is_broken(NET1, entry(NET1)))
        with mock.patch.object(shared_proxy.ip_lib,
                               'network_namespace_exists',
                               return_value=True), \
                mock.patch.object(shared_proxy.ip_lib, 'IPDevice') as device:
            device.return_value.addr.list.return_value = [
                {'cidr': '10.0.0.1/24'}]
            self.assertTrue(self.proxy._is_broken(NET1, entry(NET1)))
            device.return_value.addr.list.return_value = [
                {'cidr': '169.254.169.254/32'}]
            self.assertFalse(self.proxy._is_broken(NET1, entry(NET1)))

    def test_unexpected_error_does_not_raise(self):
        self.proxy.add(NET1, *entry(NET1))
        self.reload.side_effect = RuntimeError
        with mock.patch.object(self.proxy, '_is_broken', return_value=False):
            self.assertFalse(self.proxy._apply())

    def test_stop_leftover(self):
        shared_proxy.SharedMetadataProxy.stop_leftover(self.conf)
        self.pm.disable.assert_called_once_with(sig='15')
        self.pm.disable.reset_mock()
        self.pm.active = False
        shared_proxy.SharedMetadataProxy.stop_leftover(self.conf)
        self.pm.disable.assert_not_called()

    def _one_pass(self, apply_result=True):
        """Runs _run until it would wait a second time, then stops it."""
        self.proxy._batch = mock.Mock()
        waits = []

        def wait(timeout):
            waits.append(timeout)
            if len(waits) > 1:
                self.proxy._stop.set()
            return self.proxy._dirty.is_set() and len(waits) == 1
        self.proxy._dirty = mock.Mock(wraps=self.proxy._dirty)
        self.proxy._dirty.wait.side_effect = wait
        self.apply = mock.patch.object(self.proxy, '_apply',
                                       return_value=apply_result).start()
        self.proxy._run()

    def test_idle_pass_does_not_apply(self):
        self.proxy._networks[NET1] = entry(NET1)
        self._one_pass()
        self.apply.assert_not_called()

    def test_dead_process_is_applied(self):
        self.proxy._networks[NET1] = entry(NET1)
        self.pm.active = False
        self._one_pass()
        self.apply.assert_called()

    def test_failed_apply_is_retried(self):
        self.proxy._networks[NET1] = entry(NET1)
        self.proxy._retry = True
        self._one_pass(apply_result=False)
        self.assertTrue(self.proxy._retry)
        self.apply.assert_called()

    def _state_proxy(self, content):
        with mock.patch('builtins.open', mock.mock_open(read_data=content)):
            return shared_proxy.SharedMetadataProxy(self.conf)

    def test_restart_keeps_the_networks_of_a_running_process(self):
        proxy = self._state_proxy(json.dumps({NET1: list(entry(NET1))}))
        self.assertEqual({NET1: entry(NET1)}, proxy._networks)

    def test_restart_with_a_dead_process_starts_empty(self):
        self.pm.active = False
        proxy = self._state_proxy(json.dumps({NET1: list(entry(NET1))}))
        self.assertEqual({}, proxy._networks)

    def test_restart_with_a_bad_state_file_starts_empty(self):
        self.assertEqual({}, self._state_proxy('not json')._networks)

    def test_retain_applies_once(self):
        self.proxy.add(NET1, *entry(NET1))
        self.proxy.add(NET2, *entry(NET2))
        self.proxy.retain([NET2])
        self.assertEqual([NET2], list(self.proxy._networks))
        self.reload.assert_called_once_with()

    def test_retain_without_a_change_does_nothing(self):
        self.proxy.add(NET1, *entry(NET1))
        self.proxy.retain([NET1, NET2])
        self.reload.assert_not_called()


class TestSharedMetadataProxyFiles(base.BaseTestCase):
    """The files themselves: a new pod starts with an empty state path."""

    def setUp(self):
        super().setUp()
        self.state_path = os.path.join(
            self.useFixture(fixtures.TempDir()).path, 'new')
        self.conf = cfg.ConfigOpts()
        self.conf.register_opts(meta_conf.SHARED_OPTS)
        self.conf.register_opts(meta_conf.METADATA_HAPROXY_OPTS,
                                meta_conf.HAPROXY_GROUP)
        self.conf.register_opts(meta_conf.METADATA_RATE_LIMITING_OPTS,
                                meta_conf.RATE_LIMITING_GROUP)
        self.conf.register_opt(cfg.StrOpt('state_path'))
        self.conf.register_opt(cfg.StrOpt('external_pids'))
        self.conf([], project='neutron', default_config_files=[])
        self.conf.set_override('state_path', self.state_path)
        self.conf.set_override('metadata_proxy_user', 'root')
        self.conf.set_override('metadata_proxy_group', 'root')
        self.conf.set_override('metadata_proxy_socket', '/state/md')
        self.pm = mock.Mock(active=False, pid=None, cmd_addl_env={})
        self.pm.get_pid_file_name.return_value = '/state/pid'
        mock.patch.object(shared_proxy.SharedMetadataProxy, 'process_manager',
                          return_value=self.pm).start()

    def test_first_start_creates_the_directory(self):
        proxy = shared_proxy.SharedMetadataProxy(self.conf)
        self.assertTrue(proxy._apply_networks({NET1: entry(NET1)}))
        self.pm.enable.assert_called_once_with()
        self.assertTrue(os.path.exists(proxy.cfg_path))
        with open(proxy._state_path) as f:
            self.assertIn(NET1, json.load(f))
