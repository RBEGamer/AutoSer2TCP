"""Dynamic wildcard serial pool management."""

import copy as _copy
import fnmatch as _fnmatch
import glob as _glob
import logging as _logging
import os as _os
import time as _time

import ser2tcp.serial_proxy as _serial_proxy


class PoolValidationError(Exception):
    """Pool config or operation validation error."""


class PoolRuntime():
    """Runtime for one wildcard serial pool."""

    def __init__(self, manager, config, index, log=None):
        self._manager = manager
        self._config = config
        self._index = index
        self._log = log if log else _logging.getLogger(__name__)
        self._active = {}
        self._errors = {}
        self._matches = []

    @property
    def config(self):
        """Return pool config."""
        return self._config

    @property
    def matches(self):
        """Return current matched identities."""
        return self._matches

    def close(self):
        """Stop all active assignment proxies."""
        for identity in list(self._active):
            self._stop_assignment(identity)

    def read_sockets(self):
        """Collect sockets from active assignment proxies."""
        sockets = []
        for proxy in self._active.values():
            sockets.extend(proxy.read_sockets())
        return sockets

    def write_sockets(self):
        """Collect writable sockets from active assignment proxies."""
        sockets = []
        for proxy in self._active.values():
            sockets.extend(proxy.write_sockets())
        return sockets

    def process_read(self, read_sockets):
        """Delegate read processing to active proxies."""
        for proxy in list(self._active.values()):
            proxy.process_read(read_sockets)

    def process_write(self, write_sockets):
        """Delegate write processing to active proxies."""
        for proxy in list(self._active.values()):
            proxy.process_write(write_sockets)

    def process_stale(self):
        """Delegate stale processing to active proxies."""
        for proxy in list(self._active.values()):
            proxy.process_stale()

    def scan(self):
        """Refresh current matches and active assignments."""
        self._matches = self._scan_matches()
        assignments = self._config.setdefault('assignments', [])
        ignored = set(self._config.setdefault('ignored_identities', []))
        assigned_ids = {assignment['identity'] for assignment in assignments}

        for identity in self._matches:
            if identity in assigned_ids or identity in ignored:
                continue
            assignments.append({
                'identity': identity,
                'port': self._allocate_port(),
                'enabled': True,
            })
            self._manager.save_config()
            assigned_ids.add(identity)

        enabled = bool(self._config.get('enabled', True))
        current_matches = set(self._matches)
        valid_identities = set()
        for assignment in assignments:
            identity = assignment.get('identity')
            if not identity:
                continue
            valid_identities.add(identity)
            if not enabled or not assignment.get('enabled', True):
                self._stop_assignment(identity)
                continue
            if identity not in current_matches:
                self._stop_assignment(identity)
                self._errors.pop(identity, None)
                continue
            self._ensure_assignment_running(assignment)

        for identity in list(self._active):
            if identity not in valid_identities:
                self._stop_assignment(identity)

    def status(self):
        """Return pool runtime status for HTTP API."""
        assignments = []
        matches = set(self._matches)
        for assignment in self._config.get('assignments', []):
            identity = assignment.get('identity')
            proxy = self._active.get(identity)
            server = proxy.servers[0] if proxy and proxy.servers else None
            connections = []
            if server:
                connections = [
                    {'address': con.address_str()}
                    for con in server.connections
                ]
            assignments.append({
                'identity': identity,
                'name': assignment.get('name'),
                'enabled': bool(assignment.get('enabled', True)),
                'port': assignment.get('port'),
                'present': identity in matches,
                'running': proxy is not None,
                'connected': proxy.is_connected if proxy else False,
                'connections': connections,
                'error': self._errors.get(identity),
            })
        serial_cfg = _copy.deepcopy(self._config.get('serial', {}))
        server_cfg = _copy.deepcopy(self._config.get('server', {}))
        return {
            'name': self._config.get('name'),
            'enabled': bool(self._config.get('enabled', True)),
            'serial': serial_cfg,
            'server': server_cfg,
            'matches': list(self._matches),
            'assignments': assignments,
        }

    def active_terminal_targets(self):
        """Return active assignment proxies for web terminal tunneling."""
        targets = []
        assignments = self._config.get('assignments', [])
        for index, assignment in enumerate(assignments):
            identity = assignment.get('identity')
            proxy = self._active.get(identity)
            if proxy is None:
                continue
            targets.append({
                'pool_index': self._index,
                'assignment_index': index,
                'identity': identity,
                'name': assignment.get('name'),
                'pool_name': self._config.get('name'),
                'port': assignment.get('port'),
                'proxy': proxy,
            })
        return targets

    def add_assignment(self, identity, name=None, enabled=True):
        """Add explicit assignment."""
        if not _fnmatch.fnmatch(identity, self._glob_pattern()):
            raise PoolValidationError('Assignment identity does not match pool glob')
        assignments = self._config.setdefault('assignments', [])
        if any(item.get('identity') == identity for item in assignments):
            raise PoolValidationError('Assignment already exists')
        assignment = {
            'identity': identity,
            'port': self._allocate_port(),
            'enabled': bool(enabled),
        }
        if name:
            assignment['name'] = name
        assignments.append(assignment)
        ignored = self._config.setdefault('ignored_identities', [])
        if identity in ignored:
            ignored.remove(identity)
        self._manager.save_config()
        self.scan()
        return len(assignments) - 1

    def set_enabled(self, enabled):
        """Enable or disable entire pool."""
        self._config['enabled'] = bool(enabled)
        self._manager.save_config()
        self.scan()

    def set_assignment_enabled(self, assignment_index, enabled):
        """Enable or disable one assignment."""
        assignments = self._config.setdefault('assignments', [])
        if assignment_index < 0 or assignment_index >= len(assignments):
            raise PoolValidationError('Assignment not found')
        assignments[assignment_index]['enabled'] = bool(enabled)
        self._manager.save_config()
        self.scan()

    def remove_assignment(self, assignment_index):
        """Remove assignment and suppress rediscovery."""
        assignments = self._config.setdefault('assignments', [])
        if assignment_index < 0 or assignment_index >= len(assignments):
            raise PoolValidationError('Assignment not found')
        assignment = assignments.pop(assignment_index)
        identity = assignment.get('identity')
        if identity:
            self._stop_assignment(identity)
            ignored = self._config.setdefault('ignored_identities', [])
            if identity not in ignored:
                ignored.append(identity)
        self._manager.save_config()

    def update_config(self, new_config):
        """Replace editable config fields, preserving runtime metadata."""
        new_config['assignments'] = _copy.deepcopy(
            self._config.get('assignments', []))
        new_config['ignored_identities'] = _copy.deepcopy(
            self._config.get('ignored_identities', []))
        self.close()
        self._errors.clear()
        self._config.clear()
        self._config.update(new_config)
        self._manager.save_config()
        self.scan()

    def _scan_matches(self):
        """Return sorted unique matches for pool glob."""
        matches = _glob.glob(self._glob_pattern())
        return sorted(dict.fromkeys(matches))

    def _glob_pattern(self):
        """Return serial glob pattern."""
        return self._config.get('serial', {}).get('glob', '')

    def _stop_assignment(self, identity):
        """Stop runtime proxy for assignment identity."""
        proxy = self._active.pop(identity, None)
        if proxy:
            proxy.close()

    def _ensure_assignment_running(self, assignment):
        """Create runtime proxy when needed."""
        identity = assignment['identity']
        if identity in self._active:
            return
        try:
            proxy = _serial_proxy.SerialProxy(
                self._build_proxy_config(assignment), self._log)
        except Exception as err:  # pragma: no cover - exercised in tests via mocks
            self._errors[identity] = str(err)
            return
        self._active[identity] = proxy
        self._errors.pop(identity, None)

    def _build_proxy_config(self, assignment):
        """Build SerialProxy config for one assignment."""
        serial_cfg = _copy.deepcopy(self._config.get('serial', {}))
        serial_cfg.pop('glob', None)
        serial_cfg['port'] = assignment['identity']
        server_template = self._config.get('server', {})
        server_cfg = {
            'protocol': 'tcp',
            'address': server_template.get('address', '0.0.0.0'),
            'port': assignment['port'],
        }
        for key in ('send_timeout', 'buffer_limit', 'max_connections'):
            if key in server_template:
                server_cfg[key] = server_template[key]
        proxy_cfg = {
            'serial': serial_cfg,
            'servers': [server_cfg],
        }
        if assignment.get('name'):
            proxy_cfg['name'] = assignment['name']
        elif self._config.get('name'):
            proxy_cfg['name'] = assignment['identity']
        return proxy_cfg

    def _allocate_port(self):
        """Allocate next free port for a new assignment."""
        start_port = int(self._config.get('server', {}).get('start_port', 10001))
        used_ports = self._manager.used_ports()
        port = start_port
        while port in used_ports:
            port += 1
        return port


class PoolManager():
    """Manage wildcard serial pools and their runtime assignments."""

    def __init__(self, config_store, log=None, scan_interval=2.0):
        self._config_store = config_store
        self._configuration = config_store.data
        self._log = log if log else _logging.getLogger(__name__)
        self._scan_interval = scan_interval
        self._last_scan = 0
        self._pools = []
        self._rebuild()

    def _rebuild(self):
        """Rebuild runtime pool list from config."""
        for pool in self._pools:
            pool.close()
        self._pools = []
        for index, config in enumerate(self._configuration.get('pools', [])):
            self._pools.append(PoolRuntime(self, config, index, self._log))
        self.scan(force=True)

    def save_config(self):
        """Persist config store."""
        self._config_store.save()

    def read_sockets(self):
        """Return sockets for active assignment proxies."""
        sockets = []
        for pool in self._pools:
            sockets.extend(pool.read_sockets())
        return sockets

    def write_sockets(self):
        """Return writable sockets for active assignment proxies."""
        sockets = []
        for pool in self._pools:
            sockets.extend(pool.write_sockets())
        return sockets

    def process_read(self, read_sockets):
        """Delegate read events."""
        for pool in self._pools:
            pool.process_read(read_sockets)

    def process_write(self, write_sockets):
        """Delegate write events."""
        for pool in self._pools:
            pool.process_write(write_sockets)

    def process_stale(self):
        """Delegate stale events and periodically rescan globs."""
        for pool in self._pools:
            pool.process_stale()
        self.scan()

    def close(self):
        """Close all runtime pools."""
        for pool in self._pools:
            pool.close()

    def scan(self, force=False):
        """Scan pool globs when interval expires."""
        now = _time.time()
        if not force and now - self._last_scan < self._scan_interval:
            return
        self._last_scan = now
        for pool in self._pools:
            pool.scan()

    def status(self):
        """Return status payload for all pools."""
        return [pool.status() for pool in self._pools]

    def used_ports(self):
        """Return all configured TCP ports across static ports, pools, and HTTP."""
        used = set()
        for port_cfg in self._configuration.get('ports', []):
            for server in port_cfg.get('servers', []):
                protocol = server.get('protocol', '').upper()
                if protocol in ('TCP', 'TELNET', 'SSL') and 'port' in server:
                    used.add(server['port'])
        http_cfg = self._configuration.get('http', [])
        if isinstance(http_cfg, dict):
            http_cfg = [http_cfg]
        for server in http_cfg:
            if 'port' in server:
                used.add(server['port'])
        for pool_cfg in self._configuration.get('pools', []):
            for assignment in pool_cfg.get('assignments', []):
                if 'port' in assignment:
                    used.add(assignment['port'])
        return used

    def add_pool(self, config):
        """Add new pool config."""
        pools = self._configuration.setdefault('pools', [])
        config = _copy.deepcopy(config)
        config.setdefault('enabled', True)
        config.setdefault('assignments', [])
        config.setdefault('ignored_identities', [])
        pools.append(config)
        self.save_config()
        self._rebuild()
        return len(pools) - 1

    def update_pool(self, index, config):
        """Update existing pool config."""
        pools = self._configuration.setdefault('pools', [])
        if index < 0 or index >= len(pools):
            raise PoolValidationError('Pool not found')
        self._pools[index].update_config(_copy.deepcopy(config))

    def delete_pool(self, index):
        """Delete one pool."""
        pools = self._configuration.setdefault('pools', [])
        if index < 0 or index >= len(pools):
            raise PoolValidationError('Pool not found')
        self._pools[index].close()
        del self._pools[index]
        del pools[index]
        self.save_config()
        self._rebuild()

    def set_pool_enabled(self, index, enabled):
        """Start or stop one pool."""
        if index < 0 or index >= len(self._pools):
            raise PoolValidationError('Pool not found')
        self._pools[index].set_enabled(enabled)

    def add_assignment(self, pool_index, identity, name=None, enabled=True):
        """Add explicit assignment to pool."""
        if pool_index < 0 or pool_index >= len(self._pools):
            raise PoolValidationError('Pool not found')
        return self._pools[pool_index].add_assignment(
            identity, name=name, enabled=enabled)

    def set_assignment_enabled(self, pool_index, assignment_index, enabled):
        """Enable or disable one assignment."""
        if pool_index < 0 or pool_index >= len(self._pools):
            raise PoolValidationError('Pool not found')
        self._pools[pool_index].set_assignment_enabled(
            assignment_index, enabled)

    def delete_assignment(self, pool_index, assignment_index):
        """Remove assignment from pool."""
        if pool_index < 0 or pool_index >= len(self._pools):
            raise PoolValidationError('Pool not found')
        self._pools[pool_index].remove_assignment(assignment_index)

    def terminal_targets(self):
        """Return active assignment proxies for the web terminal."""
        targets = []
        for pool in self._pools:
            targets.extend(pool.active_terminal_targets())
        return targets
