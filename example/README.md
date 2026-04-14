# Docker Example

This example is intended for Linux hosts where serial devices are available
under `/dev`, including `/dev/serial/by-id/...`.

## Files

- `docker-compose.yml`: example Compose stack
- `config.yaml`: example `autoserial2tcp` configuration

## What it does

- exposes the HTTP admin UI on `0.0.0.0:20080`
- forwards `/dev/ttyUSB0` to TCP `10001`
- enables a wildcard pool for `/dev/serial/by-id/usb-*` starting at TCP `11000`

## Start

```bash
cd example
docker compose up -d --build
```

## Stop

```bash
cd example
docker compose down
```

## Notes

- This example uses `network_mode: host`, so it is intended for Linux only.
- This example uses `privileged: true` and mounts `/dev:/dev` so wildcard
  device paths and by-id symlinks are visible in the container.
- If you want a tighter setup, replace the full `/dev` mount with explicit
  `devices:` entries for the exact serial devices you want to expose.
