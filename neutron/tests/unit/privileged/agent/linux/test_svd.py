# Copyright 2026 OpenStack Foundation
#
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

from unittest import mock

from neutron.agent.linux import nl_constants as nl_const
from neutron import privileged
from neutron.privileged.agent.linux import ip_lib as priv_ip_lib
from neutron.privileged.agent.linux import svd as priv_svd
from neutron.tests import base


class TestDelVni(base.BaseTestCase):

    BR, VX = 'brevpn-0', 'vxlanevpn-0'
    IDX = {'brevpn-0': 4, 'vxlanevpn-0': 3, 'vl-0-1': 6, 'lo-0-1': 7}

    def setUp(self):
        super().setUp()
        self.addCleanup(privileged.default.set_client_mode, True)
        privileged.default.set_client_mode(False)
        self.ipr = mock.Mock()
        self.ipr.link_lookup.side_effect = (
            lambda ifname: [self.IDX[ifname]] if ifname in self.IDX else [])
        get_iproute = mock.patch.object(priv_ip_lib, 'get_iproute').start()
        get_iproute.return_value.__enter__.return_value = self.ipr

    def test_del_vni_removes_tunnel_mapping_before_vlan(self):
        # FRR's zebra only learns that a VLAN-to-VNI mapping is gone when the
        # kernel reports the VXLAN port's VLAN without it, which needs the
        # mapping removed in a message of its own while the VLAN still exists.
        priv_svd.del_vni(self.BR, self.VX, 'vl-0-1', 'lo-0-1', 50000, 1)

        vx_calls = [c for c in self.ipr.vlan_filter.call_args_list
                    if c.kwargs['index'] == self.IDX[self.VX]]
        self.assertEqual(
            [mock.call(nl_const.IP_LINK_DEL, index=self.IDX[self.VX],
                       vlan_tunnel_info={'vid': 1, 'id': 50000}),
             mock.call(nl_const.IP_LINK_DEL, index=self.IDX[self.VX],
                       vlan_info={'vid': 1})],
            vx_calls)
        for call in vx_calls:
            self.assertFalse(
                'vlan_info' in call.kwargs and
                'vlan_tunnel_info' in call.kwargs,
                'VLAN and its tunnel mapping deleted in one message')

    def test_del_vni_removes_bridge_vlan_last(self):
        priv_svd.del_vni(self.BR, self.VX, 'vl-0-1', 'lo-0-1', 50000, 1)

        self.assertEqual(
            mock.call(nl_const.IP_LINK_DEL, index=self.IDX[self.BR],
                      vlan_info={'vid': 1}, vlan_flags='self'),
            self.ipr.vlan_filter.call_args_list[-1])
