# Arquitectura y reglas

Edge es el contexto **Edge Processing** de MachineGuard: lo que pasa con las
lecturas dentro del gateway, antes de llegar a Core.

## Capas

El código se organiza en cuatro capas (estilo DDD: las reglas del negocio van
separadas de la tecnología):

| Capa | Qué contiene |
|---|---|
| `domain` | Reglas del negocio, sin depender de Flask ni de la base |
| `application` | Casos de uso que coordinan las reglas |
| `infrastructure` | SQLite con Peewee, cliente HTTP hacia Core y procesos de fondo |
| `interfaces` | Rutas HTTP de Flask (`/api/v1/edge`) |

## Términos

- **Calibration Offset**: valor fijo que se suma a la medición de un nodo para
  corregir su sesgo.
- **Calibration Profile**: los offsets de un nodo y su historial de cambios.
- **Sampling Interval**: cada cuántos segundos mide un nodo.
- **Local Buffer**: cola en SQLite con las lecturas que aún no llegaron a Core.
- **Cuarentena**: lecturas que Core rechaza por un problema de datos. Quedan
  apartadas hasta que las revises.
- **Idempotencia**: repetir la misma operación no duplica datos.

## Reglas de negocio

- La lectura conserva la hora de captura del nodo, no la hora del envío.
- Una lectura fuera del rango físico del sensor se descarta antes de calibrarse
  y nunca se publica.
- La lectura calibrada también debe estar en el rango físico. Un descarte no
  devuelve el nodo a ONLINE.
- Solo se publican lecturas en estado `CALIBRATED`. Sin conexión, se guardan en
  el Local Buffer.
- Mientras haya lecturas pendientes, las nuevas también se guardan en el buffer.
  Así no se rompe el orden cronológico.
- La sincronización envía lotes en orden cronológico ascendente. Una lectura
  `SYNCED` no se reenvía.
- Si el buffer se llena, la lectura se conserva y queda un registro de pérdida.
  Ese registro es distinto de las lecturas descartadas por implausibles.
- OFFLINE se calcula con la última recepción válida y un margen del 25 % del
  intervalo (`EDGE_OFFLINE_GRACE_RATIO`). Las capturas futuras, los reintentos y
  las lecturas implausibles no alargan esa ventana.

## Entrega y recuperación

- Cada muestra se guarda en SQLite antes de responder. La respuesta indica
  `persistence=LOCAL_COMMITTED`: ya está guardada en Edge.
- `delivery=CONFIRMED` significa además que Core devolvió un recibo válido.
  `delivery=PENDING` significa que el envío sigue pendiente.
- Edge reintenta solo, con espera creciente. También lo hace después de
  reiniciar, usando el mismo archivo SQLite.
- Un conflicto o un valor rechazado va a cuarentena. Un problema de
  configuración pausa solo el nodo afectado; los nodos pausados se reintentan
  solos.
- Para revisar, consulta `/health` y `/quarantine?limit=50&offset=0` con la
  clave administrativa. Cuando corrijas la causa en Core, puedes pedir
  `POST /quarantine/{id}/reprocess`. Conserva la muestra, la calibración y el
  historial.
- `/buffer/sync` pide un ciclo de sincronización acotado. Si ya hay otro envío
  en curso, responde `busy=true`.
- Si se supera `EDGE_BUFFER_CAPACITY`, las pendientes más antiguas que no estén
  en envío pasan a `DROPPED_OVERFLOW`, con un registro de pérdida. La evidencia
  sigue en SQLite: la capacidad limita las pendientes, no el tamaño total de la base.

## Contrato con Core

El gateway se autentica con la cabecera `X-Device-Credential`, sin Bearer, y publica:

- `POST {MACHINEGUARD_API_URL}/api/v1/measurements` con
  `{deviceCode, temperature, humidity, capturedAt}`
- `POST {MACHINEGUARD_API_URL}/api/v1/measurements/batch` con
  `{measurements: [...]}` (sincronización del buffer)

La credencial la emite Core con un ADMIN activo. Queda ligada a su organización
y a los nodos autorizados. Se guarda en `EDGE_DEVICE_CREDENTIAL`, fuera del
repositorio. La rotación y la revocación se administran en Core. Al cambiar la
credencial, reinicia Edge con el valor nuevo: el buffer SQLite se conserva.

**Cómo se confirma un envío.** Hace falta una respuesta 2xx y un recibo válido:

- `readingsAccepted` coincide con las lecturas enviadas.
- `measurementsCreated + measurementsReused` equivale a dos mediciones por lectura.

Una página HTML, una redirección, una respuesta vacía o un recibo incompleto no
elimina pendientes. Edge no sigue redirecciones ni escribe secretos en logs.

**Por qué no se duplican datos.** Core convierte el `deviceCode` en los UUID de
sus sensores autorizados y guarda temperatura y humedad en una sola transacción.
Un reintento idéntico recibe los registros que ya existen. Datos distintos en el
mismo instante son un conflicto 409. Así, si Core guardó la lectura pero la
respuesta se perdió, Edge puede confirmarla después sin duplicarla.

**Qué hace Edge con cada respuesta.** Interpreta el campo `code`:

- Errores de muestra van a cuarentena.
- Errores de configuración pausan el nodo.
- Un error de red, 401/403, 5xx o una respuesta sin código conocido conserva
  los pendientes.

**Lotes.** Se conservan microsegundos y la hora se normaliza a UTC. Cada lote tiene
hasta 500 lecturas. Ante rechazos identificables o 5xx repetidos, Edge divide el
lote, con un límite de peticiones por ciclo (`EDGE_DELIVERY_MAX_REQUESTS`). Core
mantiene la atomicidad de cada petición. El worker automático y la sincronización
manual comparten una exclusión. Un recibo inválido nunca libera pendientes.

La repetición segura aplica al envío de lecturas. No aplica a comandos que inicia
una persona.

Las pruebas del transporte están en `tests/test_cloud_client.py`.
