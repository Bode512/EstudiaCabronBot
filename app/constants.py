"""Application-wide constants."""

from __future__ import annotations

COMMAND_NAMES = [
    "start", "help", "hoy", "semana", "examenes", "tareas", "anadir", "archivos",
    "plan", "disponibilidad", "recordatorios", "progreso", "perfil", "cancelar",
    "privacidad", "perfil", "borrarme",
]

# Standard subjects for 2.o de Bachillerato (fallback vocabulary for NL matching)
STANDARD_SUBJECTS = [
    "Matematicas", "Matematicas CCSS", "Lengua y Literatura", "Historia de Espana",
    "Ingles", "Frances", "Filosofia", "Fisica", "Quimica", "Biologia",
    "Historia del Arte", "Economia", "Dibujo Tecnico", "Latina", "Latin", "Griego",
    "Educacion Fisica", "Tecnologia Industrial",
]

MAX_DAILY_NOTIFICATIONS = 30
LIST_PAGE_SIZE = 8
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB soft cap
ALLOWED_DOC_MIMES = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "image/jpeg": "image",
    "image/png": "image",
}
ALLOWED_DOC_EXTENSIONS = {".pdf": "pdf", ".txt": "txt", ".docx": "docx", ".jpg": "image", ".jpeg": "image", ".png": "image"}

CALLBACK_PREFIX = "ecb"

WEEKDAY_NAMES_INDEX = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
