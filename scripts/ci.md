# Integración local y CI

Instala `requirements-test.txt` con `--require-hashes` en un venv nuevo. Se necesitan
Java 21, Maven y ambos checkouts aprobados. Prepara PostgreSQL desechable en loopback
con bases vacías `mgci` y `mgci_test`, usuario de pruebas y sin datos reales.

Define `DB_URL=jdbc:postgresql://127.0.0.1:PUERTO/mgci`,
`MACHINEGUARD_TEST_DB_URL=jdbc:postgresql://127.0.0.1:PUERTO/mgci_test`,
`DB_USERNAME` y `DB_PASSWORD`. El subconjunto PostgreSQL usa autenticación trust
solo en ese cluster de prueba local. No reutilices una base existente.

```text
python scripts/edge-core-integration.py --core ../machineguard-core-api --edge . --report RUTA_FUERA_DE_GIT
```

El runner ejecuta Core completo, repite el subconjunto PostgreSQL, inicia Core,
ejecuta Edge completo sin permitir live omitidas y ensaya ThingSpeak con doble
loopback. Genera sesiones sintéticas con IAM real y detiene únicamente su Core.
PostgreSQL lo detiene quien lo preparó. `--maven` permite indicar el ejecutable.
Los SHA suministrados con `--core-sha`/`--edge-sha` se reservan a copias Git archive
comprobadas; normalmente se leen del checkout.

Antes de ejecutar GitHub Actions, Pedro debe publicar las ramas aprobadas y fijar
el SHA compañero de Edge en el workflow de Core. Los jobs necesitan
`MACHINEGUARD_CI_READ_TOKEN`, de solo lectura al repositorio privado compañero;
el token automático no concede acceso a otro repositorio. No se crea ese secreto
ni se cambian settings desde los scripts. No se usa pull_request_target.

Los gates consultan versiones de dependencias en OSV y buscan patrones de secretos
en archivos versionados/sin ignorar. Fallan si detectan hallazgos o no completan la
consulta; no son una auditoría completa del historial. No envían código a OSV,
solo nombres/versiones de paquetes. Actions siguen por tag: proponer pines SHA
verificados antes de publicar. Branch protection, reviews obligatorias, required
checks, Dependabot y secret scanning requieren configuración posterior de Pedro;
no se modifican automáticamente. CI GitHub y contenedores quedan sin acreditar
hasta ejecutar allí; una prueba Windows no los sustituye.
