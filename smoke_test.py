"""End-to-end smoke test of the core rules. Run:  python smoke_test.py

Uses a throw-away database, so your real instance/emp.db is never touched.
"""
import os
import tempfile
from datetime import date, timedelta

from app import create_app

tmp = tempfile.mkdtemp()
app = create_app({"DATABASE": os.path.join(tmp, "test.db"), "TESTING": True})

TODAY = date.today()
D = lambda n: (TODAY + timedelta(days=n)).isoformat()
passed = 0


def client():
    return app.test_client()


def post(c, url, data=None, follow=True):
    with c.session_transaction() as s:
        s["_csrf"] = "tok"
    return c.post(url, data={**(data or {}), "csrf_token": "tok"}, follow_redirects=follow)


def login(email, pw):
    c = client()
    r = post(c, "/login", {"email": email, "password": pw})
    return c, r


def check(cond, label):
    global passed
    assert cond, f"FAILED: {label}"
    passed += 1
    print(f"  ok  {label}")


def has(resp, text):
    return text.encode() in resp.data


# ---------------------------------------------------------------- setup via UI
admin, r = login("admin@emp.local", "admin123")
check(has(r, "Admin dashboard"), "admin can log in (pre-existing, DB created in code)")
check(login("admin@emp.local", "wrong")[1].status_code == 200 and has(login("admin@emp.local", "wrong")[1], "Invalid email"), "bad password rejected")

r = post(admin, "/admin/courses/new", {"code": "cs101", "name": "Intro to CS", "credits": "4", "status": "Active", "description": "x"})
check(has(r, "CS101"), "course created (code upper-cased)")
r = post(admin, "/admin/courses/new", {"code": "CS101", "name": "Dup", "credits": "4", "status": "Active"})
check(has(r, "already exists"), "duplicate course code blocked")

r = post(admin, "/admin/exams/new", {
    "course_id": "1", "name": "Viva 1", "exam_type": "Viva", "duration_minutes": "15", "max_marks": "50",
    "slot_create_start": D(-1), "slot_create_end": D(3), "book_start": D(1), "book_end": D(5), "venue": "Lab 1"})
check(has(r, "Viva 1"), "exam created")
for name, mx, wt in (("Concepts", "20", "40"), ("Problem solving", "20", "40"), ("Communication", "10", "20")):
    post(admin, "/admin/exams/1/rubrics/add", {"criterion": name, "max_marks": mx, "weightage": wt})
print("  ok  rubrics added")

# registrations
anon = client()
r = post(anon, "/register/examiner", {"name": "Dr Rao", "email": "rao@x.in", "department": "CS", "password": "secret1", "confirm": "secret1"})
check(has(r, "approves your account"), "examiner can self-register")
post(anon, "/register/examiner", {"name": "Dr Sen", "email": "sen@x.in", "department": "CS", "password": "secret1", "confirm": "secret1"})
for i, n in enumerate("ABC", 1):
    r = post(anon, "/register/student", {"name": f"Stu {n}", "email": f"s{n}@x.in", "roll_no": f"R{i}", "password": "secret1", "confirm": "secret1"})
check(has(r, "Registration successful"), "students can self-register")
r = post(anon, "/register/student", {"name": "Dup", "email": "sA@x.in", "roll_no": "R9", "password": "secret1", "confirm": "secret1"})
check(has(r, "already registered"), "duplicate email blocked")

rao, r = login("rao@x.in", "secret1")
check(has(r, "Awaiting Admin approval"), "unapproved examiner is held at the approval page")
check(rao.get("/examiner/").status_code == 302, "unapproved examiner cannot open dashboard")

post(admin, "/admin/examiners/2/approve")
post(admin, "/admin/examiners/3/approve")
r = rao.get("/examiner/", follow_redirects=True)
check(has(r, "Welcome, Dr Rao"), "approved examiner reaches dashboard")

# ---------------------------------------------------------------- slot creation rules
r = post(admin, "/admin/exams/1/status", {"action": "open_slots"})
check(has(r, "Assign at least one examiner"), "cannot open slot creation without an assigned examiner")
post(admin, "/admin/exams/1/assign", {"examiner_id": "2"})
post(admin, "/admin/exams/1/assign", {"examiner_id": "3"})

slot = {"slot_date": D(2), "start_time": "10:00", "end_time": "11:00", "capacity": "1", "venue": "Lab"}
r = post(rao, "/examiner/exams/1", slot)
check(has(r, "not open for this examination"), "examiner cannot create slots before Admin opens slot creation")

post(admin, "/admin/exams/1/status", {"action": "open_slots"})
r = post(rao, "/examiner/exams/1", slot)
check(has(r, "Slot created"), "examiner creates slot while slot creation is open")
r = post(rao, "/examiner/exams/1", {**slot, "start_time": "10:30", "end_time": "11:30"})
check(has(r, "overlapping"), "overlapping slots for same examiner blocked")
r = post(rao, "/examiner/exams/1", {**slot, "slot_date": D(-2)})
check(has(r, "in the past"), "past-dated slot blocked")
post(rao, "/examiner/exams/1", {**slot, "start_time": "12:00", "end_time": "13:00", "capacity": "2"})
r = post(rao, "/examiner/slots/1/edit", {**slot, "capacity": "3"})
check(has(r, "Slot updated"), "examiner updates slot before booking starts")
post(rao, "/examiner/slots/1/edit", {**slot, "capacity": "1"})

sen, _ = login("sen@x.in", "secret1")
r = post(sen, "/examiner/slots/1/edit", slot)
check(r.status_code == 404, "another examiner cannot edit my slot")

# ---------------------------------------------------------------- booking rules
sa, _ = login("sA@x.in", "secret1")
sb, _ = login("sB@x.in", "secret1")
sc, _ = login("sC@x.in", "secret1")
r = post(sa, "/student/slots/1/book")
check(has(r, "Booking is not open"), "student cannot book before Admin opens booking")

post(admin, "/admin/exams/1/status", {"action": "close_slots"})
r = post(admin, "/admin/exams/1/timeline", {"slot_create_start": D(-1), "slot_create_end": D(3), "book_start": D(1), "book_end": D(-1)})
check(has(r, "cannot be before"), "timeline validation: booking end before start rejected")
post(admin, "/admin/exams/1/timeline", {"slot_create_start": D(-1), "slot_create_end": D(3), "book_start": D(0), "book_end": D(5)})
r = post(admin, "/admin/exams/1/status", {"action": "open_booking"})
check(has(r, "Student booking opened"), "Admin opens booking")
r = post(rao, "/examiner/exams/1", {**slot, "slot_date": D(3)})
check(has(r, "not open for this examination"), "examiner cannot create slots once creation is closed")
r = post(rao, "/examiner/slots/1/edit", slot)
check(has(r, "before booking starts"), "slot update blocked once booking has started")

r = post(sa, "/student/slots/1/book")
check(has(r, "Slot booked successfully"), "student A books the 1-seat slot")
check(has(sa.get("/student/exams/1"), "Full"), "slot closes automatically when capacity is reached")
r = post(sb, "/student/slots/1/book")
check(has(r, "full or no longer available"), "overbooking prevented")
r = post(sa, "/student/slots/2/book")
check(has(r, "already have a booking"), "duplicate booking for same exam prevented")

r = post(sa, "/student/bookings/1/cancel")
check(has(r, "Booking cancelled"), "student cancels before the deadline")
check(has(sb.get("/student/exams/1"), "Available"), "cancelled seat re-opens the slot")
r = post(sa, "/student/slots/1/book")
check(has(r, "Slot booked successfully"), "student can re-book after cancelling (booking #2)")
post(sb, "/student/slots/2/book")
post(sc, "/student/slots/2/book")
check(has(sa.get("/student/bookings"), "Cancelled"), "history keeps cancelled booking")
check(has(sa.get("/student/bookings"), "Booked") and has(sa.get("/student/bookings"), "Cancelled"), "booking history log maintained")

# ---------------------------------------------------------------- admin tools
r = admin.get("/admin/")
check(has(r, "Admin dashboard") and has(r, "Slot fill rate"), "admin dashboard with stats and charts")
r = admin.get("/admin/search?q=Stu&type=students")
check(has(r, "Stu A") and has(r, "Stu C"), "admin search by name")
r = admin.get("/admin/search?q=1&type=bookings")
check(has(r, "Stu"), "admin search bookings by ID")

r = post(admin, "/admin/bookings/3/reschedule", {"slot_id": "1"})  # slot 1 is full (student A)
check(has(r, "no free seats"), "reschedule into a full slot rejected")
r = post(admin, "/admin/slots/1/examiner", {"examiner_id": "3"})
check(has(r, "Examiner changed"), "admin changes assigned examiner")
check(has(sen.get("/examiner/slots/1"), "Stu A"), "new examiner sees the booked student")
check(rao.get("/examiner/slots/1").status_code == 404, "old examiner loses access to that slot")
post(admin, "/admin/slots/1/examiner", {"examiner_id": "2"})  # restore


# ---------------------------------------------------------------- evaluation
marks = {"r_1": "18", "r_2": "15", "r_3": "9", "remarks": "Good work"}
r = post(sen, "/examiner/bookings/2/evaluate", marks)
check(r.status_code == 403, "examiner cannot evaluate students from someone else's slot")
r = post(rao, "/examiner/bookings/2/evaluate", {**marks, "r_1": "25"})
check(has(r, "cannot exceed"), "marks above criterion maximum rejected")
r = post(rao, "/examiner/bookings/2/evaluate", marks)
check(has(r, "Marks submitted"), "examiner submits rubric marks and remarks")
# weighted: (18/20*40 + 15/20*40 + 9/10*20)/100*50 = (36+30+18)/100*50 = 42
check(has(r, "42"), "weighted total computed (42 / 50)")

r = sa.get("/student/results")
check(not has(r, "Viva 1"), "student cannot see marks before results are published")
post(admin, "/admin/exams/1/publish")
r = sa.get("/student/results")
check(has(r, "Viva 1") and has(r, "42"), "student sees published result")
check(has(sa.get("/student/results/2"), "Good work"), "student sees examiner remarks")
check(has(sa.get("/student/results/2/scorecard"), "Examination Scorecard"), "scorecard renders")
check(sa.get("/student/results/2/scorecard?download=1").headers["Content-Disposition"].startswith("attachment"), "scorecard downloadable")
check(sb.get("/student/results/2").status_code == 404, "students cannot open each other's results")
r = post(rao, "/examiner/bookings/2/evaluate", marks)
check(has(r, "locked") or has(r, "already published"), "marks locked after publication")

# ---------------------------------------------------------------- access control, API, profile
check(sa.get("/admin/").status_code == 403, "student blocked from admin pages")
check(anon.get("/student/").status_code == 302, "anonymous redirected to login")
check(sa.get("/api/exams").json[0]["name"] == "Viva 1", "API: exams as JSON")
check(sa.get("/api/students").status_code == 403, "API: students list is admin-only")
check(len(sa.get("/api/bookings").json) == 2, "API: student sees only own bookings")
check(len(admin.get("/api/bookings").json) == 4, "API: admin sees all bookings")
r = post(sa, "/profile", {"name": "Stu A2", "contact": "999", "program": "BS"})
check(has(r, "Profile updated") and has(r, "Stu A2"), "student edits profile")
r = anon.post("/login", data={"email": "admin@emp.local", "password": "admin123"})
check(r.status_code == 400, "POST without CSRF token rejected")

print(f"\nAll {passed} checks passed.")
