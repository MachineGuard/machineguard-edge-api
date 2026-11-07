# Integración con Core y CI

Esta guía prepara la prueba completa de Edge con Core y PostgreSQL real. El job
de GitHub Actions "PostgreSQL real y Edge-Core" hace lo mismo.

## Preparar

- Instala `requirements-test.txt` con `--require-hashes` en un venv nuevo.
- Necesitas Java 21, Maven y los dos repositorios (Core y Edge) en las versiones aprobadas.
- Prepara un PostgreSQL desechable en loopback (127.0.0.1), con bases vacías
  `mgci` y `mgci_test`, un usuario de prueba y sin datos reales.
- Define estas variables:

```text
DB_URL=jdbc:postgresql://127.0.0.1:PUERTO/mgci
MACHINEGUARD_TEST_DB_URL=jdbc:postgresql://127.0.0.1:PUERTO/mgci_test
DB_USERNAME=...
DB_PASSWORD=...
```

El subconjunto de PostgreSQL usa autenticación `trust`, pero solo en ese clúster
de prueba local. No reutilices una base existente.

## Ejecutar

```text
python scripts/edge-core-integration.py --core ../machineguard-core-api --edge . --report RUTA_FUERA_DE_GIT
```

El runner hace esto:

- Ejecuta Core completo y repite el subconjunto de PostgreSQL.
- Inicia Core y ejecuta Edge completo, sin permitir pruebas live omitidas.
- Ensaya ThingSpeak con doble loopback.
- Genera sesiones sintéticas con IAM real y detiene solo su propio Core.
  PostgreSQL lo detiene quien lo preparó.

Opciones:

- `--maven` indica el ejecutable de Maven.
- `--core-sha` y `--edge-sha` solo se usan con copias Git archive comprobadas.
  Normalmente el runner lee los SHA del checkout.

Los reportes de los subprocesos se generan en un área temporal privada y se
limpian de credenciales antes de copiarse a `--report`. Ocurre también si la
prueba falla o vence su tiempo. Incluye los reportes de Surefire/Cucumber y JUnit de Edge.
Maven normal conserva sus reportes predeterminados. Se conservan los códigos de
salida y los diagnósticos, sin credenciales.

## En GitHub Actions

- Antes de ejecutar, publica las ramas aprobadas y fija en el workflow de Core el
  SHA de Edge.
- El job necesita el secreto `MACHINEGUARD_CI_READ_TOKEN`, con acceso de solo
  lectura al repositorio privado compañero. El token automático de GitHub no
  da acceso a otro repositorio.
- Los scripts no crean ese secreto ni cambian la configuración del repositorio.
- No se usa `pull_request_target`.

Los controles de la CI consultan versiones de dependencias en OSV y buscan
patrones de secretos en los archivos versionados. Fallan si encuentran un
hallazgo o si no completan la consulta. No son una auditoría completa del
historial de Git. Solo se envían nombres y versiones de paquetes a OSV, no código.

Limitaciones:

- Las acciones de GitHub están fijadas por etiqueta (tag). Fijarlas por SHA es
  pendiente: proponer los SHA verificados antes de cambiarlas.
- Branch protection, revisiones obligatorias, checks requeridos, Dependabot y
  secret scanning se configuran a mano en GitHub. Los scripts no los modifican.
- La CI de GitHub y los contenedores solo se dan por comprobados cuando corren
  allí. Una prueba en Windows no los sustituye.
