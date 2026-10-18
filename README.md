EstudiaCabronBot
================

Bot de Telegram para estudiantes de 2.o de Bachillerato: examenes, trabajos,
recordatorios por adelantado, plan de estudio automatico y notas de progreso.
Todo tu vocabulario esta en Espanol y TODO se guarda en tu propio ordenador
(SQLite como fuente de verdad + backups locales).

Caracteristicas
---------------
- Registro inicial con /start (nombre, zona horaria con por defecto Europe/Madrid)
- /hoy, /semana, /examenes, /tareas, /progreso  -- vistas de tu dia a dia
- /anadir paso-a-paso para crear examenes y tareas (elige tipo, asignatura, titulo, fecha)
- Lenguaje natural: puedes escribir directamente "Tengo un examen de Historia el jueves"
  o "He terminado el tema 3 de Biologia" y el bot lo saca
- /archivos: sube un PDF/DOCX/TXT o una foto de tu programa y crea los exam y 
  entregas que detecta, uno a uno, tras tu confirmacion
- /plan genera un plan de estudio determinista (pure-cualitativo, no IA necesaria),
  con margenes por dificultad y pesos por importancia y proximidad
- /disponibilidad para definir minutos de estudio por dia
- /recordatorios: resumen diario, horas de silencio, catch-up tras no abrir el bot
- /perfil, /privacidad, /borrarme (borrado en cascada), /cancelar
- Owner: /estado diagnostico completo del sistema

Arquitectura (basica)
---------------------
- Python 3.14 + python-telegram-bot v22 + SQLAlchemy async + SQLite (WAL)
- Outbox de recordatorios transaccional: nada se envia que no este antes en la DB; 
  por cada paso se hace claim atomico para no duplicar mensajes
- Horarios persistentes: si el bot esta apagado, al arrancar ejecuta catch-up
  (resumen por defecto, max_backlog=10) y arregla el outbox (recover_stuck_processing)
- Zona horaria profesional: almacenamiento en UTC, mensajes en tu tz
- Backups: copia diaria tipo "snapshot" con sqlite3 backup API en backups/
  (rotacion de 14 copias; el tick corre a las 04:30)
- Migraciones con Alembic (db/app.db inicializado con `alembic upgrade head`)

Arranque
--------
1. python -m venv .venv
2. .venv\Scripts\pip install -r requirements.txt
3. Copia .env.example a .env y rellena TELEGRAM_BOT_TOKEN (obligatorio).
   OWNER_TELEGRAM_IDS puede ir vacio (entonces /estado no esta disponible);
   AI_PROVIDER vacio significa 'sin IA' (todo es determinista y local).
4. Arranca doble script o consola:
       powershell -ExecutionPolicy Bypass -File scripts\start_bot.ps1
   (o: .venv\Scripts\python -m app.main)

Backups y restauracion
----------------------
- Auto: diariamente a las 04:30 (UTC) via tick en app.main
- Manual: powershell -ExecutionPolicy Bypass -File scripts\backup.ps1
- Restaurar manual: apaga el bot, copia el backup que quieras de data/backups/
  sobre db app.db (p.ej. copy data\backups\estudia_2026xxxx.db data\app.db) y 
  arranca de nuevo: al subir, Alembic verificara el esquema.

Migraciones
-----------
- Crear nueva migracion tras tocar app\database\models.py:
      .venv\Scripts\alembic revision --autogenerate -m "mi Cambio"
- Aplicar: .venv\Scripts\alembic upgrade head

Tests
-----
- Corre: .venv\Scripts\pytest -q
- Cubren: parseo de fechas en Espanol, planificador determinista, 
  deduplicacion de examenes/tareas, outbox de recordatorios (claim atomico,
  reintentos, expiracion, catch-up con resumen), borrado en cascada, etc.

Estimaciones de estudio
-----------------------
- El plan usa est_prep_minutes afectos a dificultad e importancia del examen
  y est_minutes de cada tarea. Todo es determinista y corre en milisegundos 
  (sin IA, por defecto).

Privacidad y datos
------------------
- Todos los datos en: db/app.db (SQLite) y data/storage/ (documentos extraidos)
- Copias de seguridad: data/backups/ (14 copias max)
- Logs: logs/estudia_bot.log (rotado por archivo) y consola
- /borrarme BORRA TODO en cascada en la DB (audit + datos + recordatorios)

Solucion de problemas rapidos
-----------------------------
- 'Falta TELEGRAM_BOT_TOKEN en .env' -> rellena el .env (token de @BotFather)
- Error de OCR 'Tesseract no esta instalado' -> instala tesseract-ocr
- 'no such table users' tras usar otra DATABASE_URL -> ejecuta 
  run_startup_recovery al arrancar; el ordenador crea muestras y las limpias
  en app/db si hace falta: alembic upgrade head
- No entiendo fecha ('jueves', '12/11', '12 de noviembre a las 9') -> 
  cambia o usa /anadir guiado
- persistent WAL lock en Windows -> cierra otro proceso con la DB abierta
  (p.ej. DB Browser for SQLite lo bloquea)
