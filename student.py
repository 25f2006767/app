"""Student blueprint: browse exams, book/cancel slots, history, results, scorecard."""
from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request,
                   url_for)

from auth import role_guard
from db import q, q1, tx
from utils import RuleError, in_window, log_history, now, refresh_slot, student_clash, today

bp = Blueprint("student", __name__, url_prefix="/student")
bp.before_request(role_guard("student"))

EXAM_TYPES = ["Viva", "Practical", "Project Demo", "Assessment"]


def booking_open(exam):
    return exam["status"] == "Booking Open" and in_window(exam["book_start"], exam["book_end"])


# --------------------------------------------------------------------------- dashboard
@bp.route("/")
def dashboard():
    uid = g.user["id"]
    open_exams = [e for e in q(
        """SELECT e.*, c.code AS course_code, c.name AS course_name,
                  (SELECT COUNT(*) FROM slots s WHERE s.exam_id=e.id AND s.status='Available' AND s.available_seats>0) AS free_slots,
                  (SELECT COUNT(*) FROM bookings b WHERE b.exam_id=e.id AND b.student_id=? AND b.status IN ('Booked','Completed')) AS mine
           FROM exams e JOIN courses c ON c.id=e.course_id WHERE e.status='Booking Open' ORDER BY e.book_end""", (uid,))
        if booking_open(e)]
    open_ids = [e["id"] for e in open_exams if not e["mine"]]
    slots = []
    if open_ids:
        marks = ",".join("?" * len(open_ids))
        slots = q(
            f"""SELECT s.*, e.name AS exam_name, u.name AS examiner_name FROM slots s
                JOIN exams e ON e.id=s.exam_id JOIN users u ON u.id=s.examiner_id
                WHERE s.exam_id IN ({marks}) AND s.status='Available' AND s.available_seats>0
                ORDER BY s.slot_date, s.start_time LIMIT 8""", open_ids)
    booked = q(
        """SELECT b.id, b.status, s.slot_date, s.start_time, s.end_time, s.venue, e.name AS exam_name, e.exam_type,
                  e.book_end, e.status AS exam_status, c.code AS course_code, u.name AS examiner_name
           FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id
           JOIN courses c ON c.id=e.course_id JOIN users u ON u.id=s.examiner_id
           WHERE b.student_id=? AND b.status='Booked' ORDER BY s.slot_date, s.start_time""", (uid,))
    upcoming = [b for b in booked if b["slot_date"] >= today()]
    results = q(
        """SELECT b.id AS booking_id, e.name AS exam_name, e.max_marks, ev.total_marks, c.code AS course_code
           FROM bookings b JOIN evaluations ev ON ev.booking_id=b.id JOIN exams e ON e.id=b.exam_id
           JOIN courses c ON c.id=e.course_id
           WHERE b.student_id=? AND e.results_published=1 ORDER BY ev.evaluated_at DESC LIMIT 5""", (uid,))
    return render_template("student/dashboard.html", open_exams=open_exams, slots=slots,
                           upcoming=upcoming, booked=booked, results=results)


# --------------------------------------------------------------------------- exams
@bp.route("/exams")
def exams():
    uid = g.user["id"]
    term = request.args.get("q", "").strip()
    etype = request.args.get("type", "")
    course = request.args.get("course", "")
    only_open = request.args.get("open") == "1"
    sql = """SELECT e.*, c.code AS course_code, c.name AS course_name,
                    (SELECT COUNT(*) FROM slots s WHERE s.exam_id=e.id AND s.status='Available' AND s.available_seats>0) AS free_slots,
                    (SELECT COUNT(*) FROM bookings b WHERE b.exam_id=e.id AND b.student_id=? AND b.status IN ('Booked','Completed')) AS mine
             FROM exams e JOIN courses c ON c.id=e.course_id
             WHERE e.status!='Draft' AND (e.name LIKE ? OR c.code LIKE ? OR c.name LIKE ?)"""
    args = [uid, f"%{term}%", f"%{term}%", f"%{term}%"]
    if etype:
        sql += " AND e.exam_type=?"
        args.append(etype)
    if course:
        sql += " AND e.course_id=?"
        args.append(course)
    if only_open:
        sql += " AND e.status='Booking Open'"
    rows = q(sql + " ORDER BY e.id DESC", args)
    return render_template("student/exams.html", exams=rows, term=term, etype=etype, course=course,
                           only_open=only_open, types=EXAM_TYPES,
                           courses=q("SELECT id, code FROM courses WHERE status='Active' ORDER BY code"),
                           booking_open=booking_open)


@bp.route("/exams/<int:eid>")
def exam_detail(eid):
    uid = g.user["id"]
    exam = q1("SELECT e.*, c.code AS course_code, c.name AS course_name FROM exams e "
              "JOIN courses c ON c.id=e.course_id WHERE e.id=? AND e.status!='Draft'", (eid,))
    if exam is None:
        abort(404)
    slots = q(
        """SELECT s.*, u.name AS examiner_name FROM slots s JOIN users u ON u.id=s.examiner_id
           WHERE s.exam_id=? AND s.status!='Cancelled' ORDER BY s.slot_date, s.start_time""", (eid,))
    mine = q1("SELECT * FROM bookings WHERE exam_id=? AND student_id=? AND status IN ('Booked','Completed')", (eid, uid))
    rubrics = q("SELECT criterion, max_marks, weightage, description FROM rubrics WHERE exam_id=? ORDER BY id", (eid,))
    return render_template("student/exam_detail.html", exam=exam, slots=slots, mine=mine, rubrics=rubrics,
                           open_now=booking_open(exam))


# --------------------------------------------------------------------------- booking
@bp.route("/slots/<int:sid>/book", methods=["POST"])
def book(sid):
    uid = g.user["id"]
    exam_id = None
    try:
        with tx() as db:  # BEGIN IMMEDIATE serialises concurrent bookings -> no overbooking
            slot = db.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone()
            if slot is None:
                abort(404)
            exam_id = slot["exam_id"]
            exam = db.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
            if not booking_open(exam):
                raise RuleError("Booking is not open for this examination.")
            if slot["status"] != "Available" or slot["available_seats"] < 1:
                raise RuleError("This slot is full or no longer available.")
            if db.execute("SELECT 1 FROM bookings WHERE student_id=? AND exam_id=? AND status IN ('Booked','Completed')",
                          (uid, exam_id)).fetchone():
                raise RuleError("You already have a booking for this examination.")
            clash = student_clash(db, uid, slot["slot_date"], slot["start_time"], slot["end_time"])
            if clash:
                raise RuleError(f"This overlaps with your booking for '{clash['name']}'.")
            bid = db.execute("INSERT INTO bookings(student_id,slot_id,exam_id,booked_at,status) VALUES(?,?,?,?,'Booked')",
                             (uid, sid, exam_id, now())).lastrowid
            log_history(db, bid, uid, "Booked", f"Slot #{sid} on {slot['slot_date']} {slot['start_time']}-{slot['end_time']}")
            refresh_slot(db, sid)  # closes the slot automatically when capacity is reached
        flash("Slot booked successfully.", "success")
        return redirect(url_for("student.bookings"))
    except RuleError as e:
        flash(str(e), "danger")
    return redirect(url_for("student.exam_detail", eid=exam_id) if exam_id else url_for("student.exams"))


@bp.route("/bookings/<int:bid>/cancel", methods=["POST"])
def cancel(bid):
    uid = g.user["id"]
    try:
        with tx() as db:
            b = db.execute("SELECT * FROM bookings WHERE id=? AND student_id=?", (bid, uid)).fetchone()
            if b is None:
                abort(404)
            exam = db.execute("SELECT * FROM exams WHERE id=?", (b["exam_id"],)).fetchone()
            if b["status"] != "Booked":
                raise RuleError("Only active bookings can be cancelled.")
            if exam["status"] != "Booking Open" or (exam["book_end"] and today() > exam["book_end"]):
                raise RuleError("The booking deadline has passed; this booking can no longer be cancelled.")
            db.execute("UPDATE bookings SET status='Cancelled', cancelled_at=? WHERE id=?", (now(), bid))
            log_history(db, bid, uid, "Cancelled", f"Cancelled by student (slot #{b['slot_id']})")
            refresh_slot(db, b["slot_id"])
        flash("Booking cancelled.", "success")
    except RuleError as e:
        flash(str(e), "danger")
    return redirect(url_for("student.bookings"))


@bp.route("/bookings")
def bookings():
    uid = g.user["id"]
    rows = q(
        """SELECT b.*, s.slot_date, s.start_time, s.end_time, s.venue, e.name AS exam_name, e.exam_type,
                  e.status AS exam_status, e.book_end, e.results_published, c.code AS course_code,
                  u.name AS examiner_name, ev.total_marks, e.max_marks
           FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id
           JOIN courses c ON c.id=e.course_id JOIN users u ON u.id=s.examiner_id
           LEFT JOIN evaluations ev ON ev.booking_id=b.id
           WHERE b.student_id=? ORDER BY s.slot_date DESC, s.start_time DESC""", (uid,))
    history = q("SELECT * FROM booking_history WHERE student_id=? ORDER BY id DESC", (uid,))
    return render_template("student/bookings.html", rows=rows, history=history, today=today(),
                           can_cancel=lambda r: r["status"] == "Booked" and r["exam_status"] == "Booking Open"
                           and (not r["book_end"] or today() <= r["book_end"]))


# --------------------------------------------------------------------------- results
def _result(bid):
    uid = g.user["id"]
    res = q1(
        """SELECT b.id AS booking_id, ev.id AS ev_id, ev.total_marks, ev.remarks, ev.evaluated_at, e.name AS exam_name,
                  e.exam_type, e.max_marks, c.code AS course_code, c.name AS course_name, x.name AS examiner_name,
                  s.slot_date, s.start_time
           FROM bookings b JOIN evaluations ev ON ev.booking_id=b.id JOIN exams e ON e.id=b.exam_id
           JOIN courses c ON c.id=e.course_id JOIN slots s ON s.id=b.slot_id JOIN users x ON x.id=ev.examiner_id
           WHERE b.id=? AND b.student_id=? AND e.results_published=1""", (bid, uid))
    if res is None:
        abort(404)
    scores = q(
        """SELECT r.criterion, r.max_marks, r.weightage, sc.marks FROM evaluation_scores sc
           JOIN rubrics r ON r.id=sc.rubric_id WHERE sc.evaluation_id=? ORDER BY r.id""", (res["ev_id"],))
    return res, scores


@bp.route("/results")
def results():
    rows = q(
        """SELECT b.id AS booking_id, e.name AS exam_name, e.max_marks, c.code AS course_code, ev.total_marks,
                  ev.remarks, ev.evaluated_at
           FROM bookings b JOIN evaluations ev ON ev.booking_id=b.id JOIN exams e ON e.id=b.exam_id
           JOIN courses c ON c.id=e.course_id
           WHERE b.student_id=? AND e.results_published=1 ORDER BY ev.evaluated_at DESC""", (g.user["id"],))
    return render_template("student/results.html", rows=rows)


@bp.route("/results/<int:bid>")
def result_detail(bid):
    res, scores = _result(bid)
    return render_template("student/result_detail.html", res=res, scores=scores)


@bp.route("/results/<int:bid>/scorecard")
def scorecard(bid):
    res, scores = _result(bid)
    html = render_template("student/scorecard.html", res=res, scores=scores)
    if request.args.get("download"):
        return Response(html, mimetype="text/html",
                        headers={"Content-Disposition": f"attachment; filename=scorecard_{bid}.html"})
    return html
