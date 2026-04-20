"""HTTP server integration with uhttp"""

import copy as _copy
import logging as _logging
import os as _os
import pathlib as _pathlib
import ssl as _ssl

import serial.tools.list_ports as _list_ports
import yaml as _yaml

import uhttp.server as _uhttp_server

import ser2tcp.http_auth as _http_auth
import ser2tcp.connection_control as _control
import ser2tcp.ip_filter as _ip_filter
import ser2tcp.pool_manager as _pool_manager
import ser2tcp.serial_proxy as _serial_proxy
import ser2tcp.server as _server
import ser2tcp.server_websocket as _server_websocket

HTML_DIR = _pathlib.Path(__file__).parent / 'html'


class HttpServerWrapper():
    """Wrapper around uhttp.HttpServer compatible with ServersManager"""

    def __init__(self, configs, serial_proxies, log=None,
            config_path=None, configuration=None,
            server_manager=None, config_store=None, pool_manager=None):
        self._log = log if log else _logging.getLogger(__name__)
        self._serial_proxies = serial_proxies
        self._server_manager = server_manager
        self._config_path = config_path
        self._configuration = configuration if configuration else {}
        self._config_store = config_store
        self._pool_manager = pool_manager
        if isinstance(configs, dict):
            configs = [configs]
        # Auth config at root level (users, tokens, session_timeout)
        # Migrate from old format (auth inside http config) if needed
        auth_config = {}
        if self._configuration.get('users'):
            auth_config['users'] = self._configuration['users']
        if self._configuration.get('tokens'):
            auth_config['tokens'] = self._configuration['tokens']
        if 'session_timeout' in self._configuration:
            auth_config['session_timeout'] = self._configuration['session_timeout']
        # Backward compatibility: migrate auth from http config to root
        if not auth_config:
            for config in configs:
                if 'auth' in config:
                    auth_config = config['auth']
                    break
        self._auth = _http_auth.SessionManager(auth_config) if auth_config else None
        self._ws_clients = {}  # uhttp client -> ServerWebSocket
        self._terminal_ws_servers = {}
        self._servers = []  # list of (HttpServer, IpFilter or None)
        self._pending_reload = False
        for config in configs:
            address = config.get('address', '0.0.0.0')
            port = config.get('port', 8080)
            ssl_context = None
            if 'ssl' in config:
                ssl_config = config['ssl']
                certfile = ssl_config.get('certfile')
                keyfile = ssl_config.get('keyfile')
                if not certfile or not keyfile:
                    self._log.error(
                        "HTTPS server %s:%d: missing certfile or keyfile, skipping",
                        address, port)
                    continue
                if not _os.path.exists(certfile):
                    self._log.error(
                        "HTTPS server %s:%d: certfile not found: %s, skipping",
                        address, port, certfile)
                    continue
                if not _os.path.exists(keyfile):
                    self._log.error(
                        "HTTPS server %s:%d: keyfile not found: %s, skipping",
                        address, port, keyfile)
                    continue
                ssl_context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
                ssl_context.load_cert_chain(certfile, keyfile)
                self._log.info(
                    "HTTPS server: %s:%d", address, port)
            else:
                self._log.info(
                    "HTTP server: %s:%d", address, port)
            ip_flt = _ip_filter.create_filter(config, log=self._log)
            self._servers.append((_uhttp_server.HttpServer(
                address=address, port=port, ssl_context=ssl_context,
                event_mode=True), ip_flt))

    def _ensure_pool_manager(self):
        """Create pool manager lazily when pool API is first used."""
        if self._pool_manager:
            return self._pool_manager
        self._configuration.setdefault('pools', [])
        config_store = self._config_store
        if config_store is None:
            class _InlineConfigStore():
                def __init__(self, wrapper):
                    self.data = wrapper._configuration
                    self._wrapper = wrapper

                def save(self):
                    self._wrapper._save_config()

            config_store = _InlineConfigStore(self)
        self._pool_manager = _pool_manager.PoolManager(config_store, self._log)
        if self._server_manager:
            self._server_manager.add_server(self._pool_manager)
        return self._pool_manager

    def _create_http_server(self, config):
        """Create a single HTTP server from config, return (server, ip_flt) or None"""
        address = config.get('address', '0.0.0.0')
        port = config.get('port', 8080)
        ssl_context = None
        if 'ssl' in config:
            ssl_config = config['ssl']
            certfile = ssl_config.get('certfile')
            keyfile = ssl_config.get('keyfile')
            if not certfile or not keyfile:
                raise ValueError(f"HTTPS {address}:{port}: missing certfile or keyfile")
            if not _os.path.exists(certfile):
                raise ValueError(f"HTTPS {address}:{port}: certfile not found: {certfile}")
            if not _os.path.exists(keyfile):
                raise ValueError(f"HTTPS {address}:{port}: keyfile not found: {keyfile}")
            ssl_context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(certfile, keyfile)
            self._log.info("HTTPS server: %s:%d", address, port)
        else:
            self._log.info("HTTP server: %s:%d", address, port)
        ip_flt = _ip_filter.create_filter(config, log=self._log)
        server = _uhttp_server.HttpServer(
            address=address, port=port, ssl_context=ssl_context,
            event_mode=True)
        return (server, ip_flt)

    def add_http_server(self, config):
        """Add a new HTTP server dynamically"""
        srv_tuple = self._create_http_server(config)
        self._servers.append(srv_tuple)
        return len(self._servers) - 1

    def remove_http_server(self, index):
        """Remove HTTP server by index"""
        if index < 0 or index >= len(self._servers):
            raise ValueError("Invalid server index")
        server, _ = self._servers[index]
        server.close()
        del self._servers[index]

    def reload_http_servers(self):
        """Reload all HTTP servers from current configuration"""
        # Close all existing servers
        for server, _ in self._servers:
            server.close()
        self._servers.clear()
        # Create new servers from config
        configs = self._configuration.get('http', [])
        if isinstance(configs, dict):
            configs = [configs]
        for config in configs:
            try:
                srv_tuple = self._create_http_server(config)
                self._servers.append(srv_tuple)
            except ValueError as e:
                self._log.error("Failed to create HTTP server: %s", e)

    def schedule_reload(self):
        """Schedule HTTP servers reload for next process_stale cycle"""
        self._pending_reload = True

    def read_sockets(self):
        """Return sockets for reading"""
        sockets = []
        for server, _ in self._servers:
            sockets.extend(server.read_sockets)
        return sockets

    def write_sockets(self):
        """Return sockets for writing"""
        sockets = []
        for server, _ in self._servers:
            sockets.extend(server.write_sockets)
        return sockets

    def process_read(self, read_sockets):
        """Process read events - also handles writes for uhttp"""
        self._process_uhttp(read_sockets, [])

    def process_write(self, write_sockets):
        """Process write events"""
        self._process_uhttp([], write_sockets)

    def _process_uhttp(self, read_sockets, write_sockets):
        """Process uhttp events"""
        for server, ip_flt in self._servers:
            client = server.process_events(read_sockets, write_sockets)
            if client:
                # Check IP filter for new requests
                if ip_flt and client.event in (
                        _uhttp_server.EVENT_REQUEST,
                        _uhttp_server.EVENT_HEADERS,
                        _uhttp_server.EVENT_WS_REQUEST):
                    client_ip = client.addr[0] \
                        if isinstance(client.addr, tuple) else None
                    if client_ip and not ip_flt.is_allowed(client_ip):
                        self._log.info(
                            "HTTP rejected (IP filter): %s", client_ip)
                        client.respond({'error': 'Forbidden'}, status=403)
                        continue
                if client.event == _uhttp_server.EVENT_WS_REQUEST:
                    self._handle_ws_upgrade(client)
                elif client.event in (
                        _uhttp_server.EVENT_WS_MESSAGE,
                        _uhttp_server.EVENT_WS_CHUNK_FIRST,
                        _uhttp_server.EVENT_WS_CHUNK_NEXT,
                        _uhttp_server.EVENT_WS_CHUNK_LAST):
                    ws_server = self._ws_clients.get(client)
                    if ws_server:
                        ws_server.process_message(client)
                elif client.event == _uhttp_server.EVENT_WS_CLOSE:
                    ws_server = self._ws_clients.pop(client, None)
                    if ws_server:
                        ws_server.remove_connection(client)
                elif client.event == _uhttp_server.EVENT_HEADERS:
                    client.accept_body()
                elif client.event == _uhttp_server.EVENT_COMPLETE:
                    self._handle_request(client)
                elif client.event == _uhttp_server.EVENT_REQUEST:
                    self._handle_request(client)


    def process_stale(self):
        """Cleanup expired sessions and handle pending reload"""
        if self._auth:
            self._auth.cleanup()
        if self._pending_reload:
            self._pending_reload = False
            self.reload_http_servers()
        for item in list(self._terminal_ws_servers.values()):
            item['server'].process_stale()

    def close(self):
        """Close all HTTP servers"""
        for server, _ in self._servers:
            server.close()
        for item in list(self._terminal_ws_servers.values()):
            item['server'].close()
        self._terminal_ws_servers.clear()

    def _get_ws_endpoints(self):
        """Build mapping of endpoint name -> ServerWebSocket"""
        endpoints = {}
        for proxy in self._serial_proxies:
            for server in proxy.servers:
                if server.protocol == 'WEBSOCKET':
                    endpoints[server.endpoint] = server
        for target in self._get_terminal_targets():
            endpoints[target['endpoint']] = self._terminal_ws_servers[
                target['endpoint']]['server']
        return endpoints

    def _get_terminal_targets(self):
        """Return web terminal targets backed by managed serial proxies."""
        targets = []
        for index, proxy in enumerate(self._serial_proxies):
            endpoint = f'__terminal-port-{index}'
            targets.append({
                'endpoint': endpoint,
                'label': self._format_terminal_label(proxy, index),
                'kind': 'port',
                'port_index': index,
                'connected': bool(proxy.is_connected),
                'proxy': proxy,
            })
        if self._pool_manager:
            terminal_targets = getattr(self._pool_manager, 'terminal_targets', None)
            if callable(terminal_targets):
                for target in terminal_targets():
                    proxy = target['proxy']
                    endpoint = (
                        f"__terminal-pool-{target['pool_index']}"
                        f"-{target['assignment_index']}"
                    )
                    targets.append({
                        'endpoint': endpoint,
                        'label': self._format_pool_terminal_label(target),
                        'kind': 'pool-assignment',
                        'pool_index': target['pool_index'],
                        'assignment_index': target['assignment_index'],
                        'connected': bool(proxy.is_connected),
                        'proxy': proxy,
                    })
        self._sync_terminal_ws_servers(targets)
        for target in targets:
            target['ws_path'] = '/ws/' + target['endpoint']
            target.pop('proxy', None)
        return targets

    def _sync_terminal_ws_servers(self, targets):
        """Keep managed terminal WebSocket endpoints aligned with live proxies."""
        next_servers = {}
        for target in targets:
            endpoint = target['endpoint']
            proxy = target['proxy']
            current = self._terminal_ws_servers.get(endpoint)
            if current and current['proxy'] is proxy:
                next_servers[endpoint] = current
                continue
            if current:
                current['server'].close()
            next_servers[endpoint] = {
                'proxy': proxy,
                'server': _server_websocket.ServerWebSocket(
                    self._terminal_server_config(endpoint), proxy, self._log),
            }
        for endpoint, current in list(self._terminal_ws_servers.items()):
            if endpoint not in next_servers:
                current['server'].close()
        self._terminal_ws_servers = next_servers

    def _terminal_server_config(self, endpoint):
        """Return WebSocket tunnel config for one managed terminal target."""
        return {
            'protocol': 'websocket',
            'endpoint': endpoint,
            'control': {
                'rts': True,
                'dtr': True,
                'signals': list(_control.SIGNAL_NAMES),
            },
        }

    def _format_terminal_label(self, proxy, index):
        """Return dropdown label for one static serial proxy."""
        serial_cfg = proxy.serial_config
        name = proxy.name or serial_cfg.get('port')
        if not name and proxy.match:
            name = 'match: ' + ', '.join(
                f'{key}={value}' for key, value in proxy.match.items())
        if not name:
            name = f'Port {index}'
        return name

    def _format_pool_terminal_label(self, target):
        """Return dropdown label for one active pool assignment."""
        name = target.get('name') or target.get('identity') or 'assignment'
        pool_name = target.get('pool_name')
        if pool_name:
            return f'{pool_name} - {name}'
        return name

    def _handle_ws_upgrade(self, client):
        """Handle WebSocket upgrade request"""
        path = client.path
        if not path.startswith('/ws/'):
            client.respond({'error': 'Not found'}, status=404)
            return
        endpoint_name = path[4:]
        endpoints = self._get_ws_endpoints()
        ws_server = endpoints.get(endpoint_name)
        if not ws_server:
            client.respond({'error': 'Not found'}, status=404)
            return
        # IP filter check
        if ws_server.ip_filter:
            client_ip = client.addr[0] if isinstance(client.addr, tuple) else None
            if client_ip and not ws_server.ip_filter.is_allowed(client_ip):
                self._log.info(
                    "WebSocket rejected (IP filter): %s", client_ip)
                client.respond({'error': 'Forbidden'}, status=403)
                return
        # Auth: per-server token, global auth, or both
        # No auth configured and no per-server token → allow
        token = self._get_bearer_token(client)
        if ws_server.token and token == ws_server.token:
            pass  # per-server token matches
        elif self._auth and not self._auth.is_empty:
            if not token:
                client.respond(
                    {'error': 'Authorization required'}, status=401)
                return
            # Try global auth first, then per-server token
            user = self._auth.authenticate(token)
            if not user and token != ws_server.token:
                client.respond(
                    {'error': 'Invalid or expired token'}, status=401)
                return
        elif ws_server.token:
            # No global auth, but server has token
            if token != ws_server.token:
                client.respond(
                    {'error': 'Authorization required'}, status=401)
                return
        client.accept_websocket()
        self._ws_clients[client] = ws_server
        ws_server.add_connection(client)

    def _get_bearer_token(self, client):
        """Extract token from Authorization header or query parameter"""
        auth = client.headers.get('authorization', '')
        if auth.startswith('Bearer '):
            return auth[7:]
        if client.query:
            return client.query.get('token')
        return None

    def _error(self, client, error, status):
        """Log warning and send error response"""
        self._log.warning("%s", error)
        client.respond({'error': error}, status=status)

    def _format_connection_address(self, server, connection):
        """Return formatted address for socket or WebSocket connections"""
        protocol = getattr(server, 'protocol', '')
        if isinstance(protocol, str) and protocol.upper() == 'WEBSOCKET':
            addr = getattr(connection, 'addr', None)
            if isinstance(addr, tuple) and len(addr) >= 2:
                return "%s:%d" % (addr[0], addr[1])
            if addr is not None:
                return str(addr)
            return 'unknown'
        return connection.address_str()

    def _disconnect_connection(self, server, connection):
        """Disconnect a runtime connection across supported server types"""
        protocol = getattr(server, 'protocol', '')
        if isinstance(protocol, str) and protocol.upper() == 'WEBSOCKET':
            server.remove_connection(connection)
            return
        server._remove_connection(connection)

    def _require_auth(self, client):
        """Check authentication, return user info or None (sends 401)"""
        if not self._auth or self._auth.is_empty:
            return {'login': None, 'admin': True}
        token = self._get_bearer_token(client)
        if not token:
            self._error(client, 'Authorization required', 401)
            return None
        user = self._auth.authenticate(token)
        if not user:
            self._error(client, 'Invalid or expired token', 401)
            return None
        return user

    def _handle_request(self, client):
        """Handle HTTP request"""
        if self._log.isEnabledFor(_logging.INFO):
            self._log.info("%s %s", client.method, client.path)
        # Login endpoint - no auth required
        if client.method == 'POST' and client.path == '/api/login':
            self._handle_api_login(client)
            return
        # Logout endpoint
        if client.method == 'POST' and client.path == '/api/logout':
            self._handle_api_logout(client)
            return
        # WebSocket terminal clients
        if client.method == 'GET' \
                and client.path.startswith('/xterm/'):
            client.respond_file(str(HTML_DIR / 'xterm.html'))
            return
        if client.method == 'GET' \
                and client.path.startswith('/raw/'):
            client.respond_file(str(HTML_DIR / 'raw.html'))
            return
        # Static files - no auth
        if client.method == 'GET' and not client.path.startswith('/api/'):
            self._handle_static(client)
            return
        # All API endpoints require auth
        user = self._require_auth(client)
        if not user:
            return
        if client.method == 'GET' and client.path == '/api/status':
            self._handle_api_status(client, user)
        elif client.method == 'GET' and client.path == '/api/terminal-targets':
            self._handle_api_terminal_targets(client)
        elif client.method == 'GET' and client.path == '/api/detect':
            self._handle_api_detect(client)
        elif client.path == '/api/ports':
            if client.method == 'POST':
                self._handle_api_ports_add(client, user)
            else:
                self._error(client, 'Method not allowed', 405)
        elif client.path == '/api/pools':
            if client.method == 'POST':
                self._handle_api_pools_add(client, user)
            else:
                self._error(client, 'Method not allowed', 405)
        elif client.method == 'GET' and client.path == '/api/signals':
            self._handle_api_signals(client)
        elif client.path.startswith('/api/ports/'):
            self._route_api_ports_item(client, user)
        elif client.path.startswith('/api/pools/'):
            self._route_api_pools_item(client, user)
        elif client.path == '/api/users':
            if client.method == 'GET':
                self._handle_api_users_list(client, user)
            elif client.method == 'POST':
                self._handle_api_users_add(client, user)
            else:
                self._error(client, 'Method not allowed', 405)
        elif client.path.startswith('/api/users/'):
            login = client.path[len('/api/users/'):]
            if client.method == 'PUT':
                self._handle_api_users_update(client, user, login)
            elif client.method == 'DELETE':
                self._handle_api_users_delete(client, user, login)
            else:
                self._error(client, 'Method not allowed', 405)
        elif client.path == '/api/tokens':
            if client.method == 'GET':
                self._handle_api_tokens_list(client, user)
            elif client.method == 'POST':
                self._handle_api_tokens_add(client, user)
            else:
                self._error(client, 'Method not allowed', 405)
        elif client.path.startswith('/api/tokens/'):
            token_id = client.path[len('/api/tokens/'):]
            if client.method == 'PUT':
                self._handle_api_tokens_update(client, user, token_id)
            elif client.method == 'DELETE':
                self._handle_api_tokens_delete(client, user, token_id)
            else:
                self._error(client, 'Method not allowed', 405)
        elif client.path == '/api/settings':
            if client.method == 'GET':
                self._handle_api_settings_get(client)
            elif client.method == 'PUT':
                self._handle_api_settings_update(client, user)
            else:
                self._error(client, 'Method not allowed', 405)
        elif client.path == '/api/settings/http':
            if client.method == 'POST':
                self._handle_api_http_add(client, user)
            else:
                self._error(client, 'Method not allowed', 405)
        elif client.path.startswith('/api/settings/http/'):
            try:
                index = int(client.path[len('/api/settings/http/'):])
            except ValueError:
                self._error(client, 'Invalid index', 400)
                return
            if client.method == 'PUT':
                self._handle_api_http_update(client, user, index)
            elif client.method == 'DELETE':
                self._handle_api_http_delete(client, user, index)
            else:
                self._error(client, 'Method not allowed', 405)
        else:
            self._error(client, 'Not found', 404)

    def _handle_static(self, client):
        """Serve static files from html directory"""
        path = client.path.lstrip('/')
        if not path:
            path = 'index.html'
        file_path = (HTML_DIR / path).resolve()
        if not str(file_path).startswith(str(HTML_DIR)):
            self._error(client, 'Not found', 404)
            return
        if not file_path.is_file():
            self._error(client, 'Not found', 404)
            return
        client.respond_file(str(file_path))

    def _handle_api_status(self, client, user):
        """Return runtime status with connections"""
        ports = []
        for proxy in self._serial_proxies:
            serial_cfg = proxy.serial_config
            serial_info = {
                'port': serial_cfg.get('port'),
                'connected': proxy.is_connected,
            }
            for key in ('baudrate', 'bytesize', 'parity', 'stopbits'):
                if key in serial_cfg:
                    serial_info[key] = serial_cfg[key]
            port_info = {'serial': serial_info}
            if proxy.name:
                port_info['name'] = proxy.name
            if proxy.max_connections:
                port_info['max_connections'] = proxy.max_connections
            if proxy.match:
                port_info['serial']['match'] = proxy.match
            servers = []
            for server in proxy.servers:
                if server.protocol == 'WEBSOCKET':
                    srv_info = {
                        'protocol': server.protocol,
                        'endpoint': server.endpoint,
                        'connections': [],
                    }
                    for con in server.connections:
                        try:
                            addr = con.addr
                            if isinstance(addr, tuple) and len(addr) >= 2:
                                srv_info['connections'].append(
                                    {'address': '%s:%d' % (addr[0], addr[1])})
                            else:
                                srv_info['connections'].append(
                                    {'address': str(addr)})
                        except Exception:
                            srv_info['connections'].append(
                                {'address': 'unknown'})
                else:
                    srv_info = {
                        'protocol': server.protocol,
                        'address': server.config['address'],
                        'connections': [
                            {'address': con.address_str()}
                            for con in server.connections
                        ],
                    }
                    if server.protocol != 'SOCKET':
                        srv_info['port'] = server.config['port']
                    if 'ssl' in server.config:
                        srv_info['ssl'] = server.config['ssl']
                if not server.data_enabled:
                    srv_info['data'] = False
                if server.control:
                    srv_info['control'] = server.control
                if server.max_connections:
                    srv_info['max_connections'] = server.max_connections
                servers.append(srv_info)
            port_info['servers'] = servers
            if proxy.is_connected:
                bitmask = proxy.get_signals()
                signals = {}
                for name in _control.SIGNAL_NAMES:
                    bit = _control.SIGNAL_BITS[name]
                    signals[name] = bool(bitmask & (1 << bit))
                port_info['signals'] = signals
            ports.append(port_info)
        is_admin = user.get('admin', False) if user else False
        pools = self._pool_manager.status() if self._pool_manager else []
        client.respond({'ports': ports, 'pools': pools, 'admin': is_admin})

    def _handle_api_terminal_targets(self, client):
        """Return managed serial targets for the tunnel terminal page."""
        client.respond(self._get_terminal_targets())

    def _handle_api_detect(self, client):
        """Return list of available serial ports"""
        ports = []
        for port in _list_ports.comports():
            info = {'device': port.device}
            if port.description and port.description != 'n/a':
                info['description'] = port.description
            if port.hwid and port.hwid != 'n/a':
                info['hwid'] = port.hwid
            if port.vid is not None:
                info['vid'] = f'0x{port.vid:04X}'
                info['pid'] = f'0x{port.pid:04X}'
                if port.serial_number:
                    info['serial_number'] = port.serial_number
                if port.manufacturer:
                    info['manufacturer'] = port.manufacturer
                if port.product:
                    info['product'] = port.product
                if port.location:
                    info['location'] = port.location
            ports.append(info)
        client.respond(ports)

    def _handle_api_signals(self, client):
        """Return signal states for all ports"""
        result = []
        for proxy in self._serial_proxies:
            bitmask = proxy.get_signals()
            signals = {}
            for name in _control.SIGNAL_NAMES:
                bit = _control.SIGNAL_BITS[name]
                signals[name] = bool(bitmask & (1 << bit))
            result.append({
                'name': proxy.name,
                'connected': proxy.is_connected,
                'signals': signals,
            })
        client.respond(result)

    def _save_config(self):
        """Save configuration to config file"""
        if self._config_store is not None:
            self._config_store.save()
            return
        if not self._config_path or not self._configuration:
            return
        with open(self._config_path, 'w', encoding='utf-8') as f:
            _yaml.safe_dump(
                self._configuration, f, sort_keys=False, allow_unicode=False)

    def _get_ports_config(self):
        """Get ports list from configuration"""
        ports = self._configuration.get('ports', [])
        if isinstance(self._configuration, list):
            ports = self._configuration
        return ports

    def _get_pools_config(self):
        """Get pools list from configuration."""
        return self._configuration.setdefault('pools', [])

    def _route_api_ports_item(self, client, user):
        """Route /api/ports/<index>/... requests"""
        rest = client.path[len('/api/ports/'):]
        parts = rest.split('/')
        try:
            index = int(parts[0])
        except ValueError:
            self._error(client, 'Invalid port index', 400)
            return
        if len(parts) == 1:
            if client.method == 'PUT':
                self._handle_api_ports_update(client, user, index)
            elif client.method == 'DELETE':
                self._handle_api_ports_delete(client, user, index)
            else:
                self._error(client, 'Method not allowed', 405)
        elif len(parts) == 2 and parts[1] == 'signals' \
                and client.method == 'PUT':
            self._handle_api_set_signals(client, user, index)
        elif len(parts) == 4 and parts[1] == 'connections' \
                and client.method == 'DELETE':
            try:
                srv_idx = int(parts[2])
                con_idx = int(parts[3])
            except ValueError:
                self._error(client, 'Invalid index', 400)
                return
            self._handle_api_disconnect(client, user, index,
                srv_idx, con_idx)
        else:
            self._error(client, 'Not found', 404)

    def _route_api_pools_item(self, client, user):
        """Route /api/pools/<index>/... requests."""
        if not self._pool_manager:
            self._error(client, 'Pool not found', 404)
            return
        rest = client.path[len('/api/pools/'):]
        parts = rest.split('/')
        try:
            index = int(parts[0])
        except ValueError:
            self._error(client, 'Invalid pool index', 400)
            return
        if len(parts) == 1:
            if client.method == 'PUT':
                self._handle_api_pools_update(client, user, index)
            elif client.method == 'DELETE':
                self._handle_api_pools_delete(client, user, index)
            else:
                self._error(client, 'Method not allowed', 405)
            return
        if len(parts) == 2 and parts[1] == 'state':
            if client.method == 'PUT':
                self._handle_api_pools_state(client, user, index)
            else:
                self._error(client, 'Method not allowed', 405)
            return
        if len(parts) == 2 and parts[1] == 'assignments':
            if client.method == 'POST':
                self._handle_api_assignments_add(client, user, index)
            else:
                self._error(client, 'Method not allowed', 405)
            return
        if len(parts) == 4 and parts[1] == 'assignments' and parts[3] == 'state':
            try:
                assignment_index = int(parts[2])
            except ValueError:
                self._error(client, 'Invalid assignment index', 400)
                return
            if client.method == 'PUT':
                self._handle_api_assignments_state(
                    client, user, index, assignment_index)
            else:
                self._error(client, 'Method not allowed', 405)
            return
        if len(parts) == 3 and parts[1] == 'assignments':
            try:
                assignment_index = int(parts[2])
            except ValueError:
                self._error(client, 'Invalid assignment index', 400)
                return
            if client.method == 'DELETE':
                self._handle_api_assignments_delete(
                    client, user, index, assignment_index)
            else:
                self._error(client, 'Method not allowed', 405)
            return
        self._error(client, 'Not found', 404)

    def _validate_port_config(self, data):
        """Validate port configuration, return error string or None"""
        if not isinstance(data, dict):
            return f'Expected JSON object, got {type(data).__name__}'
        if 'serial' not in data:
            return 'serial config required'
        serial = data['serial']
        if not isinstance(serial, dict):
            return 'Invalid serial config'
        if 'port' not in serial and 'match' not in serial:
            return "serial config must have 'port' or 'match'"
        # Validate port-level max_connections (0 = unlimited, default)
        if 'max_connections' in data:
            max_conn = data['max_connections']
            if not isinstance(max_conn, int) or max_conn < 0:
                return 'max_connections must be 0 or positive integer'
        if 'servers' not in data or not isinstance(data['servers'], list):
            return 'servers list required'
        if not data['servers']:
            return 'At least one server required'
        for srv in data['servers']:
            if not isinstance(srv, dict):
                return 'Invalid server config'
            if 'protocol' not in srv:
                return 'Server protocol required'
            proto = srv['protocol'].upper()
            if proto not in ('TCP', 'TELNET', 'SSL', 'SOCKET', 'WEBSOCKET'):
                return f'Unknown protocol: {srv["protocol"]}'
            if not srv.get('data', True) and 'control' not in srv:
                return '"data": false requires "control" config'
            if proto == 'WEBSOCKET':
                if 'endpoint' not in srv:
                    return 'WebSocket endpoint required'
            elif proto == 'SOCKET':
                if 'address' not in srv:
                    return 'Socket path (address) required'
            else:
                if 'port' not in srv:
                    return 'Server port required'
            if 'control' in srv:
                if proto == 'TELNET':
                    return 'Control not supported with TELNET'
                ctl = srv['control']
                if not isinstance(ctl, dict):
                    return 'Invalid control config'
                if 'signals' in ctl:
                    if not isinstance(ctl['signals'], list):
                        return 'control.signals must be a list'
                    for sig in ctl['signals']:
                        if sig.lower() not in _control.SIGNAL_BITS:
                            return f'Unknown signal: {sig}'
            # Validate IP filter config
            for key in ('allow', 'deny'):
                if key in srv:
                    if not isinstance(srv[key], list):
                        return f'{key} must be a list'
                    for network in srv[key]:
                        if not isinstance(network, str):
                            return f'{key} entries must be strings'
            # Validate max_connections (0 = unlimited)
            if 'max_connections' in srv:
                max_conn = srv['max_connections']
                if not isinstance(max_conn, int) or max_conn < 0:
                    return 'max_connections must be 0 or positive integer'
        return None

    def _validate_pool_config(self, data):
        """Validate wildcard pool config, return error string or None."""
        if not isinstance(data, dict):
            return f'Expected JSON object, got {type(data).__name__}'
        serial = data.get('serial')
        if not isinstance(serial, dict):
            return 'serial config required'
        glob_pattern = serial.get('glob')
        if not isinstance(glob_pattern, str) or not glob_pattern.strip():
            return 'serial.glob is required'
        server = data.get('server')
        if not isinstance(server, dict):
            return 'server config required'
        start_port = server.get('start_port')
        if not isinstance(start_port, int) or start_port < 1 or start_port > 65535:
            return 'server.start_port must be 1-65535'
        if 'address' in server and not isinstance(server['address'], str):
            return 'server.address must be a string'
        if 'send_timeout' in server and not isinstance(
                server['send_timeout'], (int, float)):
            return 'server.send_timeout must be numeric'
        if 'buffer_limit' in server and server['buffer_limit'] is not None:
            if not isinstance(server['buffer_limit'], int) or server['buffer_limit'] < 0:
                return 'server.buffer_limit must be null or positive integer'
        if 'max_connections' in server:
            max_conn = server['max_connections']
            if not isinstance(max_conn, int) or max_conn < 0:
                return 'server.max_connections must be 0 or positive integer'
        return None

    def _get_used_endpoints(self, exclude_index=None):
        """Return set of endpoint names used across all proxies"""
        endpoints = set()
        for i, proxy in enumerate(self._serial_proxies):
            if i == exclude_index:
                continue
            for server in proxy.servers:
                if server.protocol == 'WEBSOCKET':
                    endpoints.add(server.endpoint)
        return endpoints

    def _validate_endpoints(self, data, exclude_index=None):
        """Check for duplicate endpoints, return error or None"""
        used = self._get_used_endpoints(exclude_index)
        seen = set()
        for srv in data.get('servers', []):
            proto = srv.get('protocol', '').upper()
            if proto != 'WEBSOCKET':
                continue
            ep = srv.get('endpoint')
            if ep in seen:
                return f'Duplicate endpoint in config: {ep}'
            if ep in used:
                return f'Endpoint already in use: {ep}'
            seen.add(ep)
        return None

    def _create_proxy(self, config):
        """Create SerialProxy from config"""
        proxy = _serial_proxy.SerialProxy(config, self._log)
        return proxy

    def _handle_api_ports_add(self, client, user):
        """Add new port configuration"""
        if not self._require_admin(client, user):
            return
        data = client.data
        error = self._validate_port_config(data)
        if not error:
            error = self._validate_endpoints(data)
        if error:
            self._error(client, error, 400)
            return
        try:
            proxy = self._create_proxy(data)
        except (ValueError, KeyError, OSError, _server.ConfigError) as err:
            self._error(client, str(err), 400)
            return
        self._serial_proxies.append(proxy)
        if self._server_manager:
            self._server_manager.add_server(proxy)
        ports = self._get_ports_config()
        ports.append(data)
        if 'ports' not in self._configuration:
            self._configuration['ports'] = ports
        self._save_config()
        self._log.info("Port added: %d", len(self._serial_proxies) - 1)
        client.respond({'ok': True, 'index': len(self._serial_proxies) - 1},
            status=201)

    def _handle_api_ports_update(self, client, user, index):
        """Update port configuration"""
        if not self._require_admin(client, user):
            return
        ports = self._get_ports_config()
        if index < 0 or index >= len(ports):
            self._error(client, 'Port not found', 404)
            return
        data = client.data
        error = self._validate_port_config(data)
        if not error:
            error = self._validate_endpoints(data, exclude_index=index)
        if error:
            self._error(client, error, 400)
            return
        # Close old proxy first to release ports
        old_proxy = self._serial_proxies[index]
        old_proxy.close()
        if self._server_manager:
            self._server_manager.remove_server(old_proxy)
        try:
            new_proxy = self._create_proxy(data)
        except (ValueError, KeyError, OSError, _server.ConfigError) as err:
            # Rollback: recreate old proxy
            try:
                old_proxy = self._create_proxy(ports[index])
                self._serial_proxies[index] = old_proxy
                if self._server_manager:
                    self._server_manager.add_server(old_proxy)
            except Exception:
                pass
            self._error(client, str(err), 400)
            return
        if self._server_manager:
            self._server_manager.add_server(new_proxy)
        self._serial_proxies[index] = new_proxy
        ports[index] = data
        self._save_config()
        self._log.info("Port updated: %d", index)
        client.respond({'ok': True})

    def _handle_api_ports_delete(self, client, user, index):
        """Delete port configuration"""
        if not self._require_admin(client, user):
            return
        ports = self._get_ports_config()
        if index < 0 or index >= len(ports):
            self._error(client, 'Port not found', 404)
            return
        old_proxy = self._serial_proxies[index]
        old_proxy.close()
        if self._server_manager:
            self._server_manager.remove_server(old_proxy)
        del self._serial_proxies[index]
        del ports[index]
        self._save_config()
        self._log.info("Port deleted: %d", index)
        client.respond({'ok': True})

    def _handle_api_pools_add(self, client, user):
        """Add wildcard pool configuration."""
        if not self._require_admin(client, user):
            return
        data = client.data
        error = self._validate_pool_config(data)
        if error:
            self._error(client, error, 400)
            return
        try:
            index = self._ensure_pool_manager().add_pool(data)
        except _pool_manager.PoolValidationError as err:
            self._error(client, str(err), 400)
            return
        self._log.info("Pool added: %d", index)
        client.respond({'ok': True, 'index': index}, status=201)

    def _handle_api_pools_update(self, client, user, index):
        """Update wildcard pool config."""
        if not self._require_admin(client, user):
            return
        if not self._pool_manager:
            self._error(client, 'Pool not found', 404)
            return
        data = client.data
        error = self._validate_pool_config(data)
        if error:
            self._error(client, error, 400)
            return
        try:
            self._pool_manager.update_pool(index, data)
        except _pool_manager.PoolValidationError as err:
            self._error(client, str(err), 404)
            return
        self._log.info("Pool updated: %d", index)
        client.respond({'ok': True})

    def _handle_api_pools_delete(self, client, user, index):
        """Delete wildcard pool config."""
        if not self._require_admin(client, user):
            return
        if not self._pool_manager:
            self._error(client, 'Pool not found', 404)
            return
        try:
            self._pool_manager.delete_pool(index)
        except _pool_manager.PoolValidationError as err:
            self._error(client, str(err), 404)
            return
        self._log.info("Pool deleted: %d", index)
        client.respond({'ok': True})

    def _handle_api_pools_state(self, client, user, index):
        """Enable or disable a pool."""
        if not self._require_admin(client, user):
            return
        if not self._pool_manager:
            self._error(client, 'Pool not found', 404)
            return
        data = client.data
        if not isinstance(data, dict) or 'enabled' not in data:
            self._error(client, 'enabled is required', 400)
            return
        try:
            self._pool_manager.set_pool_enabled(index, bool(data['enabled']))
        except _pool_manager.PoolValidationError as err:
            self._error(client, str(err), 404)
            return
        client.respond({'ok': True})

    def _handle_api_assignments_add(self, client, user, pool_index):
        """Add one explicit pool assignment."""
        if not self._require_admin(client, user):
            return
        if not self._pool_manager:
            self._error(client, 'Pool not found', 404)
            return
        data = client.data
        if not isinstance(data, dict):
            self._error(client, 'Invalid request', 400)
            return
        identity = data.get('identity', '').strip()
        if not identity:
            self._error(client, 'identity is required', 400)
            return
        name = data.get('name')
        enabled = bool(data.get('enabled', True))
        try:
            index = self._pool_manager.add_assignment(
                pool_index, identity, name=name, enabled=enabled)
        except _pool_manager.PoolValidationError as err:
            self._error(client, str(err), 400)
            return
        client.respond({'ok': True, 'index': index}, status=201)

    def _handle_api_assignments_state(self, client, user, pool_index,
            assignment_index):
        """Enable or disable one assignment."""
        if not self._require_admin(client, user):
            return
        if not self._pool_manager:
            self._error(client, 'Pool not found', 404)
            return
        data = client.data
        if not isinstance(data, dict) or 'enabled' not in data:
            self._error(client, 'enabled is required', 400)
            return
        try:
            self._pool_manager.set_assignment_enabled(
                pool_index, assignment_index, bool(data['enabled']))
        except _pool_manager.PoolValidationError as err:
            self._error(client, str(err), 404)
            return
        client.respond({'ok': True})

    def _handle_api_assignments_delete(self, client, user, pool_index,
            assignment_index):
        """Delete one assignment."""
        if not self._require_admin(client, user):
            return
        if not self._pool_manager:
            self._error(client, 'Pool not found', 404)
            return
        try:
            self._pool_manager.delete_assignment(pool_index, assignment_index)
        except _pool_manager.PoolValidationError as err:
            self._error(client, str(err), 404)
            return
        client.respond({'ok': True})

    def _handle_api_set_signals(self, client, user, index):
        """Set RTS/DTR signals on a port"""
        if not self._require_admin(client, user):
            return
        if index < 0 or index >= len(self._serial_proxies):
            self._error(client, 'Port not found', 404)
            return
        proxy = self._serial_proxies[index]
        if not proxy.is_connected:
            self._error(client, 'Port not connected', 400)
            return
        data = client.data
        if not isinstance(data, dict):
            self._error(client, 'Invalid request', 400)
            return
        if 'rts' in data:
            proxy.set_rts(bool(data['rts']))
        if 'dtr' in data:
            proxy.set_dtr(bool(data['dtr']))
        client.respond({'ok': True})

    def _handle_api_disconnect(self, client, user, port_idx,
            srv_idx, con_idx):
        """Disconnect a specific client connection"""
        if port_idx < 0 or port_idx >= len(self._serial_proxies):
            self._error(client, 'Port not found', 404)
            return
        proxy = self._serial_proxies[port_idx]
        if srv_idx < 0 or srv_idx >= len(proxy.servers):
            self._error(client, 'Server not found', 404)
            return
        server = proxy.servers[srv_idx]
        if con_idx < 0 or con_idx >= len(server.connections):
            self._error(client, 'Connection not found', 404)
            return
        con = server.connections[con_idx]
        addr = self._format_connection_address(server, con)
        self._disconnect_connection(server, con)
        self._log.info("Disconnected: %s", addr)
        client.respond({'ok': True})

    def _handle_api_login(self, client):
        """Authenticate user and return session token"""
        if not self._auth:
            self._error(client, 'Auth not configured', 404)
            return
        data = client.data
        if not isinstance(data, dict):
            self._error(client, 'Invalid request', 400)
            return
        login = data.get('login', '')
        password = data.get('password', '')
        token = self._auth.login(login, password)
        if not token:
            self._error(client, f'Login failed: {login}', 401)
            return
        self._log.info("Login: %s", login)
        client.respond({'token': token})

    def _handle_api_logout(self, client):
        """Invalidate session"""
        if not self._auth:
            self._error(client, 'Auth not configured', 404)
            return
        token = self._get_bearer_token(client)
        if token:
            self._auth.logout(token)
        client.respond({'ok': True})

    def _require_admin(self, client, user):
        """Check if user is admin, send 403 if not"""
        if not user.get('admin'):
            self._error(client, 'Admin access required', 403)
            return False
        return True

    def _ensure_auth(self):
        """Create auth if not exists, return SessionManager"""
        if not self._auth:
            self._auth = _http_auth.SessionManager({})
        return self._auth

    def _save_auth_config(self):
        """Save auth config to config file (users, tokens at root level)"""
        if not self._config_path or not self._configuration:
            return
        auth_config = self._auth.get_auth_config()
        # Save at root level
        if auth_config.get('users'):
            self._configuration['users'] = auth_config['users']
        elif 'users' in self._configuration:
            del self._configuration['users']
        if auth_config.get('tokens'):
            self._configuration['tokens'] = auth_config['tokens']
        elif 'tokens' in self._configuration:
            del self._configuration['tokens']
        if 'session_timeout' in auth_config:
            self._configuration['session_timeout'] = auth_config['session_timeout']
        # Remove old auth from http configs (migration)
        http_configs = self._configuration.get('http', [])
        if isinstance(http_configs, dict):
            http_configs = [http_configs]
        for config in http_configs:
            config.pop('auth', None)
        self._save_config()

    def _handle_api_users_list(self, client, user):
        """List users (without passwords)"""
        if not self._require_admin(client, user):
            return
        if not self._auth:
            client.respond([])
            return
        client.respond(self._auth.list_users())

    def _handle_api_users_add(self, client, user):
        """Add new user"""
        if not self._require_admin(client, user):
            return
        data = client.data
        if not isinstance(data, dict) or 'login' not in data \
                or 'password' not in data:
            self._error(client, 'login and password required', 400)
            return
        kwargs = {}
        if 'admin' in data:
            kwargs['admin'] = bool(data['admin'])
        if 'session_timeout' in data:
            kwargs['session_timeout'] = data['session_timeout']
        auth = self._ensure_auth()
        is_first = auth.is_empty
        if not auth.add_user(data['login'], data['password'], **kwargs):
            self._error(client, 'User already exists', 400)
            return
        self._save_auth_config()
        self._log.info("User added: %s", data['login'])
        if is_first:
            token = auth.create_session(data['login'])
            client.respond({'ok': True, 'token': token}, status=201)
        else:
            client.respond({'ok': True}, status=201)

    def _handle_api_users_update(self, client, user, login):
        """Update existing user"""
        if not self._auth:
            self._error(client, 'User not found', 404)
            return
        if not self._require_admin(client, user):
            return
        data = client.data
        if not isinstance(data, dict):
            self._error(client, 'Invalid request', 400)
            return
        kwargs = {}
        if 'password' in data:
            kwargs['password'] = data['password']
        if 'admin' in data:
            kwargs['admin'] = bool(data['admin'])
        if 'session_timeout' in data:
            kwargs['session_timeout'] = data['session_timeout']
        result = self._auth.update_user(login, **kwargs)
        if result is False:
            self._error(client, 'User not found', 404)
            return
        if isinstance(result, str):
            self._error(client, result, 400)
            return
        self._save_auth_config()
        self._log.info("User updated: %s", login)
        client.respond({'ok': True})

    def _handle_api_users_delete(self, client, user, login):
        """Delete user"""
        if not self._auth:
            self._error(client, 'User not found', 404)
            return
        if not self._require_admin(client, user):
            return
        result = self._auth.delete_user(login)
        if result is False:
            self._error(client, 'User not found', 404)
            return
        if isinstance(result, str):
            self._error(client, result, 400)
            return
        self._save_auth_config()
        self._log.info("User deleted: %s", login)
        client.respond({'ok': True})

    def _handle_api_tokens_list(self, client, user):
        """List API tokens"""
        if not self._require_admin(client, user):
            return
        if not self._auth:
            client.respond([])
            return
        client.respond(self._auth.list_tokens())

    def _handle_api_tokens_add(self, client, user):
        """Add new API token"""
        if not self._require_admin(client, user):
            return
        data = client.data
        if not isinstance(data, dict) or 'token' not in data \
                or 'name' not in data:
            self._error(client, 'token and name required', 400)
            return
        auth = self._ensure_auth()
        admin = bool(data.get('admin', False))
        if not auth.add_token(data['token'], data['name'], admin):
            self._error(client, 'Token already exists', 400)
            return
        self._save_auth_config()
        self._log.info("Token added: %s", data['name'])
        client.respond({'ok': True}, status=201)

    def _handle_api_tokens_update(self, client, user, token):
        """Update API token"""
        if not self._auth:
            self._error(client, 'Token not found', 404)
            return
        if not self._require_admin(client, user):
            return
        data = client.data
        if not isinstance(data, dict):
            self._error(client, 'Invalid request', 400)
            return
        kwargs = {}
        if 'token' in data:
            kwargs['token'] = data['token']
        if 'name' in data:
            kwargs['name'] = data['name']
        if 'admin' in data:
            kwargs['admin'] = bool(data['admin'])
        result = self._auth.update_token(token, **kwargs)
        if result is False:
            self._error(client, 'Token not found', 404)
            return
        if isinstance(result, str):
            self._error(client, result, 400)
            return
        self._save_auth_config()
        self._log.info("Token updated: %s", token[:8] + '...')
        client.respond({'ok': True})

    def _handle_api_tokens_delete(self, client, user, token):
        """Delete API token"""
        if not self._auth:
            self._error(client, 'Token not found', 404)
            return
        if not self._require_admin(client, user):
            return
        result = self._auth.delete_token(token)
        if result is False:
            self._error(client, 'Token not found', 404)
            return
        if isinstance(result, str):
            self._error(client, result, 400)
            return
        self._save_auth_config()
        self._log.info("Token deleted: %s", token[:8] + '...')
        client.respond({'ok': True})

    def _handle_api_settings_get(self, client):
        """Return settings (http servers, session_timeout)"""
        settings = {
            'http': self._configuration.get('http', []),
            'session_timeout': self._configuration.get('session_timeout'),
        }
        client.respond(settings)

    def _handle_api_settings_update(self, client, user):
        """Update settings"""
        if not self._require_admin(client, user):
            return
        data = client.data
        if not isinstance(data, dict):
            self._error(client, f'Expected JSON object, got {type(data).__name__}', 400)
            return
        if 'session_timeout' in data:
            val = data['session_timeout']
            if val is not None and (not isinstance(val, int) or val < 0):
                self._error(client, 'session_timeout must be positive integer or null', 400)
                return
            if val is None:
                self._configuration.pop('session_timeout', None)
            else:
                self._configuration['session_timeout'] = val
        self._save_config()
        self._log.info("Settings updated")
        client.respond({'ok': True})

    def _validate_http_config(self, data):
        """Validate HTTP server config, return error string or None"""
        if not isinstance(data, dict):
            return f'Expected JSON object, got {type(data).__name__}'
        if 'port' not in data:
            return 'port is required'
        if not isinstance(data['port'], int) or data['port'] < 1 or data['port'] > 65535:
            return 'port must be 1-65535'
        if 'ssl' in data:
            ssl = data['ssl']
            if not isinstance(ssl, dict):
                return 'ssl must be an object'
            if not ssl.get('certfile') or not ssl.get('keyfile'):
                return 'ssl requires certfile and keyfile paths'
        return None

    def _handle_api_http_add(self, client, user):
        """Add new HTTP server"""
        if not self._require_admin(client, user):
            return
        data = client.data
        error = self._validate_http_config(data)
        if error:
            self._error(client, error, 400)
            return
        http_list = self._configuration.setdefault('http', [])
        srv = {'address': data.get('address', '0.0.0.0'), 'port': data['port']}
        if data.get('name'):
            srv['name'] = data['name']
        if 'ssl' in data:
            srv['ssl'] = data['ssl']
        # Try to create server before saving config
        try:
            srv_tuple = self._create_http_server(srv)
        except ValueError as e:
            self._error(client, str(e), 400)
            return
        http_list.append(srv)
        self._servers.append(srv_tuple)
        self._save_config()
        self._log.info("HTTP server added")
        client.respond({'ok': True})

    def _handle_api_http_update(self, client, user, index):
        """Update HTTP server"""
        if not self._require_admin(client, user):
            return
        http_list = self._configuration.get('http', [])
        if index < 0 or index >= len(http_list):
            self._error(client, 'HTTP server not found', 404)
            return
        data = client.data
        error = self._validate_http_config(data)
        if error:
            self._error(client, error, 400)
            return
        old = _copy.deepcopy(http_list[index])
        srv = {'address': data.get('address', '0.0.0.0'), 'port': data['port']}
        if data.get('name'):
            srv['name'] = data['name']
        if 'ssl' in data:
            srv['ssl'] = data['ssl']
        # Check if restart needed (address/port/ssl changed)
        needs_restart = (
            old.get('address', '0.0.0.0') != srv.get('address', '0.0.0.0') or
            old.get('port') != srv.get('port') or
            old.get('ssl') != srv.get('ssl'))
        if needs_restart and index < len(self._servers):
            server, _ = self._servers[index]
            same_bind = (
                old.get('address', '0.0.0.0') == srv.get('address', '0.0.0.0')
                and old.get('port') == srv.get('port'))
            if same_bind:
                server.close()
                try:
                    replacement = self._create_http_server(srv)
                except ValueError as err:
                    try:
                        self._servers[index] = self._create_http_server(old)
                    except ValueError as rollback_err:
                        self._log.error(
                            "Failed to restore HTTP server %d: %s",
                            index, rollback_err)
                    self._error(client, str(err), 400)
                    return
            else:
                try:
                    replacement = self._create_http_server(srv)
                except ValueError as err:
                    self._error(client, str(err), 400)
                    return
                server.close()
            self._servers[index] = replacement
        http_list[index] = srv
        self._save_config()
        self._log.info("HTTP server updated")
        client.respond({'ok': True})

    def _handle_api_http_delete(self, client, user, index):
        """Delete HTTP server"""
        if not self._require_admin(client, user):
            return
        http_list = self._configuration.get('http', [])
        if index < 0 or index >= len(http_list):
            self._error(client, 'HTTP server not found', 404)
            return
        if len(http_list) <= 1:
            self._error(client, 'Cannot delete last HTTP server', 400)
            return
        # Close server before removing from config
        if index < len(self._servers):
            server, _ = self._servers[index]
            server.close()
            del self._servers[index]
        del http_list[index]
        self._save_config()
        self._log.info("HTTP server deleted")
        client.respond({'ok': True})
