# Docker

Edge se puede ejecutar en un contenedor con Docker Desktop. La imagen usa
Python 3.12 slim, un usuario sin privilegios (UID 10001) y Waitress
(`python -m edge_api.server`).

## Construir

```bash
docker build -t machineguard-edge:local .
```

## Configurar

Copia la guía y completa los valores privados:

```bash
cp .env.example .env
```

- `EDGE_ADMIN_KEY` es obligatoria: al menos 32 bytes aleatorios.
- `.env` no va a Git (ya está en `.gitignore`). No pongas claves en el Dockerfile ni en el compose.
- El contenedor sobrescribe `EDGE_DATABASE_PATH=/data/edge.db`,
  `EDGE_BIND_HOST=0.0.0.0` y `EDGE_PORT=5000`.
- Dentro del contenedor, `127.0.0.1` es el propio contenedor. Si Core corre en la
  laptop, usa `MACHINEGUARD_API_URL=http://host.docker.internal:8080`, no `localhost`.

## Ejecutar con Compose

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f edge
docker compose down
```

- La base SQLite vive en el volumen `edge-data`. `docker compose down` lo conserva;
  `docker compose down -v` borra los datos de Edge.
- El puerto se publica solo en `127.0.0.1:5000` por defecto. Cambia `EDGE_HOST_PORT`
  si ese puerto está ocupado. Para que los nodos ESP32 entren desde la red, cambia
  la IP del puerto por la de la laptop; eso exige revisar HTTPS y la red del cliente
  (ver `docs/configuracion.md`).
- Usa un solo contenedor por volumen: Edge admite un solo proceso por archivo de base.

## Salud

`/api/v1/edge/health` es administrativo: pide `X-Edge-Admin-Key` y solo acepta
loopback o IPs listadas en `EDGE_ADMIN_ALLOWED_IPS`.

- El healthcheck del contenedor consulta desde dentro (loopback) con la clave del
  entorno. Mira el estado con `docker compose ps` o
  `docker inspect --format '{{.State.Health.Status}}' machineguard-edge-api-edge-1`.
- Desde Windows, la petición llega desde la puerta de enlace de la red de Compose.
  Obtén esa IP con `docker network inspect machineguard-edge-api_default` y añádela a
  `EDGE_ADMIN_ALLOWED_IPS` (en la prueba fue `172.19.0.1`). Sin ella, responde 403.
  Esa IP puede cambiar al recrear la red; si cambia, actualiza la lista.

## Limpieza

```bash
docker compose down
docker image rm machineguard-edge:local
```
