# Ejecución, credencial y ThingSpeak

## Ejecutar en Windows

Usa Python 3.12 y Waitress. Crea un venv nuevo e instala el lock de ejecución:

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install --require-hashes -r requirements.txt
& ./.venv/Scripts/python.exe -m edge_api.server
```

- Escucha en `EDGE_BIND_HOST` y `EDGE_PORT` (127.0.0.1:5000 por defecto), con
  `EDGE_WSGI_THREADS` hilos (cuatro por defecto).
- Ctrl+C detiene el servidor y el runtime. Para reiniciar, usa el mismo archivo
  SQLite (`EDGE_DATABASE_PATH`).
- Usa **un solo proceso por archivo de base**.
- `run.py` es para desarrollo.
- Desde Python, el runtime se detiene con `app.config['services'].runtime.stop()`.
  `run.py` lo hace al salir.

### Nota para quien lance Edge desde otro programa

En Windows, el proceso que lanza Edge **no debe leer stdin a través de una
tubería redirigida**. Un hilo bloqueado en esa lectura impide que el proceso
hijo de ThingSpeak publique. Para detener Edge, usa una parada por archivo o por
señal. Esto se observó en el arnés de pruebas real; el mecanismo interno de
Windows no está demostrado.

## Credencial del gateway: vencimiento y rotación

Edge muestra `credentialExpiry` en `/health`. Copia `expiresAt` de los metadatos
de Core en `EDGE_DEVICE_CREDENTIAL_EXPIRES_AT`. Si falta, se muestra `UNKNOWN`.
El aviso empieza siete días antes; cámbialo con `EDGE_CREDENTIAL_WARNING_SECONDS`.
La fecha la configura el operador: no se deduce del secreto ni reemplaza la
validación de Core.

Para rotar la credencial:

1. Un ADMIN con sesión vigente rota la credencial con
   `POST /api/v1/gateways/{id}/credentials` en Core.
2. Detén Edge. En su entorno cambia `EDGE_DEVICE_CREDENTIAL` y `expiresAt` por
   los de esa respuesta. Conserva el archivo SQLite. No copies secretos a logs,
   nodos ni Git.
3. Reinicia y consulta el health y el buffer. El worker reconcilia los pendientes solo.

Si la credencial venció, Edge conserva la cola. Si el gateway fue revocado,
registra uno nuevo con los sensores permitidos y actualiza también `EDGE_GATEWAY_ID`.

## ThingSpeak (opcional)

Está apagado por defecto. Para activarlo, define `THINGSPEAK_ENABLED=true`,
`THINGSPEAK_DEVICE_CODE`, `THINGSPEAK_CHANNEL_ID` y `THINGSPEAK_WRITE_KEY`
(ver `.env.example`).

- Usa un canal dedicado a ese nodo y a esta instancia de Edge, sin otros escritores.
- No cambies el nodo ni la ventana de un canal ya registrado. Para otra
  selección, usa otro canal.
- La clave viaja solo en la cabecera HTTPS. No llega al firmware.

### Qué envía

El proceso secundario lee las muestras válidas ya guardadas desde que se activa.
Agrupa por hora de recepción UTC, en ventanas de al menos 20 s:

- `field1`: temperatura media calibrada, en °C.
- `field2`: humedad media, en % HR.
- `field3`: cantidad de muestras.

No rellena muestras ausentes ni reemplaza los originales enviados a Core. Los
valores y los IDs de origen se congelan al cerrar cada ventana.

La licencia gratuita es no comercial y exige al menos 15 s por canal. Edge espera
una ventana entre intentos, también al recuperar pendientes.
[Límites oficiales de ThingSpeak](https://thingspeak.mathworks.com/pages/license_faq).

### Confirmación y `UNCERTAIN`

- Un ID positivo completo confirma el envío en ThingSpeak. `0` deja el envío pendiente.
- Si la respuesta se pierde, ThingSpeak pudo haber guardado el dato. En ese caso
  el envío queda en `UNCERTAIN` y **no se reintenta solo**.
- Un envío secundario nunca confirma nada en Core.
- La base conserva los envíos al reiniciar. Crece con el historial: no hay purga automática.

### Revisar un envío `UNCERTAIN`

1. Busca en el canal el identificador `mg:<id>` del payload.
2. Resuelve el envío con `POST /thingspeak/jobs/{id}/reconcile`:
   - Si lo encontraste: `{"action":"confirmed","entryId":123}`
   - Para descartarlo: `{"action":"abandon"}`
   - Para reintentar a propósito: `{"action":"retry","acceptDuplicateRisk":true}`.
     Puede duplicar el registro remoto.

Consulta la lista con `GET /thingspeak/jobs?state=UNCERTAIN&limit=50&offset=0`
y las pérdidas con `GET /thingspeak/losses`, con la clave administrativa.
La capacidad limita las ventanas pendientes. Si se desborda, queda el motivo y
las fuentes, sin borrar la evidencia primaria.

### Observación en `/health`

`thingspeak.lastObservation` muestra el último intento y las fases observadas,
con tiempos relativos e IDs locales de intento y de trabajo. Vive solo en
memoria. `UNKNOWN` significa que la fase no se observó. Que HTTP haya empezado no
prueba que DNS o TLS hayan terminado. Los logs no incluyen claves ni payloads.
El progreso no confirma un envío.

### Límites

- Un canal real y la aceptación docente se comprueban aparte. Las pruebas
  locales usan un doble HTTP.
- ThingSpeak no resuelve los pendientes de NTP/NVS, Wi-Fi virtual, IO del ESP32,
  Docker ni hardware.
