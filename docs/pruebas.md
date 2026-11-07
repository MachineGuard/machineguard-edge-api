# Pruebas

## Pruebas locales

Instala las dependencias de prueba en un venv nuevo, con hashes verificados:

```bash
python -m venv .venv
.venv\Scripts\activate        # Linux/macOS: source .venv/bin/activate
pip install --require-hashes -r requirements-test.txt
```

Ejecuta todas las pruebas:

```bash
python -m pytest
```

Incluye las pruebas unitarias, las del transporte hacia Core y los escenarios BDD.

### Escenarios BDD

Los criterios de aceptación de TS02, TS03 y US03 están escritos en Gherkin, en
español, en `tests/features`. Se ejecutan con pytest-bdd desde
`tests/test_bdd_acceptance.py`.

Para generar el reporte en formato Cucumber:

```bash
mkdir reports
python -m pytest --cucumberjson=reports/cucumber.json
```

## Integración continua (GitHub Actions)

El workflow `.github/workflows/ci.yml` se ejecuta en cada push a `develop`, `main`,
`feature/**`, `release/**` y `hotfix/**`, y en cada pull request a `develop` y
`main`. Incluye:

- Las pruebas unitarias y BDD.
- Los controles de seguridad: búsqueda de secretos y de dependencias vulnerables.
- Un job con PostgreSQL real y Core, que requiere acceso de lectura al repositorio
  compañero. Ver [integración con Core y CI](../scripts/ci.md).

## Prueba con Core real

`tests/test_live_core.py` usa la API real de IAM, configuración, gateways,
mediciones y atención de incidentes.

1. Prepara un Core y una base PostgreSQL **aislados de producción**.
2. Define estas variables solo en el entorno del proceso de prueba:
   - `MACHINEGUARD_CORE_URL`
   - `MACHINEGUARD_CORE_ADMIN_EMAIL`
   - `MACHINEGUARD_CORE_ADMIN_PASSWORD`
3. Ejecuta:

```bash
python -m pytest tests/test_live_core.py
```

Las pruebas crean zonas y sensores con nombres E2E, emiten credenciales limitadas
y prueban calibración, lectura real, reconocimiento, acción y resolución de
incidentes, pérdida de confirmación y revocación con recuperación del buffer.
Al terminar revocan las credenciales de prueba.

Sin esas variables, estas pruebas se omiten de forma explícita. El resto de la
suite funciona sin ellas.

**No las ejecutes sobre una organización con datos reales.**
