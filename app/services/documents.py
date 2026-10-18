"""Document ingestion pipeline: validate -> store -> extract -> propose (user confirms).

Handlers only pass bytes here; creation of exams/assignments happens ONLY after the
user explicitly approves a review card, so untrusted document text never auto-writes DB.
CPU-heavy work (OCR, parsing) runs in a worker thread via run_in_executor.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.config import Settings, get_settings
from app.constants import ALLOWED_DOC_EXTENSIONS, MAX_UPLOAD_BYTES
from app.database.models import Document as DocumentRow
from app.database.models import DocumentStatus, utcnow
from app.database.repositories import DocumentRepository, UserRepository
from app.security.files import (
    delete_if_within,
    ensure_within_storage,
    new_storage_name,
    sanitize_original_name,
    validate_upload,
)
from app.utils.dates import parse_spanish_datetime

logger = logging.getLogger(__name__)

_TEXT_LIMIT_CHARS = 20000
_MONTHS_RE = r"(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre|ene|feb|mar|abr|may|jun|jul|ago|sep|sept|oct|nov|dic)"
EXAM_WORDS = re.compile(r"(?i)\b(examen|examenes|test|prueba|quiz)\b")
ASSIGN_WORDS = re.compile(r"(?i)\b(trabajo|tarea|entrega|proyecto|deberes)\b")
SUBJECT_HINTS = [
    "matematicas", "lengua", "historia", "ingles", "frances", "filosofia", "fisica",
    "quimica", "biologia", "geologia", "economia", "arte", "latina", "latin", "griego",
    "educacion fisica", "tecnologia", "dibujo",
]


@dataclass
class Candidate:
    kind: str            # 'exam' | 'assignment'
    subject_guess: str | None
    title: str
    date: date | None
    assumed_year: bool
    source_line: str


@dataclass
class ExtractionResult:
    text: str
    empty: bool
    error: str | None = None


# ------------------------------------------------------------------ extraction backends (sync, run in executor)
def _extract_pdf(path: Path) -> ExtractionResult:
    import PyPDF2

    chunks: list[str] = []
    with path.open("rb") as fh:
        try:
            reader = PyPDF2.PdfReader(fh)
            for page in reader.pages:
                chunks.append(page.extract_text() or "")
        except Exception as exc:
            return ExtractionResult("", True, error=f"pdf ilegible: {exc}")
    text = "\n".join(chunks)
    return ExtractionResult(text, not text.strip())


def _extract_docx(path: Path) -> ExtractionResult:
    from docx import Document as DocxDocument

    doc = DocxDocument(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts)
    return ExtractionResult(text, not text.strip())


def _extract_txt(path: Path) -> ExtractionResult:
    raw = path.read_bytes()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    return ExtractionResult(text, not text.strip())


def _extract_image(path: Path) -> ExtractionResult:
    import shutil

    import pytesseract
    from PIL import Image

    if not shutil.which("tesseract"):
        return ExtractionResult("", True, error="OCR no disponible: Tesseract no esta instalado.")
    image = Image.open(path)
    try:
        text = pytesseract.image_to_string(image, lang="spa+eng")
    except Exception:
        try:
            text = pytesseract.image_to_string(image)
        except Exception as exc:
            return ExtractionResult("", True, error=f"OCR fallo: {exc}")
    return ExtractionResult(text, not text.strip())


def extract_text_sync(stored_path: str, kind: str) -> ExtractionResult:
    path = Path(stored_path)
    if kind == "pdf":
        return _extract_pdf(path)
    if kind == "docx":
        return _extract_docx(path)
    if kind == "txt":
        return _extract_txt(path)
    if kind == "image":
        return _extract_image(path)
    return ExtractionResult("", True, error="kind no soportado")


# ------------------------------------------------------------------ candidate analysis
def guess_subject_line(line: str) -> str | None:
    lowered = (
        line.lower()
        .replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u")
    )
    for hint in SUBJECT_HINTS:
        if len(hint) > 3 and hint in lowered:
            return hint.capitalize()
    return None


def parse_line_for_date(line: str, tz_name: str) -> tuple[date, bool] | None:
    """Numeric Spanish dates (12/11) preferred; fallback to natural parser; final fallback '12 de noviembre'."""
    m = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b", line)
    if m:
        day_num, month_num, year_raw = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= day_num <= 31 and 1 <= month_num <= 12:
            today = date.today()
            if year_raw:
                year = int(year_raw)
                if year < 100:
                    year += 2000
                try:
                    return date(year, month_num, day_num), False
                except ValueError:
                    return None
            try:
                candidate = date(today.year, month_num, day_num)
            except ValueError:
                return None
            if candidate < today:
                try:
                    candidate = date(today.year + 1, month_num, day_num)
                except ValueError:
                    return None
            return candidate, True
    parsed = parse_spanish_datetime(line, tz_name)
    if parsed is not None:
        return parsed.day, parsed.assumed_year
    return None


def find_candidates(text: str, tz_name: str) -> list[Candidate]:
    out: list[Candidate] = []
    current_subject: str | None = None
    for raw_line in (text or "").splitlines()[:400]:
        line = raw_line.strip()
        if not line or len(line) > 300:
            continue
        subject = guess_subject_line(line)
        if subject and len(line) < 60 and not EXAM_WORDS.search(line) and not ASSIGN_WORDS.search(line):
            current_subject = subject
        is_exam = bool(EXAM_WORDS.search(line))
        is_assign = bool(ASSIGN_WORDS.search(line))
        if not (is_exam or is_assign):
            continue
        parsed = parse_line_for_date(line, tz_name)
        if parsed is None:
            continue  # no date -> cannot propose with fake data
        day, assumed = parsed
        out.append(
            Candidate(
                kind="exam" if is_exam else "assignment",
                subject_guess=current_subject,
                title=line[:140],
                date=day,
                assumed_year=assumed,
                source_line=line[:200],
            )
        )
    return out


# ------------------------------------------------------------------ service
class DocumentService:
    def __init__(self, session_factory, settings: Settings | None = None) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    async def _get(self, doc_id: int, user_id: int) -> DocumentRow | None:
        async with self.session_factory() as session:
            return await DocumentRepository(session).get_owned(user_id, doc_id)

    async def ingest(self, user_id: int, file_bytes: bytes, original_name: str, mime: str | None) -> DocumentRow:
        """Full pipeline: validate -> store -> create row -> process (returns the row)."""
        from app.services.errors import ValidationError

        if not file_bytes:
            raise ValidationError("El archivo esta vacio.")
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise ValidationError(f"El archivo supera el limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        try:
            kind, _ext = validate_upload(original_name, len(file_bytes), mime, ALLOWED_DOC_EXTENSIONS, MAX_UPLOAD_BYTES)
        except ValueError:
            if mime in {"image/jpeg", "image/png"}:
                kind = "image"
            else:
                raise ValueError("Tipo de archivo no permitido. Formatos: PDF, TXT, DOCX, JPG, PNG.") from None

        display_name = sanitize_original_name(original_name)
        stored_name = new_storage_name(kind)
        storage_dir = self.settings.storage_dir
        storage_dir.mkdir(parents=True, exist_ok=True)
        target = storage_dir / stored_name
        if not ensure_within_storage(target, storage_dir):
            raise RuntimeError("ruta fuera de storage")
        target.write_bytes(file_bytes)

        async with self.session_factory() as session:
            doc = await DocumentRepository(session).create(
                user_id, original_name=display_name, stored_path=str(target), kind=kind, size_bytes=len(file_bytes)
            )
            await session.commit()
        try:
            await self.process_document(doc.id, user_id)
        except Exception:
            logger.exception("procesado fallido doc_id=%s", doc.id)
        return (await self._get(doc.id, user_id))  # type: ignore[return-value]

    async def process_document(self, doc_id: int, user_id: int) -> DocumentStatus:
        """Extract text off the event loop; set status accordingly (never raises)."""
        doc = await self._get(doc_id, user_id)
        if doc is None:
            return DocumentStatus.FAILED
        kind, stored_path = doc.kind, doc.stored_path
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
        if user is None:
            return DocumentStatus.FAILED
        loop = asyncio.get_running_loop()
        try:
            result: ExtractionResult = await loop.run_in_executor(None, extract_text_sync, stored_path, kind)
        except Exception as exc:  # defensive: executor failures
            logger.exception("lectura fallida doc_id=%s", doc_id)
            result = ExtractionResult("", True, error=f"lectura fallida: {exc}")

        if result.error:
            status = DocumentStatus.FAILED
            meta = {"text": "", "empty": result.empty, "error": result.error}
        elif not result.text.strip():
            status = DocumentStatus.NEEDS_REVIEW
            meta = {"text": "", "empty": True, "error": result.error}
        else:
            status = DocumentStatus.PROCESSED
            meta = {"text": result.text[:_TEXT_LIMIT_CHARS]}
        async with self.session_factory() as session:
            await DocumentRepository(session).update_owned(
                user_id, doc_id, status=status, meta=meta, processed_at=utcnow(), error=meta.get("error")
            )
            await session.commit()
        return status

    async def extract_candidates(self, user_id: int, doc_id: int) -> list[Candidate]:
        doc = await self._get(doc_id, user_id)
        if not doc or doc.status == DocumentStatus.FAILED:
            return []
        text = (doc.meta or {}).get("text", "")
        if not text:
            return []
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            tz = (user.timezone if user else None) or self.settings.default_timezone
        return find_candidates(text, tz)

    async def retry_failed(self, user_id: int, doc_id: int) -> bool:
        doc = await self._get(doc_id, user_id)
        if not doc or doc.status != DocumentStatus.FAILED:
            return False
        await self.process_document(doc_id, user_id)
        refreshed = await self._get(doc_id, user_id)
        return refreshed is not None and refreshed.status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)

    async def list_recent(self, user_id: int, limit: int = 20) -> list[DocumentRow]:
        async with self.session_factory() as session:
            return await DocumentRepository(session).list_by_user(user_id, limit=limit)

    async def delete_owned(self, user_id: int, doc_id: int) -> bool:
        async with self.session_factory() as session:
            doc = await DocumentRepository(session).delete_owned(user_id, doc_id)
            await session.commit()
        if not doc:
            return False
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, delete_if_within, Path(doc.stored_path), self.settings.storage_dir)
        return True