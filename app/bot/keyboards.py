"""Inline keyboards and callback data helpers.

Callback schema (validated before any DB access):
  ecb:<scope>:<id>:<action>[:<arg>]
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.constants import CALLBACK_PREFIX


def parse_callback(data: str) -> tuple[str, int | None, str, str | None] | None:
    if not data or not data.startswith(CALLBACK_PREFIX + ":"):
        return None
    parts = data.split(":")
    if len(parts) < 4:
        return None
    try:
        obj_id = int(parts[2])
    except (ValueError, IndexError):
        return None
    return parts[1], obj_id, parts[3] if len(parts) > 3 and parts[3] else "view", parts[4] if len(parts) > 4 else None


def cb(scope: str, obj_id: int, action: str, arg: str | None = None) -> str:
    base = f"{CALLBACK_PREFIX}:{scope}:{obj_id}:{action}"
    return f"{base}:{arg}" if arg else base


def cancel_button(cb_data: str = "ecb:nav:0:cancel") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("Cancelar", callback_data=cb_data)]


def confirm_keyboard(scope: str, obj_id: str, yes_action: str, no_action: str = "cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Aceptar", callback_data=f"{CALLBACK_PREFIX}:{scope}:{obj_id}:{yes_action}"),
                InlineKeyboardButton("Cancelar", callback_data=f"{CALLBACK_PREFIX}:{scope}:0:{no_action}"),
            ]
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Hoy", callback_data="ecb:nav:0:hoy"),
                InlineKeyboardButton("Semana", callback_data="ecb:nav:0:semana"),
            ],
            [
                InlineKeyboardButton("Examenes", callback_data="ecb:nav:0:examenes"),
                InlineKeyboardButton("Tareas", callback_data="ecb:nav:0:tareas"),
            ],
            [
                InlineKeyboardButton("Anadir", callback_data="ecb:nav:0:anadir"),
                InlineKeyboardButton("Plan", callback_data="ecb:nav:0:plan"),
            ],
            [InlineKeyboardButton("Ajustes", callback_data="ecb:nav:0:config")],
        ]
    )


def type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Examen", callback_data="ecb:conv:0:type:exam")],
            [InlineKeyboardButton("Tarea/Trabajo", callback_data="ecb:conv:0:type:task")],
            cancel_button(),
        ]
    )


def subject_keyboard(subjects: list[tuple[int, str]], others_row: bool = True) -> InlineKeyboardMarkup:
    rows = []
    current: list[InlineKeyboardButton] = []
    for sid, name in subjects:
        current.append(InlineKeyboardButton(name[:16], callback_data=cb("conv", sid, "subject")))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    if others_row:
        rows.append([InlineKeyboardButton("Otra asignatura...", callback_data=cb("conv", 0, "subject:other"))])
    rows.append(cancel_button())
    return InlineKeyboardMarkup(rows)


def list_keyboard(exam_items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for obj_id, label in exam_items:
        rows.append([InlineKeyboardButton(label[:48], callback_data=cb("exam", obj_id, "menu"))])
    rows.append(cancel_button())
    return InlineKeyboardMarkup(rows)


def exam_menu_keyboard(exam_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Hecho", callback_data=cb("exam", exam_id, "done")),
                InlineKeyboardButton("Mover fecha", callback_data=cb("exam", exam_id, "move")),
            ],
            [InlineKeyboardButton("Imposible/cancelar", callback_data=cb("exam", exam_id, "cancelit"))],
            [InlineKeyboardButton("Borrar", callback_data=cb("exam", exam_id, "delete"))],
            cancel_button(),
        ]
    )


def task_menu_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Completada", callback_data=cb("task", task_id, "done")),
                InlineKeyboardButton("Mover plazo", callback_data=cb("task", task_id, "move")),
            ],
            [InlineKeyboardButton("Eliminar", callback_data=cb("task", task_id, "delete"))],
            cancel_button(),
        ]
    )


def plan_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Aplicar plan", callback_data=cb("plan", plan_id, "apply")),
                InlineKeyboardButton("Descartar", callback_data=cb("plan", plan_id, "discard")),
            ],
            cancel_button(),
        ]
    )


def candidate_keyboard(doc_id: int, cand_id: int, kind: str) -> InlineKeyboardMarkup:
    action = "mk_exam" if kind == "exam" else "mk_task"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Crear " + ("examen" if kind == "exam" else "tarea"), callback_data=cb("doc", doc_id, action, str(cand_id))),
                InlineKeyboardButton("Ignorar", callback_data=cb("doc", doc_id, "ignore", str(cand_id))),
            ]
        ]
    )


def yes_no_keyboard(scope: str, obj_id: int, action: str, arg: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Si", callback_data=cb(scope, obj_id, action, arg)),
            InlineKeyboardButton("No", callback_data="ecb:nav:0:cancel"),
        ]
    ]
    return InlineKeyboardMarkup(rows)


def delete_account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("SI, eliminar todo", callback_data="ecb:acct:0:delete_confirm"),
                InlineKeyboardButton("No", callback_data="ecb:nav:0:cancel"),
            ]
        ]
    )
