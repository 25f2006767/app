"""Shared helpers: business-rule error, date helpers, validation, slot/booking bookkeeping."""
import secrets
from datetime import date, datetime

from flask import session


class RuleError(Exception):
    """Raised when a business rule is violated. The message is shown to the user."""


def today():
    return date.today().isoformat()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def in_window(start, end, d=None):
    """True if date d (default today) lies inside [start, end]. Missing bounds are open."""
    d = d or today()
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def text(form, name, required=True, label=None):
    val = (form.get(name) or "").strip()
    if required and not val:
        raise RuleError(f"{label or name.replace('_', ' ').title()} is required.")
    return val


def to_int(val, label, minimum=None):
    try:
        n = int(str(val).strip())
    except (TypeError, ValueError):
        raise RuleError(f"{label} must be a whole number.")
    if minimum is not None and n < minimum:
        raise RuleError(f"{label} must be at least {minimum}.")
    return n


def to_float(val, label, minimum=None):
    try:
        n = float(str(val).strip())
    except (TypeError, ValueError):
        raise RuleError(f"{label} must be a number.")
    if minimum is not None and n < minimum:
        raise RuleError(f"{label} must be at least {minimum}.")
    return n


def valid_date(val, label, required=False):
    val = (val or "").strip()
    if not val:
        if required:
            raise RuleError(f"{label} is required.")
        return None
    try:
        datetime.strptime(val, "%Y-%m-%d")
    except ValueError:
        raise RuleError(f"{label} must be a valid date.")
    return val


def valid_time(val, label):
    val = (val or "").strip()
    try:
        datetime.strptime(val, "%H:%M")
    except ValueError:
        raise RuleError(f"{label} must be a valid time (HH:MM).")
    return val


def times_overlap(s1, e1, s2, e2):
    return s1 < e2 and s2 < e1


def log_history(db, booking_id, student_id, action, detail=""):
    db.execute(
        "INSERT INTO booking_history(booking_id,student_id,action,detail,created_at) VALUES(?,?,?,?,?)",
        (booking_id, student_id, action, detail, now()),
    )


def refresh_slot(db, slot_id):
    """Recompute available seats and status of a slot from its live bookings.

    Must be called inside a transaction after any booking change. A slot closes
    automatically ('Full') when capacity is reached and re-opens if a seat frees up.
    """
    slot = db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    if slot is None or slot["status"] == "Cancelled":
        return
    live = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status IN ('Booked','Completed')",
        (slot_id,),
    ).fetchone()[0]
    pending = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status='Booked'", (slot_id,)
    ).fetchone()[0]
    available = max(slot["capacity"] - live, 0)
    if live > 0 and pending == 0:
        status = "Completed"
    elif available == 0:
        status = "Full"
    else:
        status = "Available"
    db.execute("UPDATE slots SET available_seats=?, status=? WHERE id=?", (available, status, slot_id))


def check_exam_dates(slot_start, slot_end, book_start, book_end):
    if slot_start and slot_end and slot_start > slot_end:
        raise RuleError("Slot creation end date cannot be before its start date.")
    if book_start and book_end and book_start > book_end:
        raise RuleError("Booking end date cannot be before its start date.")


def student_clash(db, student_id, slot_date, start, end, ignore_booking_id=None):
    """Return the clashing live booking (if any) for the student at that date/time."""
    rows = db.execute(
        """SELECT b.id, s.start_time, s.end_time, e.name FROM bookings b
           JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id
           WHERE b.student_id=? AND b.status='Booked' AND s.slot_date=? AND b.id IS NOT ?""",
        (student_id, slot_date, ignore_booking_id),
    ).fetchall()
    for r in rows:
        if times_overlap(start, end, r["start_time"], r["end_time"]):
            return r
    return None


def examiner_clash(db, examiner_id, slot_date, start, end, ignore_slot_id=None):
    rows = db.execute(
        """SELECT id, start_time, end_time FROM slots
           WHERE examiner_id=? AND slot_date=? AND status!='Cancelled' AND id IS NOT ?""",
        (examiner_id, slot_date, ignore_slot_id),
    ).fetchall()
    return any(times_overlap(start, end, r["start_time"], r["end_time"]) for r in rows)


def weighted_total(rubrics, marks, max_marks):
    """Final marks out of the exam maximum.

    Each criterion contributes (marks/criterion_max) * weightage. If weightages sum to
    zero we fall back to the plain ratio of total marks to total rubric maximum.
    """
    weight_sum = sum(r["weightage"] for r in rubrics)
    if weight_sum > 0:
        frac = sum((marks[r["id"]] / r["max_marks"]) * r["weightage"] for r in rubrics if r["max_marks"] > 0)
        return round(frac / weight_sum * max_marks, 2)
    rub_max = sum(r["max_marks"] for r in rubrics)
    if rub_max <= 0:
        return 0.0
    return round(sum(marks.values()) / rub_max * max_marks, 2)
