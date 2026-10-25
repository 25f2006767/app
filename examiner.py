"""Examiner blueprint: assigned exams, slot management, booked students, rubric evaluation."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import role_guard
from db import q, q1, tx
from utils import (RuleError, examiner_clash, in_window, log_history, now, refresh_slot, text,
                   to_float, to_int, today, valid_date, valid_time, weighted_total)

bp = Blueprint("examiner", __name__, url_prefix="/examiner")
bp.before_request(role_guard("examiner"))


def my_exam_or_403(eid):
    exam = q1(
        """SELECT e.*, c.code AS course_code, c.name AS course_name FROM exams e
           JOIN courses c ON c.id=e.course_id
           JOIN exam_examiners ee ON ee.exam_id=e.id AND ee.examiner_id=?
           WHERE e.id=?""", (g.user["id"], eid))
    if exam is None:
        abort(403)
    return exam


def my_slot_or_404(sid):
    slot = q1("SELECT * FROM slots WHERE id=? AND examiner_id=?", (sid, g.user["id"]))
    if slot is None:
        abort(404)
    return slot


def can_create_slots(exam):
    return exam["status"] == "Slot Creation" and in_window(exam["slot_create_start"], exam["slot_create_end"])


def can_modify_slot(exam):
    """Slots may be changed or removed only before booking starts."""
    if exam["status"] not in ("Draft", "Slot Creation"):
        return False
    return not (exam["book_start"] and today() >= exam["book_start"])


def slot_form_values(f):
    d = valid_date(f.get("slot_date"), "Date", required=True)
    start, end = valid_time(f.get("start_time"), "Start time"), valid_time(f.get("end_time"), "End time")
    if end <= start:
        raise RuleError("End time must be after the start time.")
    if d < today():
        raise RuleError("Slot date cannot be in the past.")
    cap = to_int(f.get("capacity", ""), "Capacity", 1)
    return d, start, end, cap, text(f, "venue", False)


# --------------------------------------------------------------------------- dashboard
@bp.route("/")
def dashboard():
    uid = g.user["id"]
    exams = q(
        """SELECT e.*, c.code AS course_code,
                  (SELECT COUNT(*) FROM slots s WHERE s.exam_id=e.id AND s.examiner_id=? AND s.status!='Cancelled') AS my_slots
           FROM exams e JOIN courses c ON c.id=e.course_id
           JOIN exam_examiners ee ON ee.exam_id=e.id AND ee.examiner_id=?
           ORDER BY e.id DESC""", (uid, uid))
    slots = q(
        """SELECT s.*, e.name AS exam_name,
                  (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status IN ('Booked','Completed')) AS booked
           FROM slots s JOIN exams e ON e.id=s.exam_id
           WHERE s.examiner_id=? AND s.status!='Cancelled' ORDER BY s.slot_date, s.start_time""", (uid,))
    booked_total = q1(
        """SELECT COUNT(*) FROM bookings b JOIN slots s ON s.id=b.slot_id
           WHERE s.examiner_id=? AND b.status IN ('Booked','Completed')""", (uid,))[0]
    pending = q1(
        """SELECT COUNT(*) FROM bookings b JOIN slots s ON s.id=b.slot_id
           WHERE s.examiner_id=? AND s.status!='Cancelled' AND b.status='Booked'""", (uid,))[0]
    return render_template("examiner/dashboard.html", exams=exams, slots=slots,
                           booked_total=booked_total, pending=pending)


# --------------------------------------------------------------------------- exams & slots
@bp.route("/exams")
def exams():
    uid = g.user["id"]
    rows = q(
        """SELECT e.*, c.code AS course_code, c.name AS course_name FROM exams e
           JOIN courses c ON c.id=e.course_id
           JOIN exam_examiners ee ON ee.exam_id=e.id AND ee.examiner_id=? ORDER BY e.id DESC""", (uid,))
    return render_template("examiner/exams.html", exams=rows)


@bp.route("/exams/<int:eid>", methods=["GET", "POST"])
def exam_detail(eid):
    exam = my_exam_or_403(eid)
    uid = g.user["id"]
    if request.method == "POST":
        try:
            d, start, end, cap, venue = slot_form_values(request.form)
            with tx() as db:
                fresh = db.execute("SELECT * FROM exams WHERE id=?", (eid,)).fetchone()
                if not can_create_slots(fresh):
                    raise RuleError("Slot creation is not open for this examination right now.")
                if examiner_clash(db, uid, d, start, end):
                    raise RuleError("You already have a slot at an overlapping time on that date.")
                db.execute(
                    """INSERT INTO slots(exam_id,examiner_id,slot_date,start_time,end_time,capacity,available_seats,venue)
                       VALUES(?,?,?,?,?,?,?,?)""", (eid, uid, d, start, end, cap, cap, venue or fresh["venue"]))
            flash("Slot created.", "success")
            return redirect(url_for("examiner.exam_detail", eid=eid))
        except RuleError as e:
            flash(str(e), "danger")
    slots = q(
        """SELECT s.*, (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status IN ('Booked','Completed')) AS booked
           FROM slots s WHERE s.exam_id=? AND s.examiner_id=? ORDER BY s.slot_date, s.start_time""", (eid, uid))
    rubrics = q("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (eid,))
    return render_template("examiner/exam_detail.html", exam=exam, slots=slots, rubrics=rubrics,
                           can_create=can_create_slots(exam), can_modify=can_modify_slot(exam))


@bp.route("/slots/<int:sid>/edit", methods=["GET", "POST"])
def slot_edit(sid):
    slot = my_slot_or_404(sid)
    exam = my_exam_or_403(slot["exam_id"])
    if request.method == "POST":
        try:
            d, start, end, cap, venue = slot_form_values(request.form)
            with tx() as db:
                fresh = db.execute("SELECT * FROM exams WHERE id=?", (exam["id"],)).fetchone()
                if not can_modify_slot(fresh):
                    raise RuleError("Slots can only be updated before booking starts.")
                if slot["status"] == "Cancelled":
                    raise RuleError("A cancelled slot cannot be edited.")
                if db.execute("SELECT 1 FROM bookings WHERE slot_id=? AND status IN ('Booked','Completed')", (sid,)).fetchone():
                    raise RuleError("Students have already booked this slot.")
                if examiner_clash(db, g.user["id"], d, start, end, sid):
                    raise RuleError("You already have a slot at an overlapping time on that date.")
                db.execute(
                    "UPDATE slots SET slot_date=?,start_time=?,end_time=?,capacity=?,available_seats=?,venue=?,status='Available' WHERE id=?",
                    (d, start, end, cap, cap, venue, sid))
            flash("Slot updated.", "success")
            return redirect(url_for("examiner.exam_detail", eid=exam["id"]))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("examiner/slot_form.html", slot=slot, exam=exam)


@bp.route("/slots/<int:sid>/delete", methods=["POST"])
def slot_delete(sid):
    slot = my_slot_or_404(sid)
    exam = my_exam_or_403(slot["exam_id"])
    try:
        with tx() as db:
            fresh = db.execute("SELECT * FROM exams WHERE id=?", (exam["id"],)).fetchone()
            if not can_modify_slot(fresh):
                raise RuleError("Slots can only be removed before booking starts.")
            if db.execute("SELECT 1 FROM bookings WHERE slot_id=? AND status IN ('Booked','Completed')", (sid,)).fetchone():
                raise RuleError("Students have already booked this slot.")
            if db.execute("SELECT 1 FROM bookings WHERE slot_id=?", (sid,)).fetchone():
                db.execute("UPDATE slots SET status='Cancelled', available_seats=0 WHERE id=?", (sid,))  # keep history
            else:
                db.execute("DELETE FROM slots WHERE id=?", (sid,))
        flash("Slot removed.", "success")
    except RuleError as e:
        flash(str(e), "danger")
    return redirect(url_for("examiner.exam_detail", eid=exam["id"]))


# --------------------------------------------------------------------------- students & evaluation
@bp.route("/slots/<int:sid>")
def slot_students(sid):
    slot = my_slot_or_404(sid)
    exam = my_exam_or_403(slot["exam_id"])
    students = q(
        """SELECT b.id AS booking_id, b.status, b.booked_at, u.id AS student_id, u.name, u.email, sp.roll_no,
                  ev.total_marks
           FROM bookings b JOIN users u ON u.id=b.student_id JOIN student_profiles sp ON sp.user_id=u.id
           LEFT JOIN evaluations ev ON ev.booking_id=b.id
           WHERE b.slot_id=? AND b.status IN ('Booked','Completed') ORDER BY u.name""", (sid,))
    return render_template("examiner/slot_students.html", slot=slot, exam=exam, students=students)


@bp.route("/evaluations")
def evaluations():
    uid = g.user["id"]
    rows = q(
        """SELECT b.id AS booking_id, b.status, u.name, sp.roll_no, e.name AS exam_name, e.max_marks,
                  s.slot_date, s.start_time, ev.total_marks
           FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id
           JOIN users u ON u.id=b.student_id JOIN student_profiles sp ON sp.user_id=u.id
           LEFT JOIN evaluations ev ON ev.booking_id=b.id
           WHERE s.examiner_id=? AND s.status!='Cancelled' AND b.status IN ('Booked','Completed')
           ORDER BY (b.status='Booked') DESC, s.slot_date, s.start_time""", (uid,))
    return render_template("examiner/evaluations.html", rows=rows)


@bp.route("/bookings/<int:bid>/evaluate", methods=["GET", "POST"])
def evaluate(bid):
    # Examiners may only evaluate students booked into THEIR OWN slots.
    booking = q1(
        """SELECT b.*, u.name AS student_name, sp.roll_no, s.slot_date, s.start_time, s.end_time, s.examiner_id,
                  e.name AS exam_name, e.max_marks, e.results_published, e.id AS the_exam_id
           FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id
           JOIN users u ON u.id=b.student_id JOIN student_profiles sp ON sp.user_id=u.id
           WHERE b.id=? AND s.examiner_id=? AND b.status IN ('Booked','Completed')""", (bid, g.user["id"]))
    if booking is None:
        abort(403)
    rubrics = q("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (booking["exam_id"],))
    existing = q1("SELECT * FROM evaluations WHERE booking_id=?", (bid,))
    old_scores = {}
    if existing:
        old_scores = {r["rubric_id"]: r["marks"] for r in
                      q("SELECT rubric_id, marks FROM evaluation_scores WHERE evaluation_id=?", (existing["id"],))}
    if request.method == "POST":
        try:
            if not rubrics:
                raise RuleError("The Admin has not defined rubrics for this examination yet.")
            if booking["results_published"]:
                raise RuleError("Results are already published; marks can no longer be changed.")
            marks = {}
            for r in rubrics:
                m = to_float(request.form.get(f"r_{r['id']}", ""), f"Marks for '{r['criterion']}'", 0)
                if m > r["max_marks"]:
                    raise RuleError(f"Marks for '{r['criterion']}' cannot exceed {r['max_marks']:g}.")
                marks[r["id"]] = m
            remarks = text(request.form, "remarks", False)
            total = weighted_total(rubrics, marks, booking["max_marks"])
            with tx() as db:
                ev = db.execute("SELECT id FROM evaluations WHERE booking_id=?", (bid,)).fetchone()
                if ev:
                    db.execute("UPDATE evaluations SET total_marks=?, remarks=?, evaluated_at=?, examiner_id=? WHERE id=?",
                               (total, remarks, now(), g.user["id"], ev["id"]))
                    ev_id = ev["id"]
                    db.execute("DELETE FROM evaluation_scores WHERE evaluation_id=?", (ev_id,))
                else:
                    ev_id = db.execute(
                        "INSERT INTO evaluations(booking_id,examiner_id,total_marks,remarks,evaluated_at) VALUES(?,?,?,?,?)",
                        (bid, g.user["id"], total, remarks, now())).lastrowid
                for rid, m in marks.items():
                    db.execute("INSERT INTO evaluation_scores(evaluation_id,rubric_id,marks) VALUES(?,?,?)", (ev_id, rid, m))
                db.execute("UPDATE bookings SET status='Completed' WHERE id=?", (bid,))
                log_history(db, bid, booking["student_id"], "Evaluated", f"Marks submitted: {total:g}/{booking['max_marks']:g}")
                refresh_slot(db, booking["slot_id"])
            flash(f"Marks submitted ({total:g} / {booking['max_marks']:g}).", "success")
            return redirect(url_for("examiner.slot_students", sid=booking["slot_id"]))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("examiner/evaluate.html", booking=booking, rubrics=rubrics,
                           existing=existing, old_scores=old_scores, form=request.form)
