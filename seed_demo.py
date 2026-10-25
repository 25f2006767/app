"""Optional: load demo data so you can click around immediately.

    python seed_demo.py

Creates 2 courses, 2 approved examiners, 1 pending examiner, 3 students and 2 examinations
(one in Slot Creation phase, one in Booking Open phase). Safe to run once on a fresh database.
All demo passwords are:  demo123
"""
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from app import create_app
from db import q1, tx

app = create_app()
D = lambda n: (date.today() + timedelta(days=n)).isoformat()
PW = generate_password_hash("demo123")

with app.app_context():
    if q1("SELECT 1 FROM courses LIMIT 1"):
        raise SystemExit("Database already has data; delete instance/emp.db first for a clean demo.")
    with tx() as db:
        db.execute("INSERT INTO courses(code,name,description,credits) VALUES('CS101','Programming Fundamentals','Intro course',4)")
        db.execute("INSERT INTO courses(code,name,description,credits) VALUES('DB201','Database Systems','SQL and design',4)")

        def user(role, name, email, contact=""):
            return db.execute("INSERT INTO users(role,name,email,password_hash,contact) VALUES(?,?,?,?,?)",
                              (role, name, email, PW, contact)).lastrowid

        for name, email, dept, status in (("Dr. Anita Rao", "anita@emp.local", "Computer Science", "Approved"),
                                          ("Dr. Vikram Sen", "vikram@emp.local", "Computer Science", "Approved"),
                                          ("Dr. Meera Nair", "meera@emp.local", "Mathematics", "Pending")):
            uid = user("examiner", name, email)
            db.execute("INSERT INTO examiner_profiles(user_id,department,designation,status) VALUES(?,?,?,?)",
                       (uid, dept, "Assistant Professor", status))
        for i, name in enumerate(("Aarav Kumar", "Diya Menon", "Rohan Iyer"), 1):
            uid = user("student", name, f"student{i}@emp.local")
            db.execute("INSERT INTO student_profiles(user_id,roll_no,program) VALUES(?,?,?)", (uid, f"21F100{i}", "BS Data Science"))

        # Exam 1: examiners can create slots right now
        db.execute("""INSERT INTO exams(course_id,name,exam_type,duration_minutes,max_marks,slot_create_start,slot_create_end,
                      book_start,book_end,status,venue,instructions) VALUES(1,'Python Viva','Viva',15,50,?,?,?,?,'Slot Creation','Lab 2',
                      'Bring your student ID and be ready to explain your code.')""", (D(-1), D(4), D(5), D(10)))
        # Exam 2: students can book right now
        db.execute("""INSERT INTO exams(course_id,name,exam_type,duration_minutes,max_marks,slot_create_start,slot_create_end,
                      book_start,book_end,status,venue) VALUES(2,'SQL Practical','Practical',45,100,?,?,?,?,'Booking Open','Lab 5')""",
                   (D(-5), D(-1), D(-1), D(7)))
        for eid in (1, 2):
            for xid in (2, 3):
                db.execute("INSERT INTO exam_examiners(exam_id,examiner_id) VALUES(?,?)", (eid, xid))
        for eid, crit in ((1, (("Concepts", 20, 40), ("Code explanation", 20, 40), ("Communication", 10, 20))),
                          (2, (("Query correctness", 50, 60), ("Schema design", 30, 30), ("Optimisation", 20, 10)))):
            for name, mx, wt in crit:
                db.execute("INSERT INTO rubrics(exam_id,criterion,max_marks,weightage) VALUES(?,?,?,?)", (eid, name, mx, wt))
        for xid, day, start, end, cap in ((2, 2, "10:00", "12:00", 3), (3, 2, "14:00", "16:00", 2), (2, 3, "10:00", "12:00", 1)):
            db.execute("""INSERT INTO slots(exam_id,examiner_id,slot_date,start_time,end_time,capacity,available_seats,venue)
                          VALUES(2,?,?,?,?,?,?,'Lab 5')""", (xid, D(day), start, end, cap, cap))

print("Demo data loaded.\n  Admin     admin@emp.local / admin123\n  Examiner  anita@emp.local / demo123  (also vikram@emp.local)"
      "\n  Pending   meera@emp.local / demo123  (approve her from the Admin dashboard)"
      "\n  Student   student1@emp.local / demo123  (also student2, student3)")
