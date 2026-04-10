"""Tests for wildcard serial pool management."""

import unittest
from unittest.mock import Mock, patch

from ser2tcp.pool_manager import PoolManager, PoolValidationError


class _FakeConfigStore:
    def __init__(self, data):
        self.data = data
        self.save = Mock()


class _FakeServer:
    def __init__(self, config):
        self.protocol = 'TCP'
        self.config = config
        self.connections = []


class _FakeProxy:
    def __init__(self, config, _log=None):
        self.config = config
        self.serial_config = config['serial']
        self.servers = [_FakeServer(config['servers'][0])]
        self.is_connected = False
        self.closed = False

    def read_sockets(self):
        return []

    def write_sockets(self):
        return []

    def process_read(self, _read_sockets):
        return None

    def process_write(self, _write_sockets):
        return None

    def process_stale(self):
        return None

    def close(self):
        self.closed = True


class TestPoolManager(unittest.TestCase):
    def _pool_config(self):
        return {
            'name': 'USB devices',
            'enabled': True,
            'serial': {
                'glob': '/dev/serial/by-id/usb-*',
                'baudrate': 115200,
            },
            'server': {
                'address': '0.0.0.0',
                'start_port': 11000,
            },
            'assignments': [],
            'ignored_identities': [],
        }

    def _config_store(self, pools=None, ports=None, http=None):
        return _FakeConfigStore({
            'ports': ports or [],
            'http': http or [],
            'pools': pools or [],
        })

    def test_auto_discovery_assigns_ports_sequentially_and_skips_used_ports(self):
        store = self._config_store(
            pools=[self._pool_config()],
            ports=[{
                'serial': {'port': '/dev/ttyUSB0'},
                'servers': [{
                    'protocol': 'tcp',
                    'address': '0.0.0.0',
                    'port': 11000,
                }],
            }],
            http=[{'address': '127.0.0.1', 'port': 11001}],
        )
        created = []

        def make_proxy(config, log=None):
            created.append(config)
            return _FakeProxy(config, log)

        with patch('ser2tcp.pool_manager._glob.glob',
                return_value=[
                    '/dev/serial/by-id/usb-b',
                    '/dev/serial/by-id/usb-a',
                ]), \
                patch('ser2tcp.pool_manager._serial_proxy.SerialProxy',
                    side_effect=make_proxy):
            manager = PoolManager(store)

        assignments = store.data['pools'][0]['assignments']
        self.assertEqual(
            [item['identity'] for item in assignments],
            ['/dev/serial/by-id/usb-a', '/dev/serial/by-id/usb-b'])
        self.assertEqual(
            [item['port'] for item in assignments],
            [11002, 11003])
        self.assertEqual(
            [item['servers'][0]['port'] for item in created],
            [11002, 11003])

    def test_reconnect_keeps_same_port_for_same_identity(self):
        store = self._config_store(pools=[self._pool_config()])
        created = []

        def make_proxy(config, log=None):
            created.append(config)
            return _FakeProxy(config, log)

        with patch('ser2tcp.pool_manager._glob.glob',
                return_value=['/dev/serial/by-id/usb-a']) as mock_glob, \
                patch('ser2tcp.pool_manager._serial_proxy.SerialProxy',
                    side_effect=make_proxy):
            manager = PoolManager(store)
            port = store.data['pools'][0]['assignments'][0]['port']
            mock_glob.return_value = []
            manager.scan(force=True)
            self.assertFalse(manager.status()[0]['assignments'][0]['running'])
            mock_glob.return_value = ['/dev/serial/by-id/usb-a']
            manager.scan(force=True)

        assignment = store.data['pools'][0]['assignments'][0]
        self.assertEqual(assignment['port'], port)
        self.assertTrue(manager.status()[0]['assignments'][0]['running'])
        self.assertEqual(len(created), 2)

    def test_remove_assignment_suppresses_rediscovery(self):
        store = self._config_store(pools=[self._pool_config()])
        with patch('ser2tcp.pool_manager._glob.glob',
                return_value=['/dev/serial/by-id/usb-a']), \
                patch('ser2tcp.pool_manager._serial_proxy.SerialProxy',
                    side_effect=_FakeProxy):
            manager = PoolManager(store)
            manager.delete_assignment(0, 0)
            manager.scan(force=True)

        pool = store.data['pools'][0]
        self.assertEqual(pool['assignments'], [])
        self.assertEqual(pool['ignored_identities'],
            ['/dev/serial/by-id/usb-a'])

    def test_disable_assignment_and_pool_preserve_mappings(self):
        store = self._config_store(pools=[self._pool_config()])
        with patch('ser2tcp.pool_manager._glob.glob',
                return_value=[
                    '/dev/serial/by-id/usb-a',
                    '/dev/serial/by-id/usb-b',
                ]), \
                patch('ser2tcp.pool_manager._serial_proxy.SerialProxy',
                    side_effect=_FakeProxy):
            manager = PoolManager(store)
            manager.set_assignment_enabled(0, 0, False)
            status = manager.status()[0]['assignments']
            self.assertFalse(status[0]['enabled'])
            self.assertFalse(status[0]['running'])
            self.assertTrue(status[1]['running'])

            manager.set_pool_enabled(0, False)

        pool_status = manager.status()[0]
        self.assertFalse(pool_status['enabled'])
        self.assertTrue(all(not item['running']
            for item in pool_status['assignments']))
        self.assertEqual(len(store.data['pools'][0]['assignments']), 2)

    def test_add_assignment_clears_ignored_identity_and_validates_glob(self):
        pool = self._pool_config()
        pool['ignored_identities'] = ['/dev/serial/by-id/usb-a']
        store = self._config_store(pools=[pool])
        with patch('ser2tcp.pool_manager._glob.glob',
                return_value=['/dev/serial/by-id/usb-a']), \
                patch('ser2tcp.pool_manager._serial_proxy.SerialProxy',
                    side_effect=_FakeProxy):
            manager = PoolManager(store)
            self.assertEqual(store.data['pools'][0]['assignments'], [])
            manager.add_assignment(0, '/dev/serial/by-id/usb-a', name='A')
            with self.assertRaises(PoolValidationError):
                manager.add_assignment(0, '/dev/cu.usbmodem141401')

        assignment = store.data['pools'][0]['assignments'][0]
        self.assertEqual(assignment['identity'], '/dev/serial/by-id/usb-a')
        self.assertEqual(assignment['name'], 'A')
        self.assertEqual(store.data['pools'][0]['ignored_identities'], [])
