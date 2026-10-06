<#
 Retroceder la configuracion de Windows N horas (por defecto 10) usando Restaurar sistema.
 Debe ejecutarse como Administrador (retroceder.bat lo pide solo).

 Uso:
   retroceder.ps1                  -> menu interactivo
   retroceder.ps1 -Accion Retroceder [-Horas 10] [-Si]
   retroceder.ps1 -Accion Listar
   retroceder.ps1 -Accion CrearPunto
   retroceder.ps1 -Accion Instalar -> activa Restaurar sistema y crea un punto cada hora
   retroceder.ps1 -Accion Quitar   -> quita la tarea horaria
#>
param(
    [ValidateSet('Menu','Retroceder','Listar','CrearPunto','Instalar','Quitar')]
    [string]$Accion = 'Menu',
    [double]$Horas = 10,
    [switch]$Si
)

$ErrorActionPreference = 'Stop'
$TareaNombre = 'Retroceder - punto de restauracion horario'

function Test-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-Puntos {
    Get-ComputerRestorePoint | ForEach-Object {
        [pscustomobject]@{
            Numero      = $_.SequenceNumber
            Fecha       = [Management.ManagementDateTimeConverter]::ToDateTime($_.CreationTime)
            Descripcion = $_.Description
        }
    } | Sort-Object Fecha
}

function Show-Puntos {
    $p = Get-Puntos
    if (-not $p) { Write-Host 'No hay puntos de restauracion. Usa "Instalar" para empezar a crearlos.'; return }
    $p | Format-Table Numero, Fecha, Descripcion -AutoSize
}

function New-Punto {
    # Por defecto Windows limita a 1 punto cada 24 h; lo quitamos para poder crear uno por hora.
    $k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore'
    Set-ItemProperty -Path $k -Name SystemRestorePointCreationFrequency -Value 0 -Type DWord
    Checkpoint-Computer -Description ('Retroceder ' + (Get-Date -Format 'yyyy-MM-dd HH:mm')) -RestorePointType MODIFY_SETTINGS
    Write-Host 'Punto de restauracion creado.'
}

function Install-Todo {
    Enable-ComputerRestore -Drive "$env:SystemDrive\"
    & vssadmin resize shadowstorage /for=$env:SystemDrive /on=$env:SystemDrive /maxsize=10% | Out-Null
    New-Punto
    $cmd = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Accion CrearPunto' -f $PSCommandPath
    $a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $cmd
    $t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1)
    $pr = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
    Register-ScheduledTask -TaskName $TareaNombre -Action $a -Trigger $t -Principal $pr -Force | Out-Null
    Write-Host "Listo: se creara un punto de restauracion cada hora. En ~$Horas horas ya podras retroceder."
}

function Remove-Tarea {
    Unregister-ScheduledTask -TaskName $TareaNombre -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'Tarea horaria eliminada.'
}

function Invoke-Retroceso {
    $objetivo = (Get-Date).AddHours(-$Horas)
    # El punto mas reciente que sea anterior o igual a "hace N horas".
    $punto = Get-Puntos | Where-Object { $_.Fecha -le $objetivo } | Select-Object -Last 1
    if (-not $punto) {
        Write-Host ("No hay ningun punto de restauracion de hace {0} horas o mas antiguo." -f $Horas) -ForegroundColor Yellow
        Write-Host 'Puntos disponibles:'; Show-Puntos
        Write-Host 'Ejecuta "Instalar" y vuelve a intentar cuando haya pasado el tiempo.'
        return
    }
    Write-Host ("Se restaurara el sistema al punto #{0} del {1} ({2})." -f $punto.Numero, $punto.Fecha, $punto.Descripcion)
    Write-Host 'Se deshacen ajustes del sistema, controladores y programas instalados despues de esa hora.'
    Write-Host 'Tus documentos y archivos personales NO se tocan. La PC se reiniciara.'
    if (-not $Si) {
        if ((Read-Host 'Escribe SI para continuar') -ne 'SI') { Write-Host 'Cancelado.'; return }
    }
    # Crea antes un punto de seguridad para poder deshacer el retroceso.
    try { New-Punto } catch { Write-Host "Aviso: no se pudo crear el punto de seguridad: $_" -ForegroundColor Yellow }
    Restore-Computer -RestorePoint $punto.Numero -Confirm:$false
}

if (-not (Test-Admin)) { Write-Host 'Ejecuta como Administrador (usa retroceder.bat).' -ForegroundColor Red; exit 1 }

switch ($Accion) {
    'Listar'     { Show-Puntos }
    'CrearPunto' { New-Punto }
    'Instalar'   { Install-Todo }
    'Quitar'     { Remove-Tarea }
    'Retroceder' { Invoke-Retroceso }
    'Menu' {
        while ($true) {
            Write-Host "`n=== Retroceder configuracion ===" -ForegroundColor Cyan
            Write-Host "1) Retroceder $Horas horas"
            Write-Host '2) Ver puntos de restauracion'
            Write-Host '3) Crear punto ahora'
            Write-Host '4) Instalar (activar y crear un punto cada hora)'
            Write-Host '5) Quitar tarea horaria'
            Write-Host '0) Salir'
            switch (Read-Host 'Opcion') {
                '1' { Invoke-Retroceso }
                '2' { Show-Puntos }
                '3' { New-Punto }
                '4' { Install-Todo }
                '5' { Remove-Tarea }
                '0' { return }
            }
        }
    }
}
