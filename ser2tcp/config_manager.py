"""YAML configuration helpers for ser2tcp."""

import os as _os
import socket as _socket

import yaml as _yaml


DEFAULT_CONFIG_DIR = _os.path.expanduser("~/.config/ser2tcp")
DEFAULT_CONFIG_PATH = _os.path.join(DEFAULT_CONFIG_DIR, "config.yaml")


class ConfigError(Exception):
    """Configuration loading or saving error."""


def is_yaml_path(path):
    """Return True when path uses a supported YAML extension."""
    return path.lower().endswith(('.yaml', '.yml'))


def find_free_port(start_port=20080, max_attempts=100):
    """Find first available port starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    return None


def _normalize_configuration(configuration):
    """Normalize root configuration mapping."""
    if configuration is None:
        configuration = {}
    if not isinstance(configuration, dict):
        raise ConfigError("Invalid configuration format: expected YAML mapping")
    configuration.setdefault('ports', [])
    configuration.setdefault('pools', [])
    return configuration


class ConfigStore():
    """Mutable YAML-backed configuration."""

    def __init__(self, path, configuration):
        self.path = path
        self.data = _normalize_configuration(configuration)

    def save(self):
        """Persist configuration as YAML."""
        config_dir = _os.path.dirname(self.path)
        if config_dir and not _os.path.exists(config_dir):
            _os.makedirs(config_dir)
        with open(self.path, 'w', encoding='utf-8') as config_file:
            _yaml.safe_dump(
                self.data, config_file, sort_keys=False, allow_unicode=False)


def create_default_config(config_path, log):
    """Create default YAML config with HTTP server on first free port."""
    if not is_yaml_path(config_path):
        raise ConfigError("Only YAML config files (.yaml/.yml) are supported")
    port = find_free_port()
    if port is None:
        raise SystemExit("Cannot find free port for HTTP server")
    store = ConfigStore(config_path, {
        "ports": [],
        "pools": [],
        "http": [{"name": "main", "address": "127.0.0.1", "port": port}],
    })
    store.save()
    log.info("Created default config: %s", config_path)
    log.info("HTTP server will start on port %d", port)
    return store


def load_config(config_path):
    """Load YAML config from path."""
    if not is_yaml_path(config_path):
        raise ConfigError("Only YAML config files (.yaml/.yml) are supported")
    with open(config_path, "r", encoding='utf-8') as config_file:
        try:
            configuration = _yaml.safe_load(config_file)
        except _yaml.YAMLError as err:
            raise ConfigError(f"Failed to parse YAML config: {err}") from err
    return ConfigStore(config_path, configuration)
