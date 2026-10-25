"""Examination Management Portal (EMP) -- Flask application entry point.

Run:  python app.py      (http://127.0.0.1:5000)
"""
import os
import secrets

from flask import Flask, abort, g, render_template, request, session

import admin
import api
import auth
import examiner
import student
from db import close_db, init_db, q1
from utils import csrf_token, today


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("EMP_SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.path.join(app.root_path, "instance", "emp.db"),
        ADMIN_NAME="Administrator",
        ADMIN_EMAIL=os.environ.get("EMP_ADMIN_EMAIL", "admin@emp.local"),
        ADMIN_PASSWORD=os.environ.get("EMP_ADMIN_PASSWORD", "admin123"),
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    init_db(app)  # tables + pre-existing admin are created in code
    app.teardown_appcontext(close_db)

    for module in (auth, admin, examiner, student, api):
        app.register_blueprint(module.bp)

    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = q1(
                """SELECT u.*, ep.status AS ex_status, ep.department, ep.designation,
                          sp.roll_no, sp.program
                   FROM users u
                   LEFT JOIN examiner_profiles ep ON ep.user_id = u.id
                   LEFT JOIN student_profiles sp ON sp.user_id = u.id
                   WHERE u.id = ?""",
                (uid,),
            )
            if g.user is None:
                session.clear()
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
                abort(400, "Invalid or missing CSRF token. Reload the page and try again.")

    @app.context_processor
    def inject():
        return {"user": g.get("user"), "csrf_token": csrf_token, "today": today()}

    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(400)
    def error_page(err):
        return render_template("error.html", err=err), err.code

    return app


if __name__ == "__main__":
    create_app().run(debug=True)
