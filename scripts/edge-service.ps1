<#
.SYNOPSIS
Arranque automático de Edge y respaldo diario de su base SQLite en Windows, con tareas programadas del usuario.

.DESCRIPTION
  Install    Registra dos tareas del usuario actual, sin privilegios de administrador:
             - "MachineGuard Edge": al iniciar sesión, ejecuta Edge con .venv y .env de la raíz
               del repositorio; si el proceso falla, la tarea lo reinicia cada minuto.
             - "MachineGuard Edge Backup": cada día a -BackupTime, ejecuta scripts\edge-backup.py.
  Uninstall  Elimina ambas tareas. No detiene un proceso de Edge que ya esté en marcha.
  Status     Muestra el estado de las tareas, los procesos de Edge detectados y la última copia.
  Run        Uso interno de la tarea de Edge. No lo ejecutes a mano salvo para diagnosticar.
  Backup     Uso interno de la tarea de respaldo.

  Con -WhatIf, Install y Uninstall muestran qué cambiarían y no tocan el programador de tareas.
  El script no cambia la política de ejecución del sistema: las tareas usan -ExecutionPolicy Bypass
  solo en su propio proceso.

.EXAMPLE
  .\scripts\edge-service.ps1 -Action Install -WhatIf
  .\scripts\edge-service.ps1 -Action Install
  .\scripts\edge-service.ps1 -Action Status
  .\scripts\edge-service.ps1 -Action Uninstall
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Uninstall', 'Status', 'Run', 'Backup')]
    [string]$Action,

    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$BackupTime = '03:00',

    [string]$LogDirectory = (Join-Path $env:LOCALAPPDATA 'MachineGuard\edge-logs')
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$EnvFile = Join-Path $RepoRoot '.env'
$BackupScript = Join-Path $RepoRoot 'scripts\edge-backup.py'
$ScriptPath = $PSCommandPath
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$EdgeTaskName = 'MachineGuard Edge'
$BackupTaskName = 'MachineGuard Edge Backup'

function Import-EnvFile {
    # Loads KEY=VALUE pairs into this process only. Values are never printed.
    if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Falta el archivo .env en $RepoRoot." }
    foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        $text = $line.Trim()
        if ($text -eq '' -or $text.StartsWith('#')) { continue }
        $separator = $text.IndexOf('=')
        if ($separator -lt 1) { continue }
        $name = $text.Substring(0, $separator).Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $value = $text.Substring($separator + 1).Trim()
        if ($value.Length -ge 2 -and ($value[0] -eq $value[-1]) -and ($value[0] -eq '"' -or $value[0] -eq "'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

function Get-Prerequisites {
    $missing = @()
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        $missing += "Falta el entorno virtual $PythonExe (crea .venv e instala requirements.txt)."
    }
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        $missing += "Falta .env en $RepoRoot (copia .env.example y define EDGE_ADMIN_KEY)."
    }
    return $missing
}

function Write-LogLine([string]$Path, [string]$Message) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    Add-Content -LiteralPath $Path -Encoding UTF8 -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Message)
}

function Invoke-Logged([string]$LogPath, [string[]]$Arguments) {
    # Native stdout and stderr go to the log; the process exit code is returned.
    # Python logs to stderr, which must not become a terminating error in Windows PowerShell 5.1.
    $ErrorActionPreference = 'Continue'
    New-Item -ItemType Directory -Path (Split-Path -Parent $LogPath) -Force | Out-Null
    Push-Location $RepoRoot
    try {
        & $PythonExe @Arguments 2>&1 | ForEach-Object { "$_" } | Add-Content -LiteralPath $LogPath -Encoding UTF8
        return $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}

switch ($Action) {

    'Run' {
        $log = Join-Path $LogDirectory 'edge.log'
        $missing = Get-Prerequisites
        if ($missing) {
            $missing | ForEach-Object { Write-LogLine $log "ERROR $_" }
            exit 1
        }
        Import-EnvFile
        $env:PYTHONUNBUFFERED = '1'
        Write-LogLine $log 'INFO iniciando Edge (python -m edge_api.server)'
        $code = Invoke-Logged $log @('-m', 'edge_api.server')
        Write-LogLine $log "INFO Edge terminó con código $code"
        exit $code
    }

    'Backup' {
        $log = Join-Path $LogDirectory 'backup.log'
        $missing = Get-Prerequisites
        if ($missing) {
            $missing | ForEach-Object { Write-LogLine $log "ERROR $_" }
            exit 1
        }
        Import-EnvFile
        $code = Invoke-Logged $log @($BackupScript)
        Write-LogLine $log "INFO respaldo terminó con código $code"
        exit $code
    }

    'Install' {
        $missing = Get-Prerequisites
        foreach ($problem in $missing) {
            if ($WhatIfPreference) { Write-Warning "$problem La simulación continúa." }
            else { throw $problem }
        }

        $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

        $edgeArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`" -Action Run -LogDirectory `"$LogDirectory`""
        $edgeAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $edgeArguments -WorkingDirectory $RepoRoot
        $edgeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $edgeSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

        $backupArguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`" -Action Backup -LogDirectory `"$LogDirectory`""
        $backupAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $backupArguments -WorkingDirectory $RepoRoot
        $backupTrigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($BackupTime, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
        $backupSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

        if ($PSCmdlet.ShouldProcess($EdgeTaskName, "Registrar tarea de arranque para $user")) {
            Register-ScheduledTask -TaskName $EdgeTaskName -Action $edgeAction -Trigger $edgeTrigger `
                -Settings $edgeSettings -Principal $principal `
                -Description 'Ejecuta Edge de MachineGuard al iniciar sesión y lo reinicia si falla.' -Force | Out-Null
        }
        if ($PSCmdlet.ShouldProcess($BackupTaskName, "Registrar respaldo diario a las $BackupTime")) {
            Register-ScheduledTask -TaskName $BackupTaskName -Action $backupAction -Trigger $backupTrigger `
                -Settings $backupSettings -Principal $principal `
                -Description 'Respalda la base SQLite de Edge con verificación y rotación.' -Force | Out-Null
        }
        if (-not $WhatIfPreference) {
            Write-Output "Tareas registradas para $user. Registros en $LogDirectory."
            Write-Output 'Edge arrancará en el próximo inicio de sesión; para iniciarlo ahora: Start-ScheduledTask.'
        }
    }

    'Uninstall' {
        foreach ($name in @($EdgeTaskName, $BackupTaskName)) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if (-not $task) {
                Write-Output "No existe la tarea '$name'."
                continue
            }
            if ($PSCmdlet.ShouldProcess($name, 'Eliminar tarea programada')) {
                Unregister-ScheduledTask -TaskName $name -Confirm:$false
                Write-Output "Tarea '$name' eliminada."
            }
        }
        Write-Output 'Si Edge sigue en marcha, deténlo con Ctrl+C en su consola o con Stop-Process sobre sus procesos python.'
    }

    'Status' {
        foreach ($name in @($EdgeTaskName, $BackupTaskName)) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if (-not $task) {
                Write-Output "${name}: NO INSTALADA"
                continue
            }
            $info = Get-ScheduledTaskInfo -TaskName $name
            Write-Output ("{0}: {1} | última ejecución {2} | resultado {3} | próxima {4}" -f `
                $name, $task.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
        }

        # En Windows, el python.exe de un venv lanza el intérprete base como proceso hijo.
        # Se cuentan solo los procesos cuyo padre no es también de Edge.
        $edgeProcesses = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
            Where-Object { $_.CommandLine -like '*edge_api.server*' })
        $edgeIds = @($edgeProcesses | ForEach-Object { $_.ProcessId })
        $edgeRoots = @($edgeProcesses | Where-Object { $edgeIds -notcontains $_.ParentProcessId })
        Write-Output "Instancias de Edge en ejecución: $($edgeRoots.Count)"

        $prerequisites = Get-Prerequisites
        $prerequisites | ForEach-Object { Write-Warning $_ }

        if (Test-Path -LiteralPath $EnvFile) {
            Import-EnvFile
            $destination = $env:EDGE_BACKUP_DIR
            # Same rules as scripts\edge-backup.py: relative paths start at the repository root.
            if ($destination -and -not [IO.Path]::IsPathRooted($destination)) {
                $destination = Join-Path $RepoRoot $destination
            }
            if (-not $destination) {
                $database = if ($env:EDGE_DATABASE_PATH) { $env:EDGE_DATABASE_PATH } else { 'edge.db' }
                if (-not [IO.Path]::IsPathRooted($database)) { $database = Join-Path $RepoRoot $database }
                $destination = Join-Path (Split-Path -Parent $database) 'backups'
            }
            if (Test-Path -LiteralPath $destination) {
                $copies = @(Get-ChildItem -LiteralPath $destination -Filter 'edge-backup-*.db' -File | Sort-Object Name)
                $latest = if ($copies) { $copies[-1].Name } else { 'ninguna' }
                Write-Output "Copias en ${destination}: $($copies.Count) | última: $latest"
            }
            else {
                Write-Output "Carpeta de copias aún no existe: $destination"
            }
        }
    }
}
