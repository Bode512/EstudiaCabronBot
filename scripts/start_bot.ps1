# Arranca el bot desde consola con los logs visibles.
# Uso: powershell -ExecutionPolicy Bypass -File scripts\start_bot.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "No existe .venv. Creando entorno..."
    python -m venv .venv
    & .venv\Scripts\python.exe -m pip install -r requirements.txt
}

Write-Host "Arrancando EstudiaCabronBot (Ctrl+C para parar)..."
& .venv\Scripts\python.exe -m app.main
