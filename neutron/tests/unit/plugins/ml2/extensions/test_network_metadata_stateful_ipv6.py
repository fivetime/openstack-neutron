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

from neutron_lib.api.definitions import external_net as extnet_apidef
from neutron_lib.api.definitions import network_metadata_stateful_ipv6 \
    as apidef
from neutron_lib import constants
from neutron_lib import context
from oslo_config import cfg

from neutron.objects import network_metadata_stateful_ipv6 as md_ipv6_obj
from neutron.tests.unit.plugins.ml2 import test_plugin


_NOT_SPECIFIED = constants.ATTR_NOT_SPECIFIED


class NetworkMetadataStatefulIpv6ExtensionTestCase(
        test_plugin.Ml2PluginV2TestCase):
    _extension_drivers = ['network_metadata_stateful_ipv6']

    def setUp(self):
        cfg.CONF.set_override('extension_drivers',
                              self._extension_drivers, group='ml2')
        super().setUp()

    def _create_network_with_attr(self, enabled=_NOT_SPECIFIED):
        kwargs = {}
        if enabled is not _NOT_SPECIFIED:
            kwargs = {'arg_list': (apidef.METADATA_STATEFUL_IPV6_ADDRESS, ),
                      apidef.METADATA_STATEFUL_IPV6_ADDRESS: enabled}
        res = self._create_network(self.fmt, 'net', True, as_admin=True,
                                   **kwargs)
        return self.deserialize(self.fmt, res)['network']

    def _update_network(self, network_id, enabled):
        data = {'network': {apidef.METADATA_STATEFUL_IPV6_ADDRESS: enabled}}
        req = self.new_update_request('networks', data, network_id,
                                      as_admin=True)
        return self.deserialize(self.fmt,
                                req.get_response(self.api))['network']

    def _get_db_object(self, network_id):
        return md_ipv6_obj.NetworkMetadataStatefulIpv6.get_object(
            context.get_admin_context(), network_id=network_id)

    def test_default_is_disabled_without_row(self):
        network = self._create_network_with_attr()
        self.assertIs(False, network[apidef.METADATA_STATEFUL_IPV6_ADDRESS])
        self.assertIsNone(self._get_db_object(network['id']))

    def test_create_disabled_does_not_store_a_row(self):
        network = self._create_network_with_attr(enabled=False)
        self.assertIs(False, network[apidef.METADATA_STATEFUL_IPV6_ADDRESS])
        self.assertIsNone(self._get_db_object(network['id']))

    def test_create_enabled(self):
        network = self._create_network_with_attr(enabled=True)
        self.assertIs(True, network[apidef.METADATA_STATEFUL_IPV6_ADDRESS])
        self.assertTrue(self._get_db_object(network['id']).enabled)

    def test_update_enable_and_disable(self):
        network = self._create_network_with_attr()

        updated = self._update_network(network['id'], True)
        self.assertIs(True, updated[apidef.METADATA_STATEFUL_IPV6_ADDRESS])
        self.assertTrue(self._get_db_object(network['id']).enabled)

        updated = self._update_network(network['id'], False)
        self.assertIs(False, updated[apidef.METADATA_STATEFUL_IPV6_ADDRESS])
        self.assertFalse(self._get_db_object(network['id']).enabled)

    def test_update_disabled_without_row_does_not_store_a_row(self):
        network = self._create_network_with_attr()
        self._update_network(network['id'], False)
        self.assertIsNone(self._get_db_object(network['id']))

    def test_update_other_attribute_keeps_the_value(self):
        network = self._create_network_with_attr(enabled=True)
        data = {'network': {'name': 'new-name'}}
        req = self.new_update_request('networks', data, network['id'],
                                      as_admin=True)
        updated = self.deserialize(self.fmt,
                                   req.get_response(self.api))['network']
        self.assertIs(True, updated[apidef.METADATA_STATEFUL_IPV6_ADDRESS])

    def test_show_network(self):
        network = self._create_network_with_attr(enabled=True)
        req = self.new_show_request('networks', network['id'], as_admin=True)
        shown = self.deserialize(self.fmt,
                                 req.get_response(self.api))['network']
        self.assertIs(True, shown[apidef.METADATA_STATEFUL_IPV6_ADDRESS])

    def test_delete_network_removes_the_row(self):
        network = self._create_network_with_attr(enabled=True)
        req = self.new_delete_request('networks', network['id'],
                                      as_admin=True)
        req.get_response(self.api)
        self.assertIsNone(self._get_db_object(network['id']))

    def _create_external_network(self, **kwargs):
        kwargs.update({'arg_list': (extnet_apidef.EXTERNAL,
                                    ) + kwargs.get('arg_list', ()),
                       extnet_apidef.EXTERNAL: True})
        return self._create_network(self.fmt, 'net', True, as_admin=True,
                                    **kwargs)

    def test_create_enabled_on_external_network_rejected(self):
        res = self._create_external_network(
            arg_list=(apidef.METADATA_STATEFUL_IPV6_ADDRESS, ),
            **{apidef.METADATA_STATEFUL_IPV6_ADDRESS: True})
        self.assertEqual(400, res.status_int)

    def test_create_disabled_on_external_network(self):
        res = self._create_external_network()
        self.assertEqual(201, res.status_int)

    def _update_status(self, network_id, attrs):
        req = self.new_update_request('networks', {'network': attrs},
                                      network_id, as_admin=True)
        return req.get_response(self.api).status_int

    def test_update_enabled_on_external_network_rejected(self):
        network = self.deserialize(
            self.fmt, self._create_external_network())['network']
        status = self._update_status(
            network['id'], {apidef.METADATA_STATEFUL_IPV6_ADDRESS: True})
        self.assertEqual(400, status)
        self.assertIsNone(self._get_db_object(network['id']))

    def test_update_to_external_with_option_enabled_rejected(self):
        network = self._create_network_with_attr(enabled=True)
        status = self._update_status(network['id'],
                                     {extnet_apidef.EXTERNAL: True})
        self.assertEqual(400, status)

    def test_update_to_internal_and_enabled(self):
        network = self.deserialize(
            self.fmt, self._create_external_network())['network']
        status = self._update_status(
            network['id'], {extnet_apidef.EXTERNAL: False,
                            apidef.METADATA_STATEFUL_IPV6_ADDRESS: True})
        self.assertEqual(200, status)
