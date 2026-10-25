"""Admin blueprint: courses, examinations, rubrics, examiners, slots, bookings, search."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from auth import role_guard
from db import q, q1, tx
from utils import (RuleError, check_exam_dates, examiner_clash, log_history, refresh_slot,
                   student_clash, text, to_float, to_int, valid_date)

bp = Blueprint("admin", __name__, url_prefix="/admin")
bp.before_request(role_guard("admin"))

EXAM_TYPES = ["Viva", "Practical", "Project Demo", "Assessment"]

# action -> (statuses it may be applied from, resulting status, label)
EXAM_ACTIONS = {
    "open_slots": ({"Draft"}, "Slot Creation", "Slot creation opened"),
    "close_slots": ({"Slot Creation"}, "Draft", "Slot creation closed"),
    "open_booking": ({"Draft", "Slot Creation", "Closed"}, "Booking Open", "Student booking opened"),
    "close_booking": ({"Booking Open"}, "Closed", "Student booking closed"),
    "complete": ({"Closed", "Booking Open"}, "Completed", "Examination marked completed"),
}


def get_or_404(sql, args):
    row = q1(sql, args)
    if row is None:
        abort(404)
    return row


# --------------------------------------------------------------------------- dashboard
@bp.route("/")
def dashboard():
    def count(sql):
        return q1(sql)[0]

    stats = {
        "courses": count("SELECT COUNT(*) FROM courses"),
        "exams": count("SELECT COUNT(*) FROM exams"),
        "examiners": count("SELECT COUNT(*) FROM examiner_profiles"),
        "pending_examiners": count("SELECT COUNT(*) FROM examiner_profiles WHERE status='Pending'"),
        "students": count("SELECT COUNT(*) FROM users WHERE role='student'"),
        "slots": count("SELECT COUNT(*) FROM slots"),
        "bookings": count("SELECT COUNT(*) FROM bookings"),
        "live_bookings": count("SELECT COUNT(*) FROM bookings WHERE status IN ('Booked','Completed')"),
    }
    workload = q(
        """SELECT u.id, u.name, COUNT(DISTINCT s.id) AS slots, COUNT(b.id) AS booked
           FROM users u JOIN examiner_profiles ep ON ep.user_id=u.id AND ep.status='Approved'
           LEFT JOIN slots s ON s.examiner_id=u.id AND s.status!='Cancelled'
           LEFT JOIN bookings b ON b.slot_id=s.id AND b.status IN ('Booked','Completed')
           GROUP BY u.id ORDER BY booked DESC, u.name"""
    )
    fill = q(
        """SELECT e.id, e.name, c.code, e.status,
                  COALESCE(SUM(s.capacity),0) AS cap,
                  COALESCE(SUM(s.capacity - s.available_seats),0) AS used
           FROM exams e JOIN courses c ON c.id=e.course_id
           LEFT JOIN slots s ON s.exam_id=e.id AND s.status!='Cancelled'
           GROUP BY e.id ORDER BY e.id DESC"""
    )
    completion = q(
        """SELECT e.id, e.name, COUNT(b.id) AS total,
                  COALESCE(SUM(b.status='Completed'),0) AS done
           FROM exams e LEFT JOIN bookings b ON b.exam_id=e.id AND b.status IN ('Booked','Completed')
           GROUP BY e.id ORDER BY e.id DESC"""
    )
    trend = list(reversed(q(
        "SELECT substr(booked_at,1,10) AS d, COUNT(*) AS n FROM bookings GROUP BY d ORDER BY d DESC LIMIT 14"
    )))
    pending = q(
        """SELECT u.id, u.name, u.email, ep.department FROM users u
           JOIN examiner_profiles ep ON ep.user_id=u.id WHERE ep.status='Pending' ORDER BY u.id"""
    )
    return render_template("admin/dashboard.html", stats=stats, workload=workload, fill=fill,
                           completion=completion, trend=trend, pending=pending)


# --------------------------------------------------------------------------- courses
@bp.route("/courses")
def courses():
    term = request.args.get("q", "").strip()
    rows = q(
        """SELECT c.*, (SELECT COUNT(*) FROM exams e WHERE e.course_id=c.id) AS exam_count
           FROM courses c WHERE c.code LIKE ? OR c.name LIKE ? ORDER BY c.code""",
        (f"%{term}%", f"%{term}%"),
    )
    return render_template("admin/courses.html", courses=rows, term=term)


def _course_form(course=None):
    if request.method == "POST":
        f = request.form
        try:
            code = text(f, "code", label="Course code").upper()
            name = text(f, "name", label="Course name")
            credits = to_int(f.get("credits", ""), "Credits", 0)
            status = f.get("status", "Active")
            if status not in ("Active", "Inactive"):
                raise RuleError("Invalid status.")
            clash = q1("SELECT id FROM courses WHERE code=? AND id IS NOT ?", (code, course["id"] if course else None))
            if clash:
                raise RuleError("A course with that code already exists.")
            with tx() as db:
                if course:
                    db.execute("UPDATE courses SET code=?,name=?,description=?,credits=?,status=? WHERE id=?",
                               (code, name, text(f, "description", False), credits, status, course["id"]))
                else:
                    db.execute("INSERT INTO courses(code,name,description,credits,status) VALUES(?,?,?,?,?)",
                               (code, name, text(f, "description", False), credits, status))
            flash("Course saved.", "success")
            return redirect(url_for("admin.courses"))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("admin/course_form.html", course=course)


@bp.route("/courses/new", methods=["GET", "POST"])
def course_new():
    return _course_form()


@bp.route("/courses/<int:cid>/edit", methods=["GET", "POST"])
def course_edit(cid):
    return _course_form(get_or_404("SELECT * FROM courses WHERE id=?", (cid,)))


@bp.route("/courses/<int:cid>/delete", methods=["POST"])
def course_delete(cid):
    get_or_404("SELECT id FROM courses WHERE id=?", (cid,))
    if q1("SELECT 1 FROM exams WHERE course_id=?", (cid,)):
        flash("This course has examinations. Mark it Inactive instead of deleting it.", "warning")
    else:
        with tx() as db:
            db.execute("DELETE FROM courses WHERE id=?", (cid,))
        flash("Course removed.", "success")
    return redirect(url_for("admin.courses"))


# --------------------------------------------------------------------------- exams
@bp.route("/exams")
def exams():
    term = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    course = request.args.get("course", "")
    sql = """SELECT e.*, c.code AS course_code, c.name AS course_name,
                    (SELECT COUNT(*) FROM slots s WHERE s.exam_id=e.id AND s.status!='Cancelled') AS slot_count,
                    (SELECT COUNT(*) FROM bookings b WHERE b.exam_id=e.id AND b.status IN ('Booked','Completed')) AS booking_count
             FROM exams e JOIN courses c ON c.id=e.course_id
             WHERE (e.name LIKE ? OR c.code LIKE ? OR CAST(e.id AS TEXT)=?)"""
    args = [f"%{term}%", f"%{term}%", term]
    if status:
        sql += " AND e.status=?"
        args.append(status)
    if course:
        sql += " AND e.course_id=?"
        args.append(course)
    rows = q(sql + " ORDER BY e.id DESC", args)
    return render_template("admin/exams.html", exams=rows, term=term, status=status, course=course,
                           courses=q("SELECT id, code FROM courses ORDER BY code"))


def _exam_values(f):
    name = text(f, "name", label="Examination name")
    etype = f.get("exam_type", "")
    if etype not in EXAM_TYPES:
        raise RuleError("Choose a valid examination type.")
    values = dict(
        course_id=to_int(f.get("course_id", ""), "Course", 1),
        name=name, exam_type=etype,
        duration_minutes=to_int(f.get("duration_minutes", ""), "Duration", 1),
        max_marks=to_float(f.get("max_marks", ""), "Maximum marks", 1),
        slot_create_start=valid_date(f.get("slot_create_start"), "Slot creation start"),
        slot_create_end=valid_date(f.get("slot_create_end"), "Slot creation end"),
        book_start=valid_date(f.get("book_start"), "Booking start"),
        book_end=valid_date(f.get("book_end"), "Booking end"),
        venue=text(f, "venue", False), instructions=text(f, "instructions", False),
    )
    check_exam_dates(values["slot_create_start"], values["slot_create_end"], values["book_start"], values["book_end"])
    if not q1("SELECT 1 FROM courses WHERE id=?", (values["course_id"],)):
        raise RuleError("Selected course does not exist.")
    return values


@bp.route("/exams/new", methods=["GET", "POST"])
def exam_new():
    if request.method == "POST":
        try:
            v = _exam_values(request.form)
            with tx() as db:
                eid = db.execute(
                    """INSERT INTO exams(course_id,name,exam_type,duration_minutes,max_marks,slot_create_start,
                       slot_create_end,book_start,book_end,venue,instructions)
                       VALUES(:course_id,:name,:exam_type,:duration_minutes,:max_marks,:slot_create_start,
                       :slot_create_end,:book_start,:book_end,:venue,:instructions)""", v).lastrowid
            flash("Examination created as Draft. Add rubrics and assign examiners next.", "success")
            return redirect(url_for("admin.exam_detail", eid=eid))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("admin/exam_form.html", exam=None, types=EXAM_TYPES,
                           courses=q("SELECT id, code, name FROM courses WHERE status='Active' ORDER BY code"))


@bp.route("/exams/<int:eid>/edit", methods=["GET", "POST"])
def exam_edit(eid):
    exam = get_or_404("SELECT * FROM exams WHERE id=?", (eid,))
    if request.method == "POST":
        try:
            v = _exam_values(request.form)
            v["id"] = eid
            with tx() as db:
                db.execute(
                    """UPDATE exams SET course_id=:course_id,name=:name,exam_type=:exam_type,
                       duration_minutes=:duration_minutes,max_marks=:max_marks,slot_create_start=:slot_create_start,
                       slot_create_end=:slot_create_end,book_start=:book_start,book_end=:book_end,
                       venue=:venue,instructions=:instructions WHERE id=:id""", v)
            flash("Examination updated.", "success")
            return redirect(url_for("admin.exam_detail", eid=eid))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("admin/exam_form.html", exam=exam, types=EXAM_TYPES,
                           courses=q("SELECT id, code, name FROM courses ORDER BY code"))


@bp.route("/exams/<int:eid>/delete", methods=["POST"])
def exam_delete(eid):
    get_or_404("SELECT id FROM exams WHERE id=?", (eid,))
    if q1("SELECT 1 FROM slots WHERE exam_id=?", (eid,)):
        flash("Examinations that have slots cannot be deleted; close or complete them instead.", "warning")
        return redirect(url_for("admin.exam_detail", eid=eid))
    with tx() as db:
        db.execute("DELETE FROM exams WHERE id=?", (eid,))
    flash("Examination removed.", "success")
    return redirect(url_for("admin.exams"))


@bp.route("/exams/<int:eid>")
def exam_detail(eid):
    exam = get_or_404(
        "SELECT e.*, c.code AS course_code, c.name AS course_name FROM exams e JOIN courses c ON c.id=e.course_id WHERE e.id=?",
        (eid,))
    rubrics = q("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (eid,))
    assigned = q(
        """SELECT u.id, u.name, u.email, ep.department FROM exam_examiners ee
           JOIN users u ON u.id=ee.examiner_id JOIN examiner_profiles ep ON ep.user_id=u.id
           WHERE ee.exam_id=? ORDER BY u.name""", (eid,))
    available_examiners = q(
        """SELECT u.id, u.name, ep.department FROM users u JOIN examiner_profiles ep ON ep.user_id=u.id
           WHERE ep.status='Approved' AND u.id NOT IN (SELECT examiner_id FROM exam_examiners WHERE exam_id=?)
           ORDER BY u.name""", (eid,))
    slots = q(
        """SELECT s.*, u.name AS examiner_name FROM slots s JOIN users u ON u.id=s.examiner_id
           WHERE s.exam_id=? ORDER BY s.slot_date, s.start_time""", (eid,))
    pending_eval = q1("SELECT COUNT(*) FROM bookings WHERE exam_id=? AND status='Booked'", (eid,))[0]
    evaluated = q1("SELECT COUNT(*) FROM bookings WHERE exam_id=? AND status='Completed'", (eid,))[0]
    return render_template(
        "admin/exam_detail.html", exam=exam, rubrics=rubrics, assigned=assigned,
        available_examiners=available_examiners, slots=slots,
        rubric_total=sum(r["max_marks"] for r in rubrics), weight_total=sum(r["weightage"] for r in rubrics),
        pending_eval=pending_eval, evaluated=evaluated, actions=EXAM_ACTIONS)


@bp.route("/exams/<int:eid>/status", methods=["POST"])
def exam_status(eid):
    exam = get_or_404("SELECT * FROM exams WHERE id=?", (eid,))
    action = request.form.get("action", "")
    if action not in EXAM_ACTIONS:
        abort(400)
    allowed_from, new_status, label = EXAM_ACTIONS[action]
    if exam["status"] not in allowed_from:
        flash(f"Cannot do that while the examination is '{exam['status']}'.", "warning")
    elif action == "open_slots" and not q1("SELECT 1 FROM exam_examiners WHERE exam_id=?", (eid,)):
        flash("Assign at least one examiner before opening slot creation.", "warning")
    elif action == "open_booking" and not q1(
            "SELECT 1 FROM slots WHERE exam_id=? AND status IN ('Available','Full')", (eid,)):
        flash("There are no slots yet, so booking cannot be opened.", "warning")
    else:
        with tx() as db:
            db.execute("UPDATE exams SET status=? WHERE id=?", (new_status, eid))
        flash(f"{label}.", "success")
    return redirect(url_for("admin.exam_detail", eid=eid))


@bp.route("/exams/<int:eid>/timeline", methods=["POST"])
def exam_timeline(eid):
    get_or_404("SELECT id FROM exams WHERE id=?", (eid,))
    f = request.form
    try:
        v = dict(
            a=valid_date(f.get("slot_create_start"), "Slot creation start"),
            b=valid_date(f.get("slot_create_end"), "Slot creation end"),
            c=valid_date(f.get("book_start"), "Booking start"),
            d=valid_date(f.get("book_end"), "Booking end"), id=eid)
        check_exam_dates(v["a"], v["b"], v["c"], v["d"])
        with tx() as db:
            db.execute("UPDATE exams SET slot_create_start=:a, slot_create_end=:b, book_start=:c, book_end=:d WHERE id=:id", v)
        flash("Timeline updated.", "success")
    except RuleError as e:
        flash(str(e), "danger")
    return redirect(url_for("admin.exam_detail", eid=eid))


@bp.route("/exams/<int:eid>/publish", methods=["POST"])
def exam_publish(eid):
    exam = get_or_404("SELECT * FROM exams WHERE id=?", (eid,))
    new = 0 if exam["results_published"] else 1
    with tx() as db:
        db.execute("UPDATE exams SET results_published=? WHERE id=?", (new, eid))
    flash("Results published to students." if new else "Results unpublished.", "success")
    return redirect(url_for("admin.exam_detail", eid=eid))


# --------------------------------------------------------------------------- examiner assignment
@bp.route("/exams/<int:eid>/assign", methods=["POST"])
def exam_assign(eid):
    get_or_404("SELECT id FROM exams WHERE id=?", (eid,))
    try:
        xid = to_int(request.form.get("examiner_id", ""), "Examiner", 1)
        if not q1("SELECT 1 FROM examiner_profiles WHERE user_id=? AND status='Approved'", (xid,)):
            raise RuleError("Only approved examiners can be assigned.")
        with tx() as db:
            db.execute("INSERT OR IGNORE INTO exam_examiners(exam_id, examiner_id) VALUES(?,?)", (eid, xid))
        flash("Examiner assigned.", "success")
    except RuleError as e:
        flash(str(e), "danger")
    return redirect(url_for("admin.exam_detail", eid=eid))


@bp.route("/exams/<int:eid>/unassign/<int:xid>", methods=["POST"])
def exam_unassign(eid, xid):
    if q1("SELECT 1 FROM slots WHERE exam_id=? AND examiner_id=? AND status!='Cancelled'", (eid, xid)):
        flash("This examiner has slots for the examination. Change the examiner on those slots first.", "warning")
    else:
        with tx() as db:
            db.execute("DELETE FROM exam_examiners WHERE exam_id=? AND examiner_id=?", (eid, xid))
        flash("Examiner unassigned.", "success")
    return redirect(url_for("admin.exam_detail", eid=eid))


# --------------------------------------------------------------------------- rubrics
def _rubric_values(f):
    return (text(f, "criterion", label="Criterion name"),
            to_float(f.get("max_marks", ""), "Maximum marks", 0.01),
            to_float(f.get("weightage", "0") or "0", "Weightage", 0),
            text(f, "description", False))


@bp.route("/exams/<int:eid>/rubrics/add", methods=["POST"])
def rubric_add(eid):
    get_or_404("SELECT id FROM exams WHERE id=?", (eid,))
    try:
        crit, mx, wt, desc = _rubric_values(request.form)
        with tx() as db:
            db.execute("INSERT INTO rubrics(exam_id,criterion,max_marks,weightage,description) VALUES(?,?,?,?,?)",
                       (eid, crit, mx, wt, desc))
        flash("Rubric criterion added.", "success")
    except RuleError as e:
        flash(str(e), "danger")
    return redirect(url_for("admin.exam_detail", eid=eid))


@bp.route("/rubrics/<int:rid>/edit", methods=["GET", "POST"])
def rubric_edit(rid):
    rubric = get_or_404("SELECT * FROM rubrics WHERE id=?", (rid,))
    if request.method == "POST":
        try:
            crit, mx, wt, desc = _rubric_values(request.form)
            with tx() as db:
                db.execute("UPDATE rubrics SET criterion=?,max_marks=?,weightage=?,description=? WHERE id=?",
                           (crit, mx, wt, desc, rid))
            flash("Rubric criterion updated.", "success")
            return redirect(url_for("admin.exam_detail", eid=rubric["exam_id"]))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("admin/rubric_form.html", rubric=rubric)


@bp.route("/rubrics/<int:rid>/delete", methods=["POST"])
def rubric_delete(rid):
    rubric = get_or_404("SELECT * FROM rubrics WHERE id=?", (rid,))
    if q1("SELECT 1 FROM evaluation_scores WHERE rubric_id=?", (rid,)):
        flash("Students have already been scored on this criterion, so it cannot be removed.", "warning")
    else:
        with tx() as db:
            db.execute("DELETE FROM rubrics WHERE id=?", (rid,))
        flash("Rubric criterion removed.", "success")
    return redirect(url_for("admin.exam_detail", eid=rubric["exam_id"]))


# --------------------------------------------------------------------------- examiners
@bp.route("/examiners")
def examiners():
    term = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    sql = """SELECT u.id, u.name, u.email, u.contact, ep.department, ep.designation, ep.status,
                    (SELECT COUNT(*) FROM exam_examiners ee WHERE ee.examiner_id=u.id) AS exam_count
             FROM users u JOIN examiner_profiles ep ON ep.user_id=u.id
             WHERE (u.name LIKE ? OR u.email LIKE ? OR CAST(u.id AS TEXT)=?)"""
    args = [f"%{term}%", f"%{term}%", term]
    if status:
        sql += " AND ep.status=?"
        args.append(status)
    return render_template("admin/examiners.html", examiners=q(sql + " ORDER BY u.id DESC", args),
                           term=term, status=status)


@bp.route("/examiners/new", methods=["GET", "POST"])
def examiner_new():
    if request.method == "POST":
        f = request.form
        try:
            name, dept = text(f, "name"), text(f, "department")
            email = text(f, "email").lower()
            if "@" not in email:
                raise RuleError("Enter a valid email address.")
            pw = f.get("password", "")
            if len(pw) < 6:
                raise RuleError("Password must be at least 6 characters.")
            if q1("SELECT 1 FROM users WHERE email=?", (email,)):
                raise RuleError("That email is already registered.")
            with tx() as db:
                uid = db.execute("INSERT INTO users(role,name,email,password_hash,contact) VALUES('examiner',?,?,?,?)",
                                 (name, email, generate_password_hash(pw), text(f, "contact", False))).lastrowid
                db.execute("INSERT INTO examiner_profiles(user_id,department,designation,status) VALUES(?,?,?,'Approved')",
                           (uid, dept, text(f, "designation", False)))
            flash("Examiner added and approved.", "success")
            return redirect(url_for("admin.examiners"))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("admin/examiner_form.html")


@bp.route("/examiners/<int:uid>/<action>", methods=["POST"])
def examiner_action(uid, action):
    get_or_404("SELECT 1 FROM examiner_profiles WHERE user_id=?", (uid,))
    new = {"approve": "Approved", "deactivate": "Deactivated", "reactivate": "Approved"}.get(action)
    if new is None:
        abort(404)
    with tx() as db:
        db.execute("UPDATE examiner_profiles SET status=? WHERE user_id=?", (new, uid))
    flash(f"Examiner status is now {new}.", "success")
    return redirect(request.form.get("back") or url_for("admin.examiners"))


# --------------------------------------------------------------------------- students
@bp.route("/students")
def students():
    term = request.args.get("q", "").strip()
    rows = q(
        """SELECT u.id, u.name, u.email, u.contact, sp.roll_no, sp.program,
                  (SELECT COUNT(*) FROM bookings b WHERE b.student_id=u.id AND b.status IN ('Booked','Completed')) AS live_bookings
           FROM users u JOIN student_profiles sp ON sp.user_id=u.id
           WHERE u.name LIKE ? OR u.email LIKE ? OR sp.roll_no LIKE ? OR CAST(u.id AS TEXT)=?
           ORDER BY u.id DESC""", (f"%{term}%", f"%{term}%", f"%{term}%", term))
    return render_template("admin/students.html", students=rows, term=term)


@bp.route("/students/<int:uid>")
def student_detail(uid):
    stu = get_or_404(
        "SELECT u.*, sp.roll_no, sp.program FROM users u JOIN student_profiles sp ON sp.user_id=u.id WHERE u.id=?", (uid,))
    bookings = q(
        """SELECT b.*, s.slot_date, s.start_time, s.end_time, e.name AS exam_name, c.code AS course_code,
                  ev.total_marks, e.max_marks, e.results_published
           FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id
           JOIN courses c ON c.id=e.course_id LEFT JOIN evaluations ev ON ev.booking_id=b.id
           WHERE b.student_id=? ORDER BY b.id DESC""", (uid,))
    history = q("SELECT * FROM booking_history WHERE student_id=? ORDER BY id DESC", (uid,))
    return render_template("admin/student_detail.html", stu=stu, bookings=bookings, history=history)


# --------------------------------------------------------------------------- slots
@bp.route("/slots")
def slots():
    exam = request.args.get("exam", "")
    examiner_id = request.args.get("examiner", "")
    status = request.args.get("status", "")
    sql = """SELECT s.*, e.name AS exam_name, c.code AS course_code, u.name AS examiner_name
             FROM slots s JOIN exams e ON e.id=s.exam_id JOIN courses c ON c.id=e.course_id
             JOIN users u ON u.id=s.examiner_id WHERE 1=1"""
    args = []
    for col, val in (("s.exam_id", exam), ("s.examiner_id", examiner_id), ("s.status", status)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    return render_template(
        "admin/slots.html", slots=q(sql + " ORDER BY s.slot_date DESC, s.start_time", args),
        exam=exam, examiner=examiner_id, status=status,
        exams=q("SELECT id, name FROM exams ORDER BY id DESC"),
        examiners=q("SELECT u.id, u.name FROM users u JOIN examiner_profiles ep ON ep.user_id=u.id ORDER BY u.name"))


@bp.route("/slots/<int:sid>/examiner", methods=["GET", "POST"])
def slot_change_examiner(sid):
    slot = get_or_404(
        "SELECT s.*, e.name AS exam_name, u.name AS examiner_name FROM slots s JOIN exams e ON e.id=s.exam_id "
        "JOIN users u ON u.id=s.examiner_id WHERE s.id=?", (sid,))
    if request.method == "POST":
        try:
            new_id = to_int(request.form.get("examiner_id", ""), "Examiner", 1)
            if slot["status"] == "Cancelled":
                raise RuleError("Cancelled slots cannot be reassigned.")
            if new_id == slot["examiner_id"]:
                raise RuleError("Pick a different examiner.")
            if not q1("SELECT 1 FROM examiner_profiles WHERE user_id=? AND status='Approved'", (new_id,)):
                raise RuleError("Only approved examiners can be assigned.")
            with tx() as db:
                if examiner_clash(db, new_id, slot["slot_date"], slot["start_time"], slot["end_time"], sid):
                    raise RuleError("That examiner already has a slot at an overlapping time.")
                db.execute("UPDATE slots SET examiner_id=? WHERE id=?", (new_id, sid))
                db.execute("INSERT OR IGNORE INTO exam_examiners(exam_id, examiner_id) VALUES(?,?)", (slot["exam_id"], new_id))
                for b in db.execute("SELECT id, student_id FROM bookings WHERE slot_id=? AND status IN ('Booked','Completed')", (sid,)).fetchall():
                    log_history(db, b["id"], b["student_id"], "Examiner changed", f"Slot #{sid} reassigned by admin")
            flash("Examiner changed for this slot.", "success")
            return redirect(url_for("admin.slots"))
        except RuleError as e:
            flash(str(e), "danger")
    examiners_ = q(
        """SELECT u.id, u.name, ep.department FROM users u JOIN examiner_profiles ep ON ep.user_id=u.id
           WHERE ep.status='Approved' AND u.id!=? ORDER BY u.name""", (slot["examiner_id"],))
    return render_template("admin/slot_examiner.html", slot=slot, examiners=examiners_)


# --------------------------------------------------------------------------- bookings
@bp.route("/bookings")
def bookings():
    term = request.args.get("q", "").strip()
    exam = request.args.get("exam", "")
    status = request.args.get("status", "")
    sql = """SELECT b.*, u.name AS student_name, sp.roll_no, e.name AS exam_name, c.code AS course_code,
                    s.slot_date, s.start_time, s.end_time, x.name AS examiner_name
             FROM bookings b JOIN users u ON u.id=b.student_id JOIN student_profiles sp ON sp.user_id=u.id
             JOIN slots s ON s.id=b.slot_id JOIN exams e ON e.id=b.exam_id JOIN courses c ON c.id=e.course_id
             JOIN users x ON x.id=s.examiner_id
             WHERE (u.name LIKE ? OR sp.roll_no LIKE ? OR e.name LIKE ? OR CAST(b.id AS TEXT)=?)"""
    args = [f"%{term}%", f"%{term}%", f"%{term}%", term]
    if exam:
        sql += " AND b.exam_id=?"
        args.append(exam)
    if status:
        sql += " AND b.status=?"
        args.append(status)
    return render_template("admin/bookings.html", bookings=q(sql + " ORDER BY b.id DESC", args),
                           term=term, exam=exam, status=status,
                           exams=q("SELECT id, name FROM exams ORDER BY id DESC"))


@bp.route("/bookings/<int:bid>/reschedule", methods=["GET", "POST"])
def booking_reschedule(bid):
    booking = get_or_404(
        """SELECT b.*, u.name AS student_name, e.name AS exam_name, s.slot_date, s.start_time, s.end_time
           FROM bookings b JOIN users u ON u.id=b.student_id JOIN exams e ON e.id=b.exam_id
           JOIN slots s ON s.id=b.slot_id WHERE b.id=?""", (bid,))
    if booking["status"] != "Booked":
        flash("Only bookings with status 'Booked' can be rescheduled.", "warning")
        return redirect(url_for("admin.bookings"))
    if request.method == "POST":
        try:
            new_slot = to_int(request.form.get("slot_id", ""), "Slot", 1)
            with tx() as db:
                slot = db.execute("SELECT * FROM slots WHERE id=?", (new_slot,)).fetchone()
                if slot is None or slot["exam_id"] != booking["exam_id"]:
                    raise RuleError("Choose a slot of the same examination.")
                if slot["id"] == booking["slot_id"]:
                    raise RuleError("The booking is already in that slot.")
                if slot["status"] != "Available" or slot["available_seats"] < 1:
                    raise RuleError("That slot has no free seats.")
                if student_clash(db, booking["student_id"], slot["slot_date"], slot["start_time"], slot["end_time"], bid):
                    raise RuleError("The student has another booking at that time.")
                db.execute("UPDATE bookings SET slot_id=? WHERE id=?", (new_slot, bid))
                refresh_slot(db, booking["slot_id"])
                refresh_slot(db, new_slot)
                log_history(db, bid, booking["student_id"], "Rescheduled",
                            f"Admin moved booking from slot #{booking['slot_id']} to slot #{new_slot}")
            flash("Booking rescheduled.", "success")
            return redirect(url_for("admin.bookings"))
        except RuleError as e:
            flash(str(e), "danger")
    options = q(
        """SELECT s.*, u.name AS examiner_name FROM slots s JOIN users u ON u.id=s.examiner_id
           WHERE s.exam_id=? AND s.id!=? AND s.status='Available' AND s.available_seats>0
           ORDER BY s.slot_date, s.start_time""", (booking["exam_id"], booking["slot_id"]))
    return render_template("admin/reschedule.html", booking=booking, options=options)


# --------------------------------------------------------------------------- search
@bp.route("/search")
def search():
    term = request.args.get("q", "").strip()
    kind = request.args.get("type", "all")
    res = {}
    if term:
        like = f"%{term}%"
        if kind in ("all", "students"):
            res["students"] = q(
                """SELECT u.id, u.name, u.email, sp.roll_no FROM users u JOIN student_profiles sp ON sp.user_id=u.id
                   WHERE u.name LIKE ? OR sp.roll_no LIKE ? OR u.email LIKE ? OR CAST(u.id AS TEXT)=?""",
                (like, like, like, term))
        if kind in ("all", "examiners"):
            res["examiners"] = q(
                """SELECT u.id, u.name, u.email, ep.department, ep.status FROM users u
                   JOIN examiner_profiles ep ON ep.user_id=u.id
                   WHERE u.name LIKE ? OR u.email LIKE ? OR CAST(u.id AS TEXT)=?""", (like, like, term))
        if kind in ("all", "exams"):
            res["exams"] = q(
                """SELECT e.id, e.name, e.exam_type, e.status, c.code AS course_code FROM exams e
                   JOIN courses c ON c.id=e.course_id
                   WHERE e.name LIKE ? OR c.code LIKE ? OR CAST(e.id AS TEXT)=?""", (like, like, term))
        if kind in ("all", "bookings"):
            res["bookings"] = q(
                """SELECT b.id, b.status, u.name AS student_name, sp.roll_no, e.name AS exam_name FROM bookings b
                   JOIN users u ON u.id=b.student_id JOIN student_profiles sp ON sp.user_id=u.id
                   JOIN exams e ON e.id=b.exam_id
                   WHERE u.name LIKE ? OR sp.roll_no LIKE ? OR e.name LIKE ? OR CAST(b.id AS TEXT)=?""",
                (like, like, like, term))
    return render_template("admin/search.html", term=term, kind=kind, res=res)
