"""Hot copies of the SQLite database (sqlite3 backup API) + integrity checks + retention."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _quotepath(path: Path) -> str:
    return str(path).replace("\\", "/")


def hot_copy(db_path: Path, backup_path: Path) -> bool:
    """Transactionally consistent copy that does not stop the running writer."""
    db_path = Path(db_path)
    backup_path = Path(backup_path)
    if not db_path.exists():
        logger.error("no existe la base para copiar: %s", db_path)
        return False
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(_quotepath(db_path))
    dst = sqlite3.connect(_quotepath(backup_path))
    try:
        with dst:
            src.backup(dst)
    except Exception:
        logger.exception("copiado de seguridad fallido")
        return False
    finally:
        src.close()
        dst.close()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.info("backup OK: %s (%s, %d bytes)", backup_path, stamp, backup_path.stat().st_size)
    return True


def integrity_check(db_path: Path) -> str:
    try:
        con = sqlite3.connect(_quotepath(Path(db_path)))
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        finally:
            con.close()
        return row[0] if row else "unknown"
    except Exception as exc:
        return f"error: {exc}"


def rotate_backups(backup_dir: Path, keep: int = 14) -> int:
    if not backup_dir.exists():
        return 0
    files = sorted(backup_dir.glob("estudia_*.db"), key=lambda p: p.name, reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            logger.exception("no se pudo borrar %s", old)
    return removed
