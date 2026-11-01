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

from neutron_lib.api.definitions import network_internal_dns as apidef
from neutron_lib import constants
from neutron_lib import context
from oslo_config import cfg

from neutron.objects import network_internal_dns as ni_dns_obj
from neutron.tests.unit.plugins.ml2 import test_plugin


_NOT_SPECIFIED = constants.ATTR_NOT_SPECIFIED


class NetworkInternalDnsExtensionTestCase(test_plugin.Ml2PluginV2TestCase):
    _extension_drivers = ['network_internal_dns']

    def setUp(self):
        cfg.CONF.set_override('extension_drivers',
                              self._extension_drivers, group='ml2')
        super().setUp()

    def _create_network_with_attr(self, enabled=_NOT_SPECIFIED):
        kwargs = {}
        if enabled is not _NOT_SPECIFIED:
            kwargs = {'arg_list': (apidef.INTERNAL_DNS_ENABLED, ),
                      apidef.INTERNAL_DNS_ENABLED: enabled}
        res = self._create_network(self.fmt, 'net', True, as_admin=True,
                                   **kwargs)
        return self.deserialize(self.fmt, res)['network']

    def _update_network(self, network_id, enabled):
        data = {'network': {apidef.INTERNAL_DNS_ENABLED: enabled}}
        req = self.new_update_request('networks', data, network_id,
                                      as_admin=True)
        return self.deserialize(self.fmt,
                                req.get_response(self.api))['network']

    def _get_db_object(self, network_id):
        return ni_dns_obj.NetworkInternalDns.get_object(
            context.get_admin_context(), network_id=network_id)

    def test_default_is_enabled_without_row(self):
        network = self._create_network_with_attr()
        self.assertIs(True, network[apidef.INTERNAL_DNS_ENABLED])
        self.assertIsNone(self._get_db_object(network['id']))

    def test_create_enabled_does_not_store_a_row(self):
        network = self._create_network_with_attr(enabled=True)
        self.assertIs(True, network[apidef.INTERNAL_DNS_ENABLED])
        self.assertIsNone(self._get_db_object(network['id']))

    def test_create_disabled(self):
        network = self._create_network_with_attr(enabled=False)
        self.assertIs(False, network[apidef.INTERNAL_DNS_ENABLED])
        self.assertFalse(self._get_db_object(network['id']).enabled)

    def test_update_disable_and_enable(self):
        network = self._create_network_with_attr()

        updated = self._update_network(network['id'], False)
        self.assertIs(False, updated[apidef.INTERNAL_DNS_ENABLED])
        self.assertFalse(self._get_db_object(network['id']).enabled)

        updated = self._update_network(network['id'], True)
        self.assertIs(True, updated[apidef.INTERNAL_DNS_ENABLED])
        self.assertTrue(self._get_db_object(network['id']).enabled)

    def test_update_enabled_without_row_does_not_store_a_row(self):
        network = self._create_network_with_attr()
        self._update_network(network['id'], True)
        self.assertIsNone(self._get_db_object(network['id']))

    def test_update_other_attribute_keeps_the_value(self):
        network = self._create_network_with_attr(enabled=False)
        data = {'network': {'name': 'new-name'}}
        req = self.new_update_request('networks', data, network['id'],
                                      as_admin=True)
        updated = self.deserialize(self.fmt,
                                   req.get_response(self.api))['network']
        self.assertIs(False, updated[apidef.INTERNAL_DNS_ENABLED])

    def test_show_network(self):
        network = self._create_network_with_attr(enabled=False)
        req = self.new_show_request('networks', network['id'], as_admin=True)
        shown = self.deserialize(self.fmt,
                                 req.get_response(self.api))['network']
        self.assertIs(False, shown[apidef.INTERNAL_DNS_ENABLED])

    def test_delete_network_removes_the_row(self):
        network = self._create_network_with_attr(enabled=False)
        req = self.new_delete_request('networks', network['id'],
                                      as_admin=True)
        req.get_response(self.api)
        self.assertIsNone(self._get_db_object(network['id']))
