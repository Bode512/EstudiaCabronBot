# EstudiaCabronBot — Diseño técnico

## 1. Visión general

Asistente académico personal para estudiantes de 2.º de Bachillerato (España), accesible vía
Telegram. Se despliega en un PC doméstico que no está encendido 24/7; por tanto, **la base de
datos es la única fuente de verdad** y cualquier mecanismo en memoria (scheduler, jobs, colas)
es descartable y se reconstruye al arrancar.

Garantía central: nada de datos académicos ni recordatorios depende del proceso en ejecución.
Al arrancar, la aplicación reconcilia su estado persistido con la realidad de la BD.

## 2. Arquitectura

```
Telegram  <--long polling-->  python-telegram-bot (async)
                                  |
                        handlers (capa presentación)
                        solo traducen Telegram <-> servicios
                                  |
                        services (regla de negocio, ownership, validación)
                                  |
                        repositories (acceso a datos, filtros por user_id)
                                  |
                        SQLite (WAL, FKs, Alembic)  <-- backups -->
```

Componentes transversales:

- **scheduler/worker.py**: APScheduler (BackgroundScheduler en jobstore memoria) con 3 tipos de job:
  1. `tick` cada 30 s → procesa recordatorios vencidos (outbox).
  2. `digest` diario → crea la fila `reminders` del resumen diario de cada usuario.
  3. `backup` diario → backup de la BD.
- **scheduler/recovery.py**: al arrancar (antes de un solo job): migraciones, reconciliación de
  recordatorios pendientes/atascados, política catch-up, informe de diagnóstico.
- **notifications.py**: único punto que habla con Telegram para envíos (con rate limiting,
  errores tipados, historial en `notification_deliveries`).

Los handlers no crean recordatorios ni tocan la BD directamente; pasan por servicios.

## 3. Base de datos (SQLite + SQLAlchemy 2.x + Alembic)

- WAL activado por PRAGMA en cada conexión; `foreign_keys=ON`; `busy_timeout=5000ms`.
- Fechas/horas: **siempre UTC** en la BD (`DateTime(timezone=True)` se guarda como epoch UTC;
  conversiones a la zona del usuario solo en presentación y al materializar `send_at`).
- Exámenes sin hora exacta → `exam_date` (DATE) a las 09:00 locales por defecto para cálculo
  de recordatorios; exámenes con hora → `exam_at` (DateTime UTC). Se distingue con un campo
  booleano `has_time`.
- Tablas (ver `app/database/models.py`):

| Tabla | Razón de diseño |
|---|---|
| `users` | telegram_user_id única (identidad externa numérica); nombre, username, idioma, tz, nivel, estado |
| `settings` | 1:1 con `users`: resumen diario, horas silencio, offsets por defecto, disponibilidad semanal (JSON), política catch-up, lí fade notificaciones |
| `subjects` | única por (user_id, nombre normalizado); borrar asignatura deja `exam/assignment/topic.subject_id = NULL` (no borrado en cascada de contenido) |
| `exams` | fecha/hora, importancia y dificultad 1-5, est. minutos de preparación, estado de preparación |
| `assignments` | vencimiento, prioridad (3=verde/2=naranja/1=roja), estado, minutos estimados |
| `topics` | progreso (pending/learning/reviewed/mastered), confianza 1-5; FK opcional a examen |
| `study_sessions` | planificadas por el planificador; estados planned/done/missed/skipped; FK plan opcional |
| `documents` | nombre original solo informativo; en disco se guarda con UUID; metadatos JSON de extracción |
| `reminders` | **outbox transaccional**: uuid estable, send_at UTC, estado (pending/processing/sent/failed/expired/cancelled), intentos, backoff |
| `notification_deliveries` | historial: intento nº, resultado Telegram, categoría de error |
| `study_plans` | versión y estado (proposed → active → superseded); sesiones asociadas vía `study_sessions.plan_id` |
| `audit_events` | acción + entidad + detalle JSON; sin secretos ni contenido personal extenso |

- Borrado de usuario → cascada total (`ON DELETE CASCADE` desde `users`).
- Migración inicial: `0001_initial` (crea todas las tablas). Nada de `Base.metadata.create_all`
  como mecanismo normal; se ejecuta `alembic upgrade head` en el arranque (con backup previo).

## 4. Ciclo de arranque y recuperación

Orden estricto en `main.py`:

1. Logging + carga de configuración (falla rápido si falta el token).
2. Apertura de BD: comprobación de conectividad + integridad (`PRAGMA quick_check`).
3. Backup de seguridad si hay migraciones pendientes, luego `alembic upgrade head`.
4. Informe: nº de usuarios, recordatorios pendientes, vencidos, entregas fallidas.
5. `Recovery.run()`:
   - RescataBusy: `processing` con más de 5 min → vuelve a `pending` (choques de crash).
   - Obsolescencia: recordatorios de eventos cuyo momento ya pasó → `expired`,
     EXCEPTO avisos de tareas vencidas aún sin entregar (`overdue_task`), que se mantienen.
   - Catch-up: recordatorios `pending` con `send_at < arranque` se agrupan por usuario:
     `catch_up_mode = individual | summary` (por defecto **summary**), y un tope
     `max_backlog` (por defecto 10) para no inundar. En modo summary se envía 1 mensaje
     organizado y las filas pasan a `sent` (marcadas summary).
   - Idempotencia: los cambios de estado son actualizaciones condicionales atomicas
     (`UPDATE ... WHERE status='pending'`), los workers se reclaman con un solo usuario activo
     (lock por extras-row) y las entregas se registran en `notification_deliveries`.
6. Solo tras éxito: se arranca el scheduler y el polling de Telegram.

### Política de envíos falibles

- Confirmado `sent` **solo tras respuesta de Telegram OK** (message_id registrado).
- Éxito-no-registrado (crash tras aceptación de Telegram, antes del commit interno):
  se documenta como resultado **ambiguo** innegable. La recuperación reenviará tras reinicio
  de `processing`→`pending`. Telegram no garantiza exactly-once; asumimos riesgo minúsculo.
- Retries con backoff exponencial + jitter (2^n·30 s ± azar), `max_retries=5`.
- Errores de Telegram tipificados: `rate_limited` (429, respeta `retry_after`),
  `blocked` (403 → deja de reintentar; deja constancia para el próximo mensaje al usuario),
  `chat_not_found` (idem), `network` (reintenta), `unknown` (reintenta con tope).

## 5. Motor de recordatorios (outbox)

- Tipos: `exam_advance` (oficiosa: 7d/3d/1d/1h antes — configurables por usuario),
  `assignment_due` (1d/1h antes), `daily_summary` (contenido calculado en el momento del envío:
  exámenes de hoy/mañana, tareas vencidas y hoy), `overdue_task` (se encola cuando una tarea
  pasa de vencida — a diario), `custom` (creados por el usuario).
- Los offsets en el pasado no se materializan (no existe recordatorio点 atrasado caducado).
- Los jobs del scheduler solo consultan “due” en la BD; los timers son efímeros.
- `Quiet hours` (evitar p.ej. de 23:00 a 08:00) pospone el envío a la hora del fin de quieto,
  salvo para exámenes que empiezan en las próximas 2 h (se envían igual).

## 6. Planificador de estudio (determinístico, puro)

`study_planner.plan_study(...) es una función pura determinística (sin BD, sin IA)`.

Entradas: exámenes (fecha, importancia, dificultad, minutos restantes de preparación,
confianza por temas), tareas (vence y minutos), disponibilidad semanal (minutos/día de la
semana), sesiones ya cerradas (done/missed), hoy (fecha local), ventana horaria (por defecto
16:00–22:00 locales).

Salida: lista de sesiones propuestas (fecha local, asignatura, tema/objetivo, minutos,
prioridad) + lista de déficits si la carga supera la disponibilidad. El servicio traduce
propuestas a sesiones en la BD y enriquece los objetivos con la tabla de temas
(el objetivo concreto que toca: tema menos dominado / más cercano en confianza 1-2),
ordenando los temas por (confianza ASC, nº orden) determinísticamente.

Tradeoff transparente: el planificador ENSEÑA la cuenta (total disponible vs. total necesario)
y no crea un plan hasta que el usuario lo aprueba. Si falta tiempo, se muestra la lista de
conflictos y opciones (añadir horas / bajar dificultad est. / quitar temas).

## 7. Interacción natural (español)

- `utils/dates.py`: parser determinista de frases de fecha en español: `mañana`,
  `pasado mañana`, `el jueves`, `el jueves que viene`, `12 de noviembre`, `12/11`,
  `12-11-2026`, `hoy`, `próximo lunes`…  con bandera `assumed_year` cuando se infiera el año
  (regla: la próxima ocurrencia >= hoy; si la interpretación más natural cae en el pasado se
  pide confirmación salvo que se pida el año tercero siguiente).
- `services/nl_parser.py`: detecta intención (examen / trabajo / cambio de fecha / consulta)
  y asignatura contra el vocabulario del usuario (asignaturas registradas + lista estándar de
  2.º Bachillerato, fuzzy matching con `difflib`).
- Si hay IA configurada (`AI_PROVIDER=openai`), la IA se usa para辅助 detectar intención y
  extraer estructura cuando el parser determinista no llega; el resultado pasa SIEMPRE por
  validación pydantic y por los mismos servicios con ownership. La IA nunca escribe en la BD
  ni decide fechas al azar: propose-only.
- Documentos: la extracción es regex/parser locales (PDF/DOCX/TXT; OCR opcional con Tesseract
  si está instalado). La IA solo se usa, si está activada, para clasificar líneas ambiguas;
  el usuario confirma todo antes de crear.

## 8. Seguridad

- Token/keys solo en `.env` (gitignored) — nunca en el código ni en el log.
- `documents/` filenames UUID generados; nunca se usa el nombre original en disco;
  WindowsDefender-style borrado de archivos; path traversal imposible por construcción
  (`safe_filename` y rutas absolutas bajo `storage/`).
- Callbacks: `ecb:<accion>:<args>` validados y con comprobación de ownership a nivel servicio.
- Lista blanca de admin por telegram_user_id (env `OWNER_TELEGRAM_IDS`).
- Contenido de documentos y mensajes se trata como no confiable: nunca se evalúa como código,
  ni se envía a logs; a la IA solo se envían extractos necesarios y nunca a log.
- Extranjeras prohibidas: sin requests síncronos en el loop; OCR/exec fuera del loop de eventos.

## 9. Limitaciones documentadas (importante)

- **No** se puede notificar con el PC apagado: los recordatorios vencidos durante la caída se
  reconcilian al arrancar según política (individual / resumen consolidado).
- Telegram solo conserva updates entrantes no entregados ≤ 24 h: los comandos enviados al bot
  apagado pueden no procesarse.
- Sin Tesseract instalado, los PDFs escaneados/imágenes no van con OCR (se marcan como
  `needs_review` para verlos el usuario a mano).
- La entrega exactamente-once de Telegram es inalcanzable; en el peor caso, un recordatorio
  cuyo resultado quedó ambiguo puede enviarse dos veces tras reinicio (probabilidad baja).
