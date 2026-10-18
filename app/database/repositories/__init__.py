"""Repository exports for convenient imports."""

from app.database.repositories.assignments import AssignmentRepository
from app.database.repositories.exams import ExamRepository
from app.database.repositories.reminders import ReminderRepository
from app.database.repositories.settings import SettingsRepository
from app.database.repositories.study import (
    DocumentRepository,
    StudyRepository,
    TopicRepository,
)
from app.database.repositories.subjects import AuditRepository, SubjectRepository
from app.database.repositories.users import UserRepository

__all__ = [
    "AssignmentRepository",
    "AuditRepository",
    "DocumentRepository",
    "ExamRepository",
    "ReminderRepository",
    "SettingsRepository",
    "StudyRepository",
    "SubjectRepository",
    "TopicRepository",
    "UserRepository",
]
