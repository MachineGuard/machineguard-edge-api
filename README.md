# MachineGuard Edge API

Edge es el gateway local de MachineGuard. Corre en una Raspberry Pi o equivalente,
dentro de la red del cliente. Recibe las lecturas de temperatura y humedad de los
nodos ESP32, descarta las que son físicamente imposibles y corrige cada una con la
calibración de su nodo. Luego envía las válidas a la API central (Core). Si no hay
conexión, las guarda en una base de datos local y las envía en orden cuando vuelve.

Usa Flask, Peewee y SQLite.

## Arrancar en local

```bash
python -m venv .venv
.venv\Scripts\activate        # Linux/macOS: source .venv/bin/activate
pip install --require-hashes -r requirements-test.txt
# Define antes las variables de entorno (ver docs/configuracion.md).
python run.py                 # http://127.0.0.1:5000
```

Como mínimo define `EDGE_ADMIN_KEY`, una clave aleatoria de 32 bytes o más.
`.env.example` es solo una guía: el programa no lo carga solo.

## Probar

```bash
python -m pytest
```

Debe terminar sin fallos. Las pruebas contra un Core real se omiten si no
defines sus variables. Detalle en [docs/pruebas.md](docs/pruebas.md).

## Cómo encaja

```text
ESP32 (Embedded)  ->  Edge (ESTE REPOSITORIO)  ->  Core (API central)  ->  Web
  nodos              gateway local, en la red       reglas y datos        panel
```

Los nodos solo hablan con Edge. Edge habla con Core con su propia credencial de gateway.

## Estructura

```text
edge_api/          Código: domain, application, infrastructure e interfaces
thingspeak_child/  Proceso secundario que publica en ThingSpeak (opcional)
tests/             Pruebas con pytest; los escenarios BDD están en tests/features
scripts/           Integración con Core y comprobaciones de seguridad para CI
run.py             Arranque para desarrollo
.env.example       Guía de variables de entorno (no se carga sola)
requirements*.txt  Dependencias con hashes: ejecución (.txt) y pruebas (-test.txt)
```

## Más detalle

- [Configuración](docs/configuracion.md): acceso, variables, credenciales de nodos y límites de datos.
- [Arquitectura y reglas](docs/arquitectura.md): capas, reglas de negocio, entrega y recuperación.
- [API](docs/api.md): endpoints y códigos de respuesta.
- [Ejecución y ThingSpeak](docs/ejecucion-y-thingspeak.md): ejecutar en Windows, rotar la credencial y el canal opcional de ThingSpeak.
- [Pruebas](docs/pruebas.md): pytest, escenarios BDD y prueba con Core real.
- [Integración con Core y CI](scripts/ci.md): PostgreSQL real y scripts de integración.
