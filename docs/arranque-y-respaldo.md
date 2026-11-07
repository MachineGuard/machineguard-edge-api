# Arranque automático y respaldo

Esta guía hace dos cosas: Edge arranca al iniciar sesión y se reinicia si falla,
y la base SQLite se respalda una vez al día. Ninguna parte requiere
administrador ni cambia la configuración de seguridad del equipo.

## Antes de empezar

1. Crea el entorno virtual en la raíz del repositorio e instala el lock de ejecución:

   ```powershell
   python -m venv .venv
   & ./.venv/Scripts/python.exe -m pip install --require-hashes -r requirements.txt
   ```

2. Crea `.env` a partir de `.env.example` y define `EDGE_ADMIN_KEY`
   (ver [configuración](configuracion.md)).

## Windows

```powershell
.\scripts\edge-service.ps1 -Action Install -WhatIf   # muestra lo que haría, sin cambiar nada
.\scripts\edge-service.ps1 -Action Install           # registra las tareas
.\scripts\edge-service.ps1 -Action Status            # estado, último resultado y última copia
.\scripts\edge-service.ps1 -Action Uninstall         # elimina las tareas
```

Si la política del equipo impide ejecutar el script, usa
`powershell -ExecutionPolicy Bypass -File .\scripts\edge-service.ps1 -Action Status`.
Esa opción solo afecta a ese proceso.

`Install` registra dos tareas del usuario actual:

| Tarea | Cuándo corre | Si falla |
|---|---|---|
| `MachineGuard Edge` | Al iniciar sesión | Se reinicia cada minuto, hasta 999 veces |
| `MachineGuard Edge Backup` | Cada día a las 03:00 (`-BackupTime HH:mm`) | Reintenta 2 veces, cada 10 minutos |

Las tareas leen `.env` de la raíz del repositorio en cada ejecución; el archivo
no se copia dentro de la tarea. Los registros quedan en
`%LOCALAPPDATA%\MachineGuard\edge-logs` (`edge.log` y `backup.log`). Otra
carpeta: `-LogDirectory`.

### Respaldo de la base

Variables opcionales en `.env`:

- `EDGE_BACKUP_DIR`: carpeta de las copias. Vacía significa la carpeta `backups`
  junto a `EDGE_DATABASE_PATH`.
- `EDGE_BACKUP_KEEP`: copias que se conservan. Por defecto, 7.

Cada copia usa la API de respaldo en línea de SQLite, así que se toma mientras
Edge escribe. Se guarda como `edge-backup-AAAAMMDDTHHMMSSffffffZ.db` y se
verifica con `PRAGMA integrity_check` antes de publicarse. Si la verificación
falla, no queda ninguna copia nueva, se conservan las anteriores y el error
queda en `backup.log`. Al pasar del límite se borran solo las copias más antiguas
con ese nombre.

Para hacer una copia a mano:

```powershell
python scripts\edge-backup.py --database edge.db --destination D:\respaldos-edge --keep 14
```

### Restaurar una copia

1. Detén Edge: Ctrl+C en su consola, o detén el proceso `python.exe` que ejecuta
   `edge_api.server`.
2. Guarda aparte el archivo actual de `EDGE_DATABASE_PATH`.
3. Copia la `edge-backup-*.db` elegida sobre `EDGE_DATABASE_PATH` y borra
   `edge.db-wal` y `edge.db-shm` si existen.
4. Arranca Edge otra vez, con `Start-ScheduledTask "MachineGuard Edge"` o
   `python -m edge_api.server`.

Las lecturas tomadas después de la copia no están en la restaurada.

## Raspberry Pi o Linux (systemd)

Ejemplo para `/opt/machineguard/machineguard-edge-api`, con un usuario de servicio
`edge` que pueda escribir en la carpeta de la base. No se ha probado en una
Raspberry Pi real: ajusta rutas y usuario.

```ini
# /etc/systemd/system/machineguard-edge.service
[Unit]
Description=MachineGuard Edge
Wants=network-online.target
After=network-online.target

[Service]
User=edge
WorkingDirectory=/opt/machineguard/machineguard-edge-api
EnvironmentFile=/opt/machineguard/machineguard-edge-api/.env
ExecStart=/opt/machineguard/machineguard-edge-api/.venv/bin/python -m edge_api.server
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Respaldo diario:

```ini
# /etc/systemd/system/machineguard-edge-backup.service
[Unit]
Description=Respaldo de la base de Edge

[Service]
Type=oneshot
User=edge
WorkingDirectory=/opt/machineguard/machineguard-edge-api
EnvironmentFile=/opt/machineguard/machineguard-edge-api/.env
ExecStart=/opt/machineguard/machineguard-edge-api/.venv/bin/python scripts/edge-backup.py
```

```ini
# /etc/systemd/system/machineguard-edge-backup.timer
[Unit]
Description=Respaldo diario de la base de Edge

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Activar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now machineguard-edge.service
sudo systemctl enable --now machineguard-edge-backup.timer
```

## Límites

- Las tareas de Windows solo corren con la sesión iniciada. Si el equipo se
  reinicia y nadie inicia sesión, Edge no arranca. Este script no configura el
  inicio de sesión automático.
- `Uninstall` quita las tareas, pero no detiene un Edge que ya esté corriendo.
  `Status` cuenta las instancias. Con el venv de Windows hay dos procesos
  `python` por instancia (el lanzador y el intérprete); detén los dos con Ctrl+C
  en la consola de Edge o con `Stop-Process`.
- No está comprobado que detener la tarea desde el Programador de tareas
  termine también el intérprete hijo. Revisa `Status` después de detenerla.
- Usa un solo proceso por archivo de base. La tarea no se duplica si ya corre,
  pero no evita un Edge lanzado a mano mientras la tarea está activa.
- Las copias quedan en el mismo equipo y no se cifran. Contienen las lecturas:
  protege la carpeta y copia una parte fuera del equipo si necesitas resistir
  la pérdida del disco.
- Los registros `edge.log` y `backup.log` no rotan.
