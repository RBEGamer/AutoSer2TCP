"""Ser2tcp
Simple proxy for connecting over TCP or telnet to serial port
"""

import argparse as _argparse
import importlib.metadata as _metadata
import logging as _logging
import os as _os
import signal as _signal

import serial.tools.list_ports as _list_ports

import ser2tcp.config_manager as _config_manager
import ser2tcp.pool_manager as _pool_manager
import ser2tcp.serial_proxy as _serial_proxy
import ser2tcp.server_manager as _server_manager

_about = None
for _dist_name in ("autoserial2tcp", "ser2tcp"):
    try:
        _about = _metadata.metadata(_dist_name)
        break
    except _metadata.PackageNotFoundError:
        continue

if _about is not None:
    VERSION_STR = "%s %s (%s)" % (
        _about["Name"], _about["Version"], _about["Author-email"])
else:
    VERSION_STR = "autoserial2tcp (not installed)"

DESCRIPTION_STR = VERSION_STR + """
(c) 2016-2026 by pavel.revak@gmail.com
https://github.com/cortexm/ser2tcp
"""

DEFAULT_CONFIG_DIR = _config_manager.DEFAULT_CONFIG_DIR
DEFAULT_CONFIG_PATH = _config_manager.DEFAULT_CONFIG_PATH


def list_usb_devices():
    """List USB serial devices with match attributes"""
    devices = []
    for port in _list_ports.comports():
        if port.vid is not None:
            devices.append(port)
    if not devices:
        print("No USB serial devices found")
        return
    for port in devices:
        print(f"{port.device}")
        print(f"  vid: 0x{port.vid:04X}")
        print(f"  pid: 0x{port.pid:04X}")
        if port.serial_number:
            print(f"  serial_number: {port.serial_number}")
        if port.manufacturer:
            print(f"  manufacturer: {port.manufacturer}")
        if port.product:
            print(f"  product: {port.product}")
        if port.location:
            print(f"  location: {port.location}")
        print()


def main():
    """Main"""
    parser = _argparse.ArgumentParser(description=DESCRIPTION_STR)
    parser.add_argument('-V', '--version', action='version', version=VERSION_STR)
    parser.add_argument(
        '-v', '--verbose', action='count', default=0,
        help="Increase verbosity")
    parser.add_argument(
        '-u', '--usb', action='store_true',
        help="List USB serial devices and exit")
    parser.add_argument(
        '--hash-password', metavar='PASSWORD',
        help="Hash password for config file and exit")
    parser.add_argument(
        '-c', '--config', default=DEFAULT_CONFIG_PATH,
        help=f"configuration in YAML format (default: {DEFAULT_CONFIG_PATH})")
    args = parser.parse_args()

    if args.hash_password:
        import ser2tcp.http_auth as _http_auth
        print(_http_auth.hash_password(args.hash_password))
        return

    if args.usb:
        list_usb_devices()
        return

    _logging.basicConfig(format='%(levelname).1s: %(message)s (%(filename)s:%(lineno)s)')
    log = _logging.getLogger('ser2tcp')
    log.setLevel((30, 20, 10)[min(2, args.verbose)])

    config_path = args.config
    if _os.path.exists(config_path):
        try:
            config_store = _config_manager.load_config(config_path)
        except _config_manager.ConfigError as err:
            raise SystemExit(str(err)) from err
    else:
        if config_path == DEFAULT_CONFIG_PATH:
            try:
                config_store = _config_manager.create_default_config(
                    config_path, log)
            except _config_manager.ConfigError as err:
                raise SystemExit(str(err)) from err
        else:
            raise SystemExit(f"Config file not found: {config_path}")
    configuration = config_store.data

    ports = configuration.get('ports', [])
    pools = configuration.get('pools', [])
    http_config = configuration.get('http')
    if not ports and not pools and not http_config:
        raise SystemExit("No ports, pools, or HTTP server configured")

    servers_manager = _server_manager.ServersManager()
    serial_proxies = []
    for config in ports:
        try:
            proxy = _serial_proxy.SerialProxy(config, log)
        except Exception as err:
            log.error("Failed to create port: %s", err)
            continue
        serial_proxies.append(proxy)
        servers_manager.add_server(proxy)

    pool_manager = None
    if pools:
        pool_manager = _pool_manager.PoolManager(config_store, log)
        servers_manager.add_server(pool_manager)

    if 'http' in configuration:
        import ser2tcp.http_server as _http_server
        http_server = _http_server.HttpServerWrapper(
            configuration['http'], serial_proxies, log,
            config_path=args.config, configuration=configuration,
            server_manager=servers_manager, config_store=config_store,
            pool_manager=pool_manager)
        servers_manager.add_server(http_server)

    _signal.signal(_signal.SIGTERM, servers_manager.stop)
    _signal.signal(_signal.SIGINT, servers_manager.stop)

    servers_manager.run()
    log.info("Exiting..")
