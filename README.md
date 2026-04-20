# Ser2tcp

Simple proxy for connecting over TCP, TELNET, SSL, WebSocket or Unix socket to serial port

https://github.com/cortexm/ser2tcp

## Features

- can serve multiple serial ports using pyserial library
- each serial port can have multiple servers
- server can use TCP, TELNET, SSL, WebSocket or SOCKET protocol
  - TCP protocol just bridge whole RAW serial stream to TCP
  - TELNET protocol will send every character immediately and not wait for ENTER, it is useful to use standard `telnet` as serial terminal
  - SSL protocol provides encrypted TCP connection with optional mutual TLS (mTLS) client certificate verification
  - WebSocket protocol connects through the HTTP server with binary frames for data and JSON text frames for signal control
  - SOCKET protocol uses Unix domain socket for local IPC
- servers accepts multiple connections at one time
  - each connected client can sent to serial port
  - serial port send received data to all connected clients
- non-blocking send with configurable timeout and buffer limit
- serial signal control (RTS, DTR, CTS, DSR, RI, CD) via escape protocol or WebSocket JSON
- IP filtering with allow/deny lists (CIDR notation supported)
- built-in HTTP server with REST API for status monitoring
- web interface for viewing configured ports and connections
- web terminal clients (xterm.js VT100 terminal, raw colored view, and WebSocket tunnel terminal with endpoint selector)
- authentication with session management and API tokens
- light/dark mode web UI (follows system preference)

## Installation

```
pip install ser2tcp
```

or from source:

```
pip install .
```

or install the runtime dependencies directly:

```
pip install -r requirements.txt
```

For development:

```
pip install -e .
```

### Build from source

`ser2tcp` is packaged with `pyproject.toml`.

Build source and wheel distributions:

```bash
python -m pip install --upgrade build
python -m build
```

Build artifacts are written to `dist/`.

Install the built wheel locally:

```bash
python -m pip install dist/ser2tcp-*.whl
```

### Docker build

Build the container image from the repository root:

```bash
docker build -t ser2tcp .
```

Run it with a mounted config file:

```bash
docker run --rm \
  --network host \
  --privileged \
  -v /dev:/dev \
  -v $(pwd)/ser2tcp.yaml:/config/config.yaml:ro \
  ser2tcp
```

### Docker Compose example

A Linux-focused Compose example is included in:

- [example/docker-compose.yml](/Users/marcelochsendorf/Downloads/AutoSer2TCP/example/docker-compose.yml)
- [example/config.yaml](/Users/marcelochsendorf/Downloads/AutoSer2TCP/example/config.yaml)
- [example/README.md](/Users/marcelochsendorf/Downloads/AutoSer2TCP/example/README.md)

Compose file:

```yaml
services:
  ser2tcp:
    build:
      context: ..
      dockerfile: Dockerfile
    container_name: ser2tcp
    restart: unless-stopped
    network_mode: host
    privileged: true
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - /dev:/dev
    command: ["-v", "-c", "/config/config.yaml"]
```

Start it:

```bash
cd example
docker compose up -d --build
```

Stop it:

```bash
cd example
docker compose down
```

The included example config:

- exposes the HTTP admin UI on `0.0.0.0:20080`
- forwards `/dev/ttyUSB0` to TCP `10001`
- enables a wildcard pool for `/dev/serial/by-id/usb-*` starting at TCP `11000`

This Compose example is intended for Linux because it uses `network_mode: host`
and mounts `/dev` so serial devices and `/dev/serial/by-id/...` symlinks are
available inside the container.

### Uninstall

```
pip uninstall ser2tcp
```

## Quick start

1. Install `ser2tcp`.
2. Start it once:
   ```bash
   ser2tcp -v
   ```
3. If `~/.config/ser2tcp/config.yaml` does not exist yet, `ser2tcp` creates it automatically and starts an HTTP admin server on the first free port from `20080`.
4. Open the web UI on the configured HTTP address, or edit the YAML file directly and add `ports` or `pools`.
5. Restart `ser2tcp` after changing the config file manually.

## Command line options

```
  -h, --help            show this help message and exit
  -V, --version         show program's version number and exit
  -v, --verbose         Increase verbosity
  -u, --usb             List USB serial devices and exit
  --hash-password PASSWORD
                        Hash password for config file and exit
  -c CONFIG, --config CONFIG
                        configuration in YAML format (default: ~/.config/ser2tcp/config.yaml)
```

If no config file is specified and default config doesn't exist, creates one with HTTP server on first free port from 20080.

### Verbose

- By default print only ERROR and WARNING messages
- `-v`: will print INFO messages
- `-vv`: print also DEBUG messages

## Configuration file example

```yaml
ports:
  - serial:
      port: /dev/ttyUSB0
      baudrate: 115200
      parity: NONE
      stopbits: ONE
    servers:
      - address: 127.0.0.1
        port: 10001
        protocol: tcp
      - address: 0.0.0.0
        port: 10002
        protocol: telnet
        send_timeout: 5.0
        buffer_limit: 65536
```

JSON configuration files are no longer supported. Use `.yaml` or `.yml`.

On macOS, serial adapters usually appear as `/dev/cu.usbserial-*` or
`/dev/cu.usbmodem*`. Use `ser2tcp --usb` to see the exact device path.

### Serial configuration

`serial` structure pass all parameters to [serial.Serial](https://pythonhosted.org/pyserial/pyserial_api.html#classes) constructor from pyserial library, this allows full control of the serial port.

#### USB device matching

Instead of specifying `port` directly, you can use `match` to find device by USB attributes:

```yaml
serial:
  match:
    vid: "0x303A"
    pid: "0x4001"
    serial_number: dcda0c2004bc0000
  baudrate: 115200
```

Use `ser2tcp --usb` to list available USB devices with their attributes:

```
$ ser2tcp --usb
/dev/cu.usbmodem1101
  vid: 0x303A
  pid: 0x4001
  serial_number: dcda0c2004bc0000
  manufacturer: Espressif Systems
  product: Espressif Device
  location: 1-1
```

Match attributes: `vid`, `pid`, `serial_number`, `manufacturer`, `product`, `location`, `description`, `hwid`

- Wildcard `*` supported (e.g. `"product": "CP210*"`)

#### Wildcard serial pools

Use `pools` to discover devices from a filesystem glob and auto-assign TCP
ports starting at `start_port`. When the same identity reappears, it keeps the
same assigned TCP port.

```yaml
pools:
  - name: USB adapters
    enabled: true
    serial:
      glob: /dev/serial/by-id/usb-*
      baudrate: 115200
    server:
      address: 0.0.0.0
      start_port: 11000
      send_timeout: 5.0
      buffer_limit: 65536
      max_connections: 2
    assignments:
      - identity: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0
        port: 11000
        enabled: true
        name: console-a
    ignored_identities: []
```

- Pools are TCP-only.
- Discovery is based on filesystem globbing, for example `/dev/serial/by-id/usb-*`.
- New devices are assigned the next free TCP port starting from `start_port`.
- Removing an assignment from the web UI stores its identity in `ignored_identities`
  so it is not recreated automatically on the next scan.
- Matching is case-insensitive
- Error if multiple devices match the criteria
- Device is resolved when client connects, not at startup (device does not need to exist at startup)
- `baudrate` is optional (default 9600, CDC devices ignore it)

### Server configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `address` | Bind address (IP for tcp/telnet/ssl, path for socket) | required* |
| `port` | TCP port (not used for socket/websocket) | required* |
| `protocol` | `tcp`, `telnet`, `ssl`, `websocket` or `socket` | required |
| `endpoint` | WebSocket URL path (websocket only), must be unique | required* |
| `token` | Per-server auth token (websocket only) | - |
| `ssl` | SSL configuration (required for `ssl` protocol) | - |
| `data` | Forward serial data (default true), `false` = control-only | true |
| `control` | Signal control configuration | - |
| `send_timeout` | Disconnect client if data cannot be sent within this time (seconds) | 5.0 |
| `buffer_limit` | Maximum send buffer size per client (bytes), `null` for unlimited | null |
| `max_connections` | Maximum clients per server (0 = unlimited) | 0 |

\* `address`/`port` required for tcp/telnet/ssl; `address` for socket; `endpoint` for websocket

#### Port-level connection limit

You can also limit total connections across all servers on a port:

```yaml
ports:
  - max_connections: 10
    serial:
      port: /dev/ttyUSB0
    servers:
      - protocol: tcp
        address: 0.0.0.0
        port: 10001
        max_connections: 5
      - protocol: websocket
        endpoint: device
```

- Port-level `max_connections`: limits total clients across all servers (default 0 = unlimited)
- Server-level `max_connections`: limits clients on that specific server (default 0 = unlimited)
- Both limits are checked — if either is reached, new connections are rejected

#### WebSocket configuration

WebSocket connections go through the HTTP server — no separate listening port needed:

```yaml
protocol: websocket
endpoint: my-device
control:
  rts: true
  signals: [rts, dtr, cts, dsr]
```

- Accessible at `ws://host:port/ws/my-device` (or `wss://` for HTTPS)
- Available on all configured HTTP servers
- Binary frames carry raw serial data (bidirectional)
- Text frames carry JSON control messages: `{"rts": true}`, `{"signals": {...}}`
- Signal state sent automatically on connect, then only on change
- Auth: per-server `token`, global user session, or both accepted
- Web terminals available at `/xterm/<endpoint>` (VT100) and `/raw/<endpoint>` (colored hex)
- Tunnel terminal available at `/webserial.html` — fetches all configured WebSocket endpoints and lets the user select and connect to one via WebSocket through the server

#### Socket configuration

For `socket` protocol, `address` is the path to the Unix domain socket:

```yaml
address: /tmp/ser2tcp.sock
protocol: socket
```

- Socket file is created on startup and removed on shutdown
- If socket file already exists, it is replaced
- Connect with: `socat - UNIX-CONNECT:/tmp/ser2tcp.sock`
- Not available on Windows

#### SSL configuration

For `ssl` protocol, add `ssl` object with certificate paths:

```yaml
address: 0.0.0.0
port: 10003
protocol: ssl
ssl:
  certfile: /path/to/server.crt
  keyfile: /path/to/server.key
  ca_certs: /path/to/ca.crt
```

| Parameter | Description | Required |
|-----------|-------------|----------|
| `certfile` | Server certificate (PEM) | yes |
| `keyfile` | Server private key (PEM) | yes |
| `ca_certs` | CA certificate for client verification (mTLS) | no |

If `ca_certs` is specified, clients must provide a valid certificate signed by the CA.

#### IP filtering

Restrict client connections by IP address using `allow` and/or `deny` lists:

```yaml
address: 0.0.0.0
port: 10001
protocol: tcp
allow: [192.168.1.0/24, 10.0.0.5]
deny: [192.168.1.100]
```

| Parameter | Description |
|-----------|-------------|
| `allow` | List of allowed IP addresses/networks (CIDR notation supported) |
| `deny` | List of denied IP addresses/networks (CIDR notation supported) |

Filter logic:
- **No config**: all IPs allowed
- **Only `deny`**: all IPs allowed except those in deny list
- **Only `allow`**: only IPs in allow list are allowed
- **Both**: deny takes precedence, then allow list is checked

Works on TCP, TELNET, SSL, WebSocket and HTTP servers. Not applicable to Unix socket (no IP addresses). Rejected connections are logged.

##### Creating self-signed certificates

Generate CA and server certificate for testing:

```bash
# Create CA key and certificate
openssl genrsa -out ca.key 2048
openssl req -new -x509 -days 365 -key ca.key -out ca.crt -subj "/CN=ser2tcp CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign"

# Create server key and certificate signing request
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=localhost"

# Sign server certificate with CA
openssl x509 -req -days 365 -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt

# For certificate bound to specific domain/IP (SAN - Subject Alternative Name):
openssl req -new -key server.key -out server.csr -subj "/CN=myserver.example.com" -addext "subjectAltName=DNS:myserver.example.com,DNS:localhost,IP:192.168.1.100"
openssl x509 -req -days 365 -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -copy_extensions copy

# Clean up CSR
rm server.csr
```

For mTLS (mutual TLS with client certificates):

```bash
# Create client key and certificate
openssl genrsa -out client.key 2048
openssl req -new -key client.key -out client.csr -subj "/CN=client"
openssl x509 -req -days 365 -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out client.crt
rm client.csr
```

Testing SSL connection:

```bash
# Without client certificate
openssl s_client -connect localhost:10003

# With client certificate (mTLS)
openssl s_client -connect localhost:10003 -cert client.crt -key client.key
```

### HTTP server and API

Optional HTTP server for monitoring and management:

```yaml
http:
  - name: main
    address: 0.0.0.0
    port: 8080
```

- `name`: optional label for the server (displayed in web UI Settings tab)
- HTTP servers can be added/removed/modified via web UI without restart

With authentication (configured at root level, shared across all HTTP servers):

```yaml
http:
  - address: 0.0.0.0
    port: 8080
users:
  - login: admin
    password: sha256:...
    admin: true
tokens:
  - token: my-api-key
    name: monitoring
    admin: false
session_timeout: 3600
```

- `users`: login credentials with optional `admin` flag and per-user `session_timeout`
- `tokens`: permanent API tokens for automation (no expiration)
- `session_timeout`: global default session timeout in seconds
- First user added (via CLI or web UI) is automatically admin
- Cannot delete last admin (user or token) — at least one admin must exist

Generate password hash:

```bash
ser2tcp --hash-password mysecretpassword
```

HTTPS with SSL:

```yaml
http:
  - address: 0.0.0.0
    port: 8080
  - address: 0.0.0.0
    port: 8443
    ssl:
      certfile: server.crt
      keyfile: server.key
```

With IP filtering:

```yaml
http:
  - address: 0.0.0.0
    port: 8080
    allow: [192.168.0.0/16]
    deny: [192.168.1.100]
```

#### API endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/login` | no | Authenticate, returns session token |
| POST | `/api/logout` | no | Invalidate session |
| GET | `/api/status` | yes | Runtime status (serial ports, servers, connections) |
| GET | `/api/detect` | yes | Available serial ports with USB/device attributes |
| GET | `/api/signals` | yes | Signal states for all ports |
| GET | `/api/settings` | yes | Get settings (http servers, session_timeout) |
| DELETE | `/api/ports/<p>/connections/<s>/<c>` | yes | Disconnect client |
| POST | `/api/ports` | admin | Add new port configuration |
| PUT | `/api/ports/<index>` | admin | Update port configuration |
| DELETE | `/api/ports/<index>` | admin | Delete port configuration |
| PUT | `/api/ports/<index>/signals` | admin | Set RTS/DTR signals |
| GET | `/api/users` | admin | List users |
| POST | `/api/users` | admin | Add user |
| PUT | `/api/users/<login>` | admin | Update user |
| DELETE | `/api/users/<login>` | admin | Delete user |
| GET | `/api/tokens` | admin | List API tokens |
| POST | `/api/tokens` | admin | Add API token |
| PUT | `/api/tokens/<token>` | admin | Update API token |
| DELETE | `/api/tokens/<token>` | admin | Delete API token |
| PUT | `/api/settings` | admin | Update session_timeout |
| POST | `/api/settings/http` | admin | Add HTTP server |
| PUT | `/api/settings/http/<index>` | admin | Update HTTP server |
| DELETE | `/api/settings/http/<index>` | admin | Delete HTTP server |
| GET | `/xterm/<endpoint>` | no | WebSocket VT100 terminal |
| GET | `/raw/<endpoint>` | no | WebSocket raw terminal |
| GET | `/webserial.html` | no | WebSocket tunnel terminal (endpoint selector) |

Auth levels: `no` = public, `yes` = any authenticated user, `admin` = admin user/token only.

Authentication: `Authorization: Bearer <token>` header or `?token=<token>` query parameter. Without users/tokens configured, all endpoints are accessible without authentication.

## Usage examples

Run installed command:

```
ser2tcp -c ser2tcp.yaml
```

Direct running from repository:

```
python run.py -c ser2tcp.yaml
```

Use a custom config path:

```bash
ser2tcp -v -c /etc/ser2tcp.yaml
```

Open the web interface:

```text
http://127.0.0.1:20080/
```

The exact port depends on your `http` configuration, or on the first free port
chosen when the default config is created.

### Connecting using telnet

```
telnet localhost 10002
```

(to exit telnet press `CTRL + ]` and type `quit`)

### Connecting to a TCP serial port

```bash
nc 127.0.0.1 10001
```

### Using wildcard pools

1. Add a pool to `pools:` in `config.yaml`, or create it from the web UI.
2. Set `serial.glob`, for example `/dev/serial/by-id/usb-*`.
3. Set `server.start_port`, for example `11000`.
4. Start or restart `ser2tcp`.
5. Each detected device gets a TCP port starting at `start_port`, and the same
   device identity keeps the same TCP port when it reconnects.

## Autostart and services

### Linux - systemd user service

Use this when `ser2tcp` should run as your user account.

1. Install `ser2tcp` so the `ser2tcp` command is available to your user.
2. Create the user service directory and copy the unit file:
    ```
    mkdir -p ~/.config/systemd/user
    cp ser2tcp.service ~/.config/systemd/user/
    ```
3. If `ser2tcp` is installed in a virtual environment or a non-default path,
   edit `~/.config/systemd/user/ser2tcp.service` and adjust `ExecStart`.
4. Configuration file will be created automatically at
   `~/.config/ser2tcp/config.yaml` on first run.
5. Reload user systemd services:
    ```
    systemctl --user daemon-reload
    ```
6. Enable autostart and start it now:
    ```
    systemctl --user enable --now ser2tcp
    ```
7. Check status and logs:
   ```bash
   systemctl --user status ser2tcp
   journalctl --user-unit ser2tcp -e
   ```
8. By default a user service starts after login. To keep it running after boot
   without an interactive login, enable linger:
    ```
    sudo loginctl enable-linger $USER
    ```

This is the simplest Linux autostart setup for a desktop or single-user system.

### Linux - Docker Compose autostart

If you prefer to run `ser2tcp` in a container on Linux, use the Compose example
described above in **Docker Compose example** and in
[example/README.md](/Users/marcelochsendorf/Downloads/AutoSer2TCP/example/README.md).

Notes:

- The example uses `network_mode: host`, which is the simplest way to expose
  both fixed TCP listeners and dynamically assigned pool ports on Linux.
- The example mounts `/dev:/dev` and uses `privileged: true` so
  `/dev/serial/by-id/...` and device nodes are visible inside the container.
- Edit [example/config.yaml](/Users/marcelochsendorf/Downloads/AutoSer2TCP/example/config.yaml)
  to match your actual serial devices and TCP port layout.

### Linux - systemd system service

Use this when `ser2tcp` should start automatically for the machine at boot,
independent of a user login.

1. Install `ser2tcp` system-wide, or note the full path to the executable you
   want the service to use.
2. Create a system user with access to serial devices:
    ```
    sudo useradd -r -s /usr/sbin/nologin -G dialout ser2tcp
    ```
3. Copy the service file:
    ```
    sudo cp ser2tcp-system.service /etc/systemd/system/ser2tcp.service
    ```
4. If needed, edit `/etc/systemd/system/ser2tcp.service` and update
   `ExecStart` to the correct `ser2tcp` path.
5. Create configuration file `/etc/ser2tcp.yaml`.
6. Reload systemd and enable autostart:
    ```
    sudo systemctl daemon-reload
    sudo systemctl enable --now ser2tcp
    ```
7. Check status and logs:
   ```bash
   sudo systemctl status ser2tcp
   sudo journalctl -u ser2tcp -e
   ```

If your distribution uses another serial-device group instead of `dialout`,
adjust the service user accordingly.

### macOS - launchd user agent

1. Copy the launch agent template:
    ```
    mkdir -p ~/Library/LaunchAgents
    cp ser2tcp-macos.plist ~/Library/LaunchAgents/com.github.cortexm.ser2tcp.plist
    ```
2. If `ser2tcp` is not on your login shell `PATH`, edit the copied plist and
   replace `ser2tcp` with the full path from `command -v ser2tcp`.
3. Configuration file will be created automatically at
   `~/.config/ser2tcp/config.yaml` on first run.
4. Load and start the agent:
    ```
    launchctl unload ~/Library/LaunchAgents/com.github.cortexm.ser2tcp.plist 2>/dev/null || true
    launchctl load -w ~/Library/LaunchAgents/com.github.cortexm.ser2tcp.plist
    ```
5. Check agent status:
    ```
    launchctl print gui/$(id -u)/com.github.cortexm.ser2tcp
    ```
6. Stop and disable the agent:
    ```
    launchctl unload -w ~/Library/LaunchAgents/com.github.cortexm.ser2tcp.plist
    ```

### Useful commands

```bash
# Check status
systemctl --user status ser2tcp

# View logs
journalctl --user-unit ser2tcp -e

# Restart
systemctl --user restart ser2tcp

# Stop
systemctl --user stop ser2tcp
```

For system service, use `sudo systemctl` instead of `systemctl --user`.

macOS useful commands:

```bash
# Check launch agent status
launchctl print gui/$(id -u)/com.github.cortexm.ser2tcp

# View recent logs
log show --last 10m --predicate 'process == "ser2tcp"'

# Follow logs
log stream --predicate 'process == "ser2tcp"'
```

## Requirements

- Python 3.8+
- pyserial 3.0+
- uhttp-server 2.3.2+ (for HTTP/API and WebSocket)
- PyYAML 6.0+ (for YAML configuration)

### Running on

- Linux
- macOS
- Windows

## Credits

(c) 2016-2026 by Pavel Revak

### Support

- Basic support is free over GitHub issues.
- Professional support is available over email: [Pavel Revak](mailto:pavel.revak@gmail.com?subject=[GitHub]%20ser2tcp).
