"""Authentication, registration, profile editing and the role guard used by blueprints."""
from flask import (Blueprint, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import q1, tx
from utils import RuleError, text

bp = Blueprint("auth", __name__)


def role_guard(role):
    """before_request hook factory: only `role` may enter. Examiners must also be Approved."""

    def check():
        u = g.user
        if u is None:
            return redirect(url_for("auth.login", next=request.path))
        if u["role"] != role:
            abort(403)
        if role == "examiner" and u["ex_status"] != "Approved":
            return redirect(url_for("auth.pending"))

    return check


def _check_email_password(form):
    email = text(form, "email").lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise RuleError("Enter a valid email address.")
    pw = form.get("password", "")
    if len(pw) < 6:
        raise RuleError("Password must be at least 6 characters.")
    if pw != form.get("confirm", ""):
        raise RuleError("Passwords do not match.")
    if q1("SELECT 1 FROM users WHERE email=?", (email,)):
        raise RuleError("That email is already registered.")
    return email, pw


@bp.route("/")
def index():
    if g.user is None:
        return redirect(url_for("auth.login"))
    return redirect(url_for(f"{g.user['role']}.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = q1("SELECT * FROM users WHERE email=?", (email,))
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = user["id"]
            nxt = request.form.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("auth.index"))
        flash("Invalid email or password.", "danger")
    return render_template("auth/login.html", next=request.args.get("next", ""))


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/register/student", methods=["GET", "POST"])
def register_student():
    if request.method == "POST":
        f = request.form
        try:
            name = text(f, "name")
            roll = text(f, "roll_no", label="Roll number")
            email, pw = _check_email_password(f)
            if q1("SELECT 1 FROM student_profiles WHERE roll_no=?", (roll,)):
                raise RuleError("That roll number is already registered.")
            with tx() as db:
                uid = db.execute(
                    "INSERT INTO users(role,name,email,password_hash,contact) VALUES('student',?,?,?,?)",
                    (name, email, generate_password_hash(pw), text(f, "contact", False)),
                ).lastrowid
                db.execute("INSERT INTO student_profiles(user_id,roll_no,program) VALUES(?,?,?)",
                           (uid, roll, text(f, "program", False)))
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("auth.login"))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("auth/register_student.html")


@bp.route("/register/examiner", methods=["GET", "POST"])
def register_examiner():
    if request.method == "POST":
        f = request.form
        try:
            name = text(f, "name")
            dept = text(f, "department")
            email, pw = _check_email_password(f)
            with tx() as db:
                uid = db.execute(
                    "INSERT INTO users(role,name,email,password_hash,contact) VALUES('examiner',?,?,?,?)",
                    (name, email, generate_password_hash(pw), text(f, "contact", False)),
                ).lastrowid
                db.execute("INSERT INTO examiner_profiles(user_id,department,designation,status) "
                           "VALUES(?,?,?,'Pending')", (uid, dept, text(f, "designation", False)))
            flash("Registration submitted. You can log in once the Admin approves your account.", "success")
            return redirect(url_for("auth.login"))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("auth/register_examiner.html")


@bp.route("/pending")
def pending():
    u = g.user
    if u is None:
        return redirect(url_for("auth.login"))
    if u["role"] != "examiner" or u["ex_status"] == "Approved":
        return redirect(url_for("auth.index"))
    return render_template("auth/pending.html")


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    u = g.user
    if u is None:
        return redirect(url_for("auth.login", next=request.path))
    if request.method == "POST":
        f = request.form
        try:
            name = text(f, "name")
            new_pw = f.get("new_password", "")
            with tx() as db:
                db.execute("UPDATE users SET name=?, contact=? WHERE id=?",
                           (name, text(f, "contact", False), u["id"]))
                if u["role"] == "student":
                    db.execute("UPDATE student_profiles SET program=? WHERE user_id=?",
                               (text(f, "program", False), u["id"]))
                elif u["role"] == "examiner":
                    db.execute("UPDATE examiner_profiles SET department=?, designation=? WHERE user_id=?",
                               (text(f, "department"), text(f, "designation", False), u["id"]))
                if new_pw:
                    if not check_password_hash(u["password_hash"], f.get("current_password", "")):
                        raise RuleError("Current password is incorrect.")
                    if len(new_pw) < 6:
                        raise RuleError("New password must be at least 6 characters.")
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (generate_password_hash(new_pw), u["id"]))
            flash("Profile updated.", "success")
            return redirect(url_for("auth.profile"))
        except RuleError as e:
            flash(str(e), "danger")
    return render_template("auth/profile.html")
