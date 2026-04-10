"""Tests for YAML config management."""

import tempfile
import unittest
from unittest.mock import Mock

import yaml

from ser2tcp import config_manager


class TestConfigManager(unittest.TestCase):
    def test_load_yaml_defaults_ports_and_pools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f'{tmpdir}/config.yaml'
            with open(path, 'w', encoding='utf-8') as config_file:
                config_file.write("http:\n  - address: 127.0.0.1\n    port: 20080\n")
            store = config_manager.load_config(path)
        self.assertEqual(store.data['ports'], [])
        self.assertEqual(store.data['pools'], [])
        self.assertEqual(store.data['http'][0]['port'], 20080)

    def test_create_default_config_writes_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f'{tmpdir}/config.yaml'
            with unittest.mock.patch.object(
                    config_manager, 'find_free_port', return_value=23456):
                store = config_manager.create_default_config(path, Mock())
            with open(path, 'r', encoding='utf-8') as config_file:
                saved = yaml.safe_load(config_file)
        self.assertEqual(store.data, saved)
        self.assertEqual(saved['http'][0]['port'], 23456)
        self.assertEqual(saved['ports'], [])
        self.assertEqual(saved['pools'], [])

    def test_rejects_json_config_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f'{tmpdir}/config.json'
            with self.assertRaises(config_manager.ConfigError):
                config_manager.create_default_config(path, Mock())
            with open(path, 'w', encoding='utf-8') as config_file:
                config_file.write('{}')
            with self.assertRaises(config_manager.ConfigError):
                config_manager.load_config(path)

    def test_rejects_root_list_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f'{tmpdir}/config.yaml'
            with open(path, 'w', encoding='utf-8') as config_file:
                config_file.write("- serial:\n    port: /dev/ttyUSB0\n")
            with self.assertRaises(config_manager.ConfigError):
                config_manager.load_config(path)

    def test_save_round_trip_preserves_yaml_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f'{tmpdir}/config.yml'
            store = config_manager.ConfigStore(path, {
                'http': [{'address': '127.0.0.1', 'port': 20080}],
                'ports': [{
                    'serial': {'port': '/dev/ttyUSB0'},
                    'servers': [{
                        'protocol': 'tcp',
                        'address': '0.0.0.0',
                        'port': 10001,
                    }],
                }],
                'pools': [{
                    'name': 'usb',
                    'enabled': True,
                    'serial': {'glob': '/dev/serial/by-id/usb-*'},
                    'server': {'address': '0.0.0.0', 'start_port': 11000},
                    'assignments': [],
                    'ignored_identities': [],
                }],
            })
            store.save()
            loaded = config_manager.load_config(path)
        self.assertEqual(loaded.data, store.data)
