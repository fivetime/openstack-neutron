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

"""One haproxy per node that serves every metadata namespace of the node.

HAProxy opens one listening socket in each namespace, and a single frontend
takes the network ID from the name of the socket that accepted the connection.
HAProxy cannot add a socket at runtime, so a change of networks is a reload.
"""

import grp
import json
import os
import pwd
import signal
import threading
import time

from neutron_lib import exceptions
from oslo_log import log as logging
from oslo_utils import fileutils
from oslo_utils import uuidutils

from neutron.agent.linux import external_process
from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils as linux_utils
from neutron.agent.ovn.extensions.metadata import driver as metadata_driver


LOG = logging.getLogger(__name__)

UUID = 'shared-ovn-metadata'
SERVICE = 'haproxy'
THREADS = 4
# A reload is batched: it runs after this quiet time, or this maximum wait.
QUIET_TIME = 1.0
MAX_WAIT = 10.0
# How often the process is checked, and a failed apply retried. A dead process
# stops metadata for every network of the node, so this does not use the
# ProcessMonitor, whose default interval is 60 seconds.
CHECK_INTERVAL = 2.0

GLOBAL_TEMPLATE = """
global
    log         /dev/log local0 %(log_level)s
    log-tag     %(log_tag)s
    user        %(user)s
    group       %(group)s
    maxconn     %(maxconn)s
    nbthread    %(threads)s
    pidfile     %(pidfile)s
    stats socket %(stats_socket)s mode 600 expose-fd listeners level admin
    daemon

defaults
    log global
    mode http
    option httplog
    option dontlognull
    option http-server-close
    option forwardfor
    retries                 3
    timeout http-request    %(timeout_http_request)ss
    timeout connect         %(timeout_connect)ss
    timeout client          %(timeout_client)ss
    timeout server          %(timeout_server)ss
    timeout http-keep-alive %(timeout_http_keep_alive)ss
"""

# CIDRs overlap between networks, so the limit is keyed on the network too.
RATE_LIMIT_TABLES_TEMPLATE = """
backend base_rate_limiter
    stick-table type string len 96 size 200k expire %(expire)ss store http_req_rate(%(base_window)ss)

backend burst_rate_limiter
    stick-table type string len 96 size 200k expire %(expire)ss store http_req_rate(%(burst_window)ss)
"""  # noqa: E501 line-length

RATE_LIMIT_RULES_TEMPLATE = """
    http-request set-var-fmt(txn.limit_key) "%%[so_name]-%%[src]"
    http-request track-sc0 var(txn.limit_key) table base_rate_limiter
    http-request track-sc1 var(txn.limit_key) table burst_rate_limiter
    http-request deny deny_status 429 if { sc_http_req_rate(0,base_rate_limiter) gt %(base_limit)s }
    http-request deny deny_status 429 if { sc_http_req_rate(1,burst_rate_limiter) gt %(burst_limit)s }
"""  # noqa: E501 line-length


class SharedMetadataProxy:

    def __init__(self, conf):
        self.conf = conf
        self._networks = {}
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._apply_lock = threading.Lock()
        self._thread = None
        self._retry = False
        cfg_dir = metadata_driver.HaproxyConfigurator.get_config_path(
            conf.state_path)
        self._cfg_dir = cfg_dir
        self.cfg_path = os.path.join(cfg_dir, '%s.conf' % UUID)
        self.stats_socket = os.path.join(cfg_dir, '%s.sock' % UUID)
        self._state_path = os.path.join(cfg_dir, '%s.json' % UUID)
        self._pm = self.process_manager(conf, self._start_command)
        self._load_state()

    @staticmethod
    def process_manager(conf, callback=None):
        return external_process.ProcessManager(
            conf=conf, uuid=UUID, service=SERVICE, run_as_root=True,
            default_cmd_callback=callback)

    @classmethod
    def stop_leftover(cls, conf):
        """Stop a shared process after the option was turned off."""
        pm = cls.process_manager(conf)
        if pm.active:
            LOG.info('Stopping the shared metadata proxy, the option is off')
            pm.disable(sig=str(int(signal.SIGTERM)))

    def _load_state(self):
        """Keep serving through an agent restart.

        The process outlives the agent, and the agent provisions its networks
        one by one after it starts. Without this, the first batched reload
        would drop every network not provisioned yet.
        """
        if not self._pm.active:
            return
        try:
            with open(self._state_path) as f:
                self._networks = {net: tuple(entry)
                                  for net, entry in json.load(f).items()}
        except (OSError, ValueError, AttributeError, TypeError) as e:
            LOG.warning('Cannot read %s: %s', self._state_path, e)

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='shared-metadata-proxy')
        self._thread.start()

    def stop_thread(self):
        self._stop.set()
        self._dirty.set()

    def add(self, network_id, namespace, bind_address, port,
            bind_address_v6=None, bind_interface=None):
        """Serve a network. It happens in the next batched reload."""
        if not uuidutils.is_uuid_like(network_id):
            raise exceptions.InvalidInput(
                error_message='Invalid network ID: %s' % network_id)
        entry = (namespace, bind_address, port, bind_address_v6,
                 bind_interface)
        if self._networks.get(network_id) != entry:
            self._networks[network_id] = entry
            self._dirty.set()

    def remove(self, network_id):
        """Stop serving a network, before this returns.

        A reload takes the sockets of the old process by address and namespace
        name. If the old process still held a socket of a deleted namespace, a
        namespace created with the same name would inherit that dead socket.
        """
        if self._networks.pop(network_id, None) is not None:
            self._apply()

    def retain(self, network_ids):
        """Stop serving every network that is not in network_ids."""
        unknown = set(self._networks) - set(network_ids)
        for net in unknown:
            self._networks.pop(net, None)
        if unknown:
            self._apply()

    def _run(self):
        while not self._stop.is_set():
            if self._dirty.wait(CHECK_INTERVAL):
                self._batch()
                if self._stop.is_set():
                    return
                self._dirty.clear()
            elif not self._retry and (self._pm.active or not self._networks):
                continue
            self._retry = not self._apply()
            if self._retry:
                self._stop.wait(CHECK_INTERVAL)

    def _batch(self):
        deadline = time.monotonic() + MAX_WAIT
        known = None
        while time.monotonic() < deadline and not self._stop.is_set():
            current = dict(self._networks)
            if current == known:
                return
            known = current
            self._stop.wait(QUIET_TIME)

    def _apply(self):
        """Make the process match the networks. Returns False on a failure.

        It never raises: the thread that calls it keeps the process alive.
        """
        with self._apply_lock:
            networks = dict(self._networks)
            try:
                return self._apply_networks(networks)
            except Exception as e:
                LOG.warning('Applying %d networks failed, checking them: %s',
                            len(networks), e)
            usable = {net: entry for net, entry in networks.items()
                      if not self._is_broken(net, entry)}
            # An empty set here must not stop what still serves the others.
            if not usable or len(usable) == len(networks):
                LOG.error('The shared metadata proxy did not apply its '
                          'configuration, and no network is at fault')
                return False
            try:
                self._apply_networks(usable)
            except Exception as e:
                LOG.error('The shared metadata proxy did not apply its '
                          'configuration: %s', e)
            # Retried: the networks left out must come back when they recover.
            return False

    @staticmethod
    def _is_broken(network_id, entry):
        """True only if a socket of this network certainly cannot bind."""
        namespace, bind_address, _port, _v6, interface = entry
        try:
            if not ip_lib.network_namespace_exists(namespace):
                LOG.error('%s: namespace %s is gone, not serving metadata',
                          network_id, namespace)
                return True
            device = ip_lib.IPDevice(interface, namespace=namespace)
            addresses = [a['cidr'].split('/')[0] for a in device.addr.list()]
        except Exception as e:
            LOG.warning('%s: cannot check namespace %s: %s',
                        network_id, namespace, e)
            return False
        if bind_address not in addresses:
            LOG.error('%s: %s is not in namespace %s, not serving metadata',
                      network_id, bind_address, namespace)
            return True
        return False

    def _apply_networks(self, networks):
        active = self._pm.active
        if not networks:
            if active:
                self._pm.disable(sig=str(int(signal.SIGTERM)))
            return True
        fileutils.ensure_tree(self._cfg_dir, mode=0o755)
        self._write_state(networks)
        config = self.render(networks, self._pm.get_pid_file_name())
        if active and config == self._read_config():
            return True
        self._write_config(config)
        if active:
            started = time.monotonic()
            self._reload()
            LOG.info('Shared metadata proxy reloaded: %d networks, %.1f s',
                     len(networks), time.monotonic() - started)
        else:
            LOG.info('Starting the shared metadata proxy: %d networks',
                     len(networks))
            self._pm.enable()
        return True

    def _start_command(self, pid_file):
        return [SERVICE, '-f', self.cfg_path]

    def _reload(self):
        cmd = [SERVICE, '-f', self.cfg_path, '-x', self.stats_socket,
               '-sf', str(self._pm.pid)]
        ip_lib.IPWrapper().netns.execute(
            cmd, addl_env=self._pm.cmd_addl_env, run_as_root=True)

    def _read_config(self):
        return linux_utils.read_if_exists(self.cfg_path, run_as_root=True)

    def _write_config(self, config):
        temp = self.cfg_path + '.new'
        with open(temp, 'w') as f:
            f.write(config)
        os.replace(temp, self.cfg_path)

    def _write_state(self, networks):
        temp = self._state_path + '.new'
        with open(temp, 'w') as f:
            json.dump(networks, f)
        os.replace(temp, self._state_path)

    def _names(self):
        user = self.conf.metadata_proxy_user or str(os.geteuid())
        group = self.conf.metadata_proxy_group or str(os.getegid())
        try:
            user = pwd.getpwuid(int(user)).pw_name
        except ValueError:
            pass
        try:
            group = grp.getgrgid(int(group)).gr_name
        except ValueError:
            pass
        return user, group

    def render(self, networks, pid_file):
        conf = self.conf
        proxy = conf.metadata_haproxy
        user, group = self._names()
        info = {
            'log_level': 'info', 'log_tag': 'haproxy-metadata-proxy-%s' % UUID,
            'user': user, 'group': group, 'pidfile': pid_file,
            'maxconn': proxy.shared_proxy_maxconn, 'threads': THREADS,
            'stats_socket': self.stats_socket,
            'timeout_http_request': proxy.timeout_http_request,
            'timeout_connect': proxy.timeout_connect,
            'timeout_client': proxy.timeout_client,
            'timeout_server': proxy.timeout_server,
            'timeout_http_keep_alive': proxy.timeout_http_keep_alive,
        }
        parts = [GLOBAL_TEMPLATE % info]
        limit = conf.metadata_rate_limiting
        if limit.rate_limit_enabled:
            parts.append(RATE_LIMIT_TABLES_TEMPLATE % {
                'expire': max(limit.base_window_duration,
                              limit.burst_window_duration),
                'base_window': limit.base_window_duration,
                'burst_window': limit.burst_window_duration})
        parts.append('\nfrontend metadata\n'
                     '    maxconn %s\n'
                     '    default_backend metadata_server\n'
                     '    http-request del-header X-OVN-Router-ID\n'
                     '    http-request set-header X-OVN-Network-ID '
                     '%%[so_name]\n' % proxy.shared_proxy_maxconn)
        if limit.rate_limit_enabled:
            parts.append(RATE_LIMIT_RULES_TEMPLATE % {
                'base_limit': limit.base_query_rate_limit,
                'burst_limit': limit.burst_query_rate_limit})
        per_network = proxy.shared_proxy_network_maxconn
        for net in sorted(networks):
            namespace, address, port, address_v6, interface = networks[net]
            parts.append(f'    bind {address}:{port} namespace {namespace} '
                         f'name {net} maxconn {per_network}\n')
            if address_v6 and interface:
                parts.append(f'    bind [{address_v6}]:{port} '
                             f'interface {interface} namespace {namespace} '
                             f'name {net} maxconn {per_network}\n')
        parts.append('\nbackend metadata_server\n'
                     '    server metadata %s\n' % conf.metadata_proxy_socket)
        return ''.join(parts)
