# Copia de seguridad manual de la base de datos (consistente, sin parar el bot si esta en marcha).
# Uso: powershell -ExecutionPolicy Bypass -File scripts\backup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& .venv\Scripts\python.exe -c "from pathlib import Path; from app.backups import hot_copy, integrity_check, rotate_backups; from app.config import get_settings; s = get_settings(); d = s.db_path.parent / 'backups'; stamp = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime('%Y%m%dT%H%M%SZ'); target = d / ('estudia_' + stamp + '.db'); ok = hot_copy(s.db_path, target); print('copiado:', target, 'OK' if ok else 'FALLO'); print('integridad:', integrity_check(target) if ok else '-'); rotate_backups(d, keep=14)"
