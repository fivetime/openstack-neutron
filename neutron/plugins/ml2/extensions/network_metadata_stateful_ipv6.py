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
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins.ml2 import api
from oslo_log import log as logging

from neutron._i18n import _
from neutron.objects import network as network_obj
from neutron.objects import network_metadata_stateful_ipv6 as md_ipv6_obj

LOG = logging.getLogger(__name__)


class NetworkMetadataStatefulIpv6ExtensionDriver(api.ExtensionDriver):
    _supported_extension_alias = apidef.ALIAS

    @property
    def extension_alias(self):
        return self._supported_extension_alias

    def initialize(self):
        LOG.info("NetworkMetadataStatefulIpv6ExtensionDriver initialization "
                 "complete")

    @staticmethod
    def _raise_external_network():
        msg = _("%s cannot be enabled on an external network") % (
            apidef.METADATA_STATEFUL_IPV6_ADDRESS)
        raise n_exc.BadRequest(resource='network', msg=msg)

    def process_create_network(self, plugin_context, request_data, db_data):
        # NOTE: networks created by Neutron itself do not go through the API
        # and may not have the attribute. Only an enabled state needs a row.
        enabled = request_data.get(
            apidef.METADATA_STATEFUL_IPV6_ADDRESS) is True
        if enabled:
            if request_data.get(extnet_apidef.EXTERNAL) is True:
                self._raise_external_network()
            md_ipv6_obj.NetworkMetadataStatefulIpv6(
                plugin_context, network_id=db_data['id'],
                enabled=True).create()
        db_data[apidef.METADATA_STATEFUL_IPV6_ADDRESS] = enabled

    def process_update_network(self, plugin_context, request_data, db_data):
        network_id = db_data['id']
        stateful_ipv6 = md_ipv6_obj.NetworkMetadataStatefulIpv6.get_object(
            plugin_context, network_id=network_id)
        current = stateful_ipv6.enabled if stateful_ipv6 else False
        enabled = request_data.get(
            apidef.METADATA_STATEFUL_IPV6_ADDRESS, current)

        if enabled:
            external = request_data.get(extnet_apidef.EXTERNAL)
            if external is True or (
                    external is None and
                    network_obj.ExternalNetwork.objects_exist(
                        plugin_context, network_id=network_id)):
                self._raise_external_network()

        if apidef.METADATA_STATEFUL_IPV6_ADDRESS not in request_data:
            return

        if stateful_ipv6:
            if stateful_ipv6.enabled != enabled:
                stateful_ipv6.enabled = enabled
                stateful_ipv6.update()
        elif enabled:
            md_ipv6_obj.NetworkMetadataStatefulIpv6(
                plugin_context, network_id=network_id,
                enabled=True).create()
        db_data[apidef.METADATA_STATEFUL_IPV6_ADDRESS] = enabled

    def extend_network_dict(self, session, db_data, response_data):
        stateful_ipv6 = db_data.metadata_stateful_ipv6
        response_data[apidef.METADATA_STATEFUL_IPV6_ADDRESS] = (
            stateful_ipv6.enabled if stateful_ipv6 is not None else False)
        return response_data
