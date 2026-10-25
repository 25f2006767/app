"""Read-only JSON API (session-authenticated, role-aware).

GET /api/exams            all roles (students see non-draft exams only)
GET /api/exams/<id>       all roles
GET /api/slots?exam_id=   all roles
GET /api/bookings         admin: all; examiner: own slots; student: own
GET /api/examiners        admin only
GET /api/students         admin only
"""
from flask import Blueprint, abort, g, jsonify, request

from db import q, q1

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.before_request
def need_login():
    if g.user is None:
        return jsonify(error="authentication required"), 401


def rows(sql, args=()):
    return [dict(r) for r in q(sql, args)]


def admin_only():
    if g.user["role"] != "admin":
        abort(403)


@bp.route("/exams")
def exams():
    sql = """SELECT e.id, e.name, e.exam_type, e.duration_minutes, e.max_marks, e.status, e.slot_create_start,
                    e.slot_create_end, e.book_start, e.book_end, c.code AS course_code, c.name AS course_name
             FROM exams e JOIN courses c ON c.id=e.course_id"""
    if g.user["role"] == "student":
        sql += " WHERE e.status!='Draft'"
    return jsonify(rows(sql + " ORDER BY e.id"))


@bp.route("/exams/<int:eid>")
def exam(eid):
    e = q1("""SELECT e.*, c.code AS course_code, c.name AS course_name FROM exams e
              JOIN courses c ON c.id=e.course_id WHERE e.id=?""", (eid,))
    if e is None or (g.user["role"] == "student" and e["status"] == "Draft"):
        abort(404)
    out = dict(e)
    out["rubrics"] = rows("SELECT id, criterion, max_marks, weightage, description FROM rubrics WHERE exam_id=?", (eid,))
    return jsonify(out)


@bp.route("/slots")
def slots():
    sql = """SELECT s.id, s.exam_id, s.examiner_id, u.name AS examiner_name, s.slot_date, s.start_time, s.end_time,
                    s.capacity, s.available_seats, s.status
             FROM slots s JOIN users u ON u.id=s.examiner_id JOIN exams e ON e.id=s.exam_id WHERE 1=1"""
    args = []
    if request.args.get("exam_id"):
        sql += " AND s.exam_id=?"
        args.append(request.args["exam_id"])
    if g.user["role"] == "student":
        sql += " AND e.status!='Draft' AND s.status!='Cancelled'"
    elif g.user["role"] == "examiner":
        sql += " AND s.examiner_id=?"
        args.append(g.user["id"])
    return jsonify(rows(sql + " ORDER BY s.slot_date, s.start_time", args))


@bp.route("/bookings")
def bookings():
    sql = """SELECT b.id, b.student_id, b.slot_id, b.exam_id, b.booked_at, b.status, s.slot_date, s.start_time
             FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE 1=1"""
    args = []
    if g.user["role"] == "student":
        sql += " AND b.student_id=?"
        args.append(g.user["id"])
    elif g.user["role"] == "examiner":
        sql += " AND s.examiner_id=?"
        args.append(g.user["id"])
    return jsonify(rows(sql + " ORDER BY b.id", args))


@bp.route("/examiners")
def examiners():
    admin_only()
    return jsonify(rows("""SELECT u.id, u.name, u.email, u.contact, ep.department, ep.status FROM users u
                           JOIN examiner_profiles ep ON ep.user_id=u.id ORDER BY u.id"""))


@bp.route("/students")
def students():
    admin_only()
    return jsonify(rows("""SELECT u.id, u.name, u.email, sp.roll_no, sp.program FROM users u
                           JOIN student_profiles sp ON sp.user_id=u.id ORDER BY u.id"""))
