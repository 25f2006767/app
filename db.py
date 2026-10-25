"""Database layer: schema, connection handling and transaction helper.

The SQLite database is created programmatically by init_db() on first run.
"""
import sqlite3
from contextlib import contextmanager

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role          TEXT NOT NULL CHECK (role IN ('admin','examiner','student')),
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    contact       TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS examiner_profiles (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    department  TEXT NOT NULL,
    designation TEXT,
    status      TEXT NOT NULL DEFAULT 'Pending'
                CHECK (status IN ('Pending','Approved','Deactivated'))
);

CREATE TABLE IF NOT EXISTS student_profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    roll_no TEXT NOT NULL UNIQUE,
    program TEXT
);

CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    credits     INTEGER NOT NULL DEFAULT 4,
    status      TEXT NOT NULL DEFAULT 'Active' CHECK (status IN ('Active','Inactive'))
);

CREATE TABLE IF NOT EXISTS exams (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id         INTEGER NOT NULL REFERENCES courses(id),
    name              TEXT NOT NULL,
    exam_type         TEXT NOT NULL
                      CHECK (exam_type IN ('Viva','Practical','Project Demo','Assessment')),
    duration_minutes  INTEGER NOT NULL,
    max_marks         REAL NOT NULL,
    slot_create_start TEXT,
    slot_create_end   TEXT,
    book_start        TEXT,
    book_end          TEXT,
    status            TEXT NOT NULL DEFAULT 'Draft'
                      CHECK (status IN ('Draft','Slot Creation','Booking Open','Closed','Completed')),
    results_published INTEGER NOT NULL DEFAULT 0,
    venue             TEXT,
    instructions      TEXT
);

CREATE TABLE IF NOT EXISTS exam_examiners (
    exam_id     INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    examiner_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (exam_id, examiner_id)
);

CREATE TABLE IF NOT EXISTS rubrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id     INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    criterion   TEXT NOT NULL,
    max_marks   REAL NOT NULL,
    weightage   REAL NOT NULL DEFAULT 0,
    description TEXT
);

CREATE TABLE IF NOT EXISTS slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id         INTEGER NOT NULL REFERENCES exams(id),
    examiner_id     INTEGER NOT NULL REFERENCES users(id),
    slot_date       TEXT NOT NULL,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    capacity        INTEGER NOT NULL CHECK (capacity >= 1),
    available_seats INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'Available'
                    CHECK (status IN ('Available','Full','Cancelled','Completed')),
    venue           TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id   INTEGER NOT NULL REFERENCES users(id),
    slot_id      INTEGER NOT NULL REFERENCES slots(id),
    exam_id      INTEGER NOT NULL REFERENCES exams(id),
    booked_at    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'Booked'
                 CHECK (status IN ('Booked','Cancelled','Completed')),
    cancelled_at TEXT
);

-- A student can hold only one live booking per examination (cancelled ones don't count).
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_live_booking
    ON bookings(student_id, exam_id) WHERE status IN ('Booked','Completed');

CREATE TABLE IF NOT EXISTS booking_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    student_id INTEGER NOT NULL REFERENCES users(id),
    action     TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id   INTEGER NOT NULL UNIQUE REFERENCES bookings(id),
    examiner_id  INTEGER NOT NULL REFERENCES users(id),
    total_marks  REAL NOT NULL,
    remarks      TEXT,
    evaluated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_scores (
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    rubric_id     INTEGER NOT NULL REFERENCES rubrics(id),
    marks         REAL NOT NULL,
    PRIMARY KEY (evaluation_id, rubric_id)
);
"""


class Row(sqlite3.Row):
    """sqlite3.Row with a dict-style .get() so templates can treat rows like form data."""

    def get(self, key, default=None):
        try:
            value = self[key]
        except (IndexError, KeyError):
            return default
        return default if value is None else value


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=15, isolation_level=None)
        conn.row_factory = Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def tx():
    """Serialised write transaction (BEGIN IMMEDIATE) -- prevents overbooking races."""
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def q(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def q1(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def init_db(app):
    """Create all tables and the pre-existing Admin account (no admin registration in the UI)."""
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    email = app.config["ADMIN_EMAIL"].lower()
    if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        conn.execute(
            "INSERT INTO users(role,name,email,password_hash) VALUES('admin',?,?,?)",
            (app.config["ADMIN_NAME"], email, generate_password_hash(app.config["ADMIN_PASSWORD"])),
        )
    conn.commit()
    conn.close()
