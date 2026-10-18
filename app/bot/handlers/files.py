"""Uploads: PDF/DOCX/TXT/photos -> text extraction -> exam/assignment candidates."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.bot.keyboards import parse_callback
from app.bot.middleware import require_registered
from app.database.repositories import AuditRepository
from app.services.assignments import AssignmentService
from app.services.documents import DocumentService
from app.services.exams import ExamService

logger = logging.getLogger(__name__)


async def _user_tz(user_id: int, service: DocumentService) -> str:
    import sqlalchemy as sa

    from app.database.models import User

    async with service.session_factory() as session:
        user = (await session.execute(sa.select(User).where(User.telegram_user_id == user_id))).scalar_one_or_none()
        return (user.timezone if user else None) or service.settings.default_timezone


@require_registered
async def on_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or update.effective_user is None:
        return
    ctx = context.bot_data["ctx"]
    db_user = await ctx.users.get_user_by_telegram(update.effective_user.id)
    if db_user is None:
        await msg.reply_text("No estas registrado. Escribe /start.")
        return
    user_id = db_user.id
    attachment = msg.effective_attachment
    if isinstance(attachment, list):  # photo sizes
        tg_file = max(attachment, key=lambda p: (p.width or 0) * (p.height or 0))
        original_name, mime = "foto_agenda.jpg", "image/jpeg"
    elif attachment is not None and getattr(attachment, "file_name", None):
        tg_file = attachment
        original_name = tg_file.file_name or "upload"
        mime = tg_file.mime_type
    else:
        await msg.reply_text("Enviame un PDF, DOCX, TXT, una foto, o usa /anadir.")
        return

    try:
        raw = await tg_file.download_as_bytearray()
    except Exception:
        logger.exception("descargando fichero")
        await msg.reply_text("No pude descargar el fichero. Prueba de nuevo.")
        return

    ctx = context.bot_data["ctx"]
    service = DocumentService(ctx.session_factory)
    try:
        document = await service.ingest(user_id, bytes(raw), original_name, mime)
    except ValueError as exc:
        await msg.reply_text(str(exc))
        return
    except Exception:
        logger.exception("ingest fallido")
        await msg.reply_text("No se pudo procesar el archivo. Intentalo otra vez.")
        return

    candidates = await service.extract_candidates(user_id, document.id)
    if not candidates:
        name_shown = document.original_name or "el archivo"
        if document.error:
            await msg.reply_text(f"'{name_shown}' guardado, pero sin texto util ({document.error}). Reescribe los datos con /anadir.")
        else:
            await msg.reply_text(
                f"'{name_shown}' guardado, pero no he encontrado examenes ni entregas claros. Crea los registros con /anadir."
            )
        return

    payload = []
    for i, c in enumerate(candidates):
        payload.append({
            "kind": c.kind,
            "subject": c.subject_guess,
            "title": c.title,
            "day": c.date.isoformat() if c.date else None,
            "assumed_year": c.assumed_year,
            "used": False,
            "i": i,
        })
    context.user_data[f"cands_{document.id}"] = payload
    lines = "<b>He encontrado en el documento:</b>"
    for i, c in enumerate(candidates[:6]):
        day_txt = c.date.strftime("%d/%m/%Y") if c.date else "?"
        tipo = "Examen" if c.kind == "exam" else "Tarea"
        subj = f" [{c.subject_guess}]" if c.subject_guess else ""
        lines += f"\n{i + 1}. {tipo}{subj}: {c.title[:64]} - {day_txt}"
    lines += "\n\nVamos uno a uno:"
    await msg.reply_html(lines)
    await _present_candidate(update, context, document.id, 0)


async def _present_candidate(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_id: int, index: int) -> None:
    payload = context.user_data.get(f"cands_{doc_id}") or []
    if index >= len(payload):
        context.user_data.pop(f"cands_{doc_id}", None)
        return
    c = payload[index]
    tipo = "examen" if c["kind"] == "exam" else "tarea"
    subj = f" [{c['subject']}]" if c.get("subject") else ""
    day_txt = c["day"]
    assumed = " (anio asumido)" if c.get("assumed_year") else ""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"Crear {tipo}", callback_data=f"ecb:doc:{doc_id}:accept:{index}"),
                InlineKeyboardButton("Convertir en tarea" if c["kind"] == "exam" else "Convertir en examen", callback_data=f"ecb:doc:{doc_id}:flip:{index}"),
            ],
            [
                InlineKeyboardButton("Descartar", callback_data=f"ecb:doc:{doc_id}:skip:{index}"),
                InlineKeyboardButton("Cancelar todo", callback_data="ecb:nav:0:cancel"),
            ],
        ]
    )
    text = f"<b>Propuesta {index + 1}/{len(payload)}</b>{subj}\n{c['title'][:100]}\nFecha: {day_txt}{assumed}\nCrear?"
    target = update.effective_message
    if target is None:
        return
    await target.reply_html(text, reply_markup=keyboard)


@require_registered
async def on_doc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    parsed = parse_callback(query.data or "")
    if parsed is None:
        await query.answer()
        return
    _scope, doc_id, action, arg = parsed
    await query.answer()
    if action == "skip":
        await _next_candidate(update, context, doc_id, int(arg or 0) + 1)
        return
    if action not in ("accept", "flip"):
        return
    db_user = await context.bot_data["ctx"].users.get_user_by_telegram(update.effective_user.id)
    if db_user is None:
        await query.answer("No estas registrado.")
        return
    user_id = db_user.id
    ctx = context.bot_data["ctx"]
    service = DocumentService(ctx.session_factory)
    payload = context.user_data.get(f"cands_{doc_id}") or []
    index = int(arg or 0)
    if index >= len(payload):
        await query.message.reply_text("No queda nada pendiente.")
        return
    c = payload[index]
    kind = c["kind"] if action == "accept" else ("task" if c["kind"] == "exam" else "exam")
    c["used"] = True
    try:
        created = await _create_candidate(user_id, service, c, kind)
        await query.message.reply_text(f"Creada la {('tarea' if kind == 'task' else 'prueba')}: {created}")
    except Exception:
        logger.exception("no se pudo crear el candidato")
        await query.message.reply_text("No se pudo crear el registro. Prueba manual con /anadir.")
    await _next_candidate(update, context, doc_id, index + 1)


async def _create_candidate(user_id: int, service: DocumentService, cand: dict, kind: str) -> str:
    day = cand.get("day")
    day_txt = day or ""
    day_from_iso = None
    if day_txt:
        d, m, y = day_txt.split("-")
        day_from_iso = __import__("datetime").date(int(y), int(m), int(d))
    if day_from_iso is None:
        return "sin fecha"
    async with service.session_factory() as session:
        audit = AuditRepository(session)
        if kind == "exam":
            svc = ExamService(session)
            exam = await svc.create_exam(
                user_id, audit,
                title=cand.get("title", "Examen")[:180],
                subject_name=cand.get("subject") or "General",
                exam_date=day_from_iso,
                has_time=False,
            )
            await session.commit()
            return f"[{await svc.subject_name(exam)}] {exam.title}"
    async with service.session_factory() as session:
        audit = AuditRepository(session)
        svc = AssignmentService(session)
        task, _created = await svc.create_assignment(
            user_id, audit,
            description=cand.get("title", "Trabajo")[:220],
            subject_name=cand.get("subject") or "General",
            due_date=day_from_iso,
            has_time=False,
        )
        await session.commit()
        return task.description


async def _next_candidate(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_id: int, index: int) -> None:
    payload = context.user_data.get(f"cands_{doc_id}") or []
    if index >= len(payload):
        used = sum(1 for c in payload if c.get("used"))
        context.user_data.pop(f"cands_{doc_id}", None)
        if update.effective_message:
            await update.effective_message.reply_text(f"Listo. {used} entrada(s) creada(s) desde el documento.")
        return
    await _present_candidate(update, context, doc_id, index)


@require_registered
async def cmd_archivos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_html(
        "<b>Como funcionan los ficheros</b>\n"
        "Sube un PDF/DOCX/TXT con el calendario o el programa del curso, incluso una foto de la agenda. "
        "Busco lineas 'Examen ... 12/11' o 'Entrega ... viernes' y te las propongo una a una para crear/descartar.\n"
        "Las fotos usan OCR (necesitas Tesseract instalado si tu tarjeta de capturas no vale).\n"
        "Los registros se crean SOLO cuando pulsas 'Crear' en cada propuesta."
    )


def register_files(app: Application) -> None:
    app.add_handler(CommandHandler("archivos", cmd_archivos), group=0)
    app.add_handler(
        MessageHandler(
            (filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND,
            on_upload,
        ),
        group=1,
    )
    app.add_handler(CallbackQueryHandler(on_doc_callback, pattern=r"^ecb:doc:\d+:"), group=0)
