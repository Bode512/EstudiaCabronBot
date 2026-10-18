"""File security helpers: safe filenames, path containment, size/type validation."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

MAX_FILENAME_LEN = 180
_TRAVERSAL = re.compile(r"(\.\.|/|\\|:)")


def sanitize_original_name(name: str) -> str:
    """Informational only - never used for disk paths."""
    base = os.path.basename(name or "").strip()
    base = base.replace("\x00", "")
    base = _TRAVERSAL.sub("_", base)
    base = re.sub(r"[^\w \-.()\[\]\u00c0-\u017f]", "_", base)
    return base[:MAX_FILENAME_LEN] or "archivo"


def new_storage_name(kind: str) -> str:
    return f"{uuid.uuid4().hex}.{kind}"


def ensure_within_storage(path: Path, storage_dir: Path) -> bool:
    try:
        resolved = path.resolve()
        base = storage_dir.resolve()
        return base == resolved or base in resolved.parents
    except OSError:
        return False


def validate_upload(name: str, size_bytes: int, mime: str | None, allowed_exts: dict[str, str], max_bytes: int) -> tuple[str, str]:
    """Return (kind, ext) or raise ValueError. Path-traversal-proof by construction."""
    ext = Path(sanitize_original_name(name)).suffix.lower()
    kind = allowed_exts.get(ext)
    if not kind and mime:
        kind = allowed_exts.get(mime)  # mime keys checked by caller passing merged dict
    if not kind:
        raise ValueError("Tipo de archivo no permitido. Formatos: PDF, TXT, DOCX, JPG, PNG.")
    if size_bytes <= 0:
        raise ValueError("Archivo vacio.")
    if size_bytes > max_bytes:
        raise ValueError(f"El archivo supera el limite de {max_bytes // (1024 * 1024)} MB.")
    return kind, ext


def safe_join(storage_dir: Path, relative: str) -> Path | None:
    candidate = (storage_dir / relative).resolve()
    try:
        candidate.relative_to(storage_dir.resolve())
    except ValueError:
        return None
    return candidate


def delete_if_within(path: Path, storage_dir: Path) -> bool:
    if not ensure_within_storage(path, storage_dir):
        return False
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError:
        return False
