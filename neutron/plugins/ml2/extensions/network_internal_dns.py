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
from neutron_lib.plugins.ml2 import api
from oslo_log import log as logging

from neutron.objects import network_internal_dns as internal_dns_obj

LOG = logging.getLogger(__name__)


class NetworkInternalDnsExtensionDriver(api.ExtensionDriver):
    _supported_extension_alias = apidef.ALIAS

    @property
    def extension_alias(self):
        return self._supported_extension_alias

    def initialize(self):
        LOG.info("NetworkInternalDnsExtensionDriver initialization complete")

    def process_create_network(self, plugin_context, request_data, db_data):
        # NOTE: networks created by Neutron itself do not go through the API
        # and may not have the attribute. Only a disabled state needs a row.
        enabled = request_data.get(apidef.INTERNAL_DNS_ENABLED) is not False
        if not enabled:
            internal_dns_obj.NetworkInternalDns(
                plugin_context, network_id=db_data['id'],
                enabled=False).create()
        db_data[apidef.INTERNAL_DNS_ENABLED] = enabled

    def process_update_network(self, plugin_context, request_data, db_data):
        if apidef.INTERNAL_DNS_ENABLED not in request_data:
            return

        enabled = request_data[apidef.INTERNAL_DNS_ENABLED]
        network_id = db_data['id']
        internal_dns = internal_dns_obj.NetworkInternalDns.get_object(
            plugin_context, network_id=network_id)
        if internal_dns:
            if internal_dns.enabled != enabled:
                internal_dns.enabled = enabled
                internal_dns.update()
        elif not enabled:
            internal_dns_obj.NetworkInternalDns(
                plugin_context, network_id=network_id,
                enabled=False).create()
        db_data[apidef.INTERNAL_DNS_ENABLED] = enabled

    def extend_network_dict(self, session, db_data, response_data):
        internal_dns = db_data.internal_dns
        response_data[apidef.INTERNAL_DNS_ENABLED] = (
            internal_dns.enabled if internal_dns is not None else True)
        return response_data
