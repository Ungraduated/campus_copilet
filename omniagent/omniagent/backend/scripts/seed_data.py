"""Sample data for the three demo domains. Dates are relative to *today* so questions like
"orders this month" or "tickets in the last 7 days" return real, current rows."""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _insert(conn: Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
    conn.execute(text(sql), rows)


FIRST = ["Aarav", "Vivaan", "Aditya", "Arjun", "Ishaan", "Kabir", "Rohan", "Karan", "Neha", "Priya", "Ananya", "Diya",
         "Meera", "Riya", "Sneha", "Tanvi", "Pooja", "Kavya", "Isha", "Aisha", "Sahil", "Nikhil", "Varun", "Manav"]
LAST = ["Sharma", "Verma", "Patel", "Gupta", "Singh", "Reddy", "Nair", "Iyer", "Khan", "Joshi", "Mehta", "Das",
        "Chauhan", "Mishra", "Yadav", "Tiwari", "Bose", "Kulkarni"]


def _grade(marks: float) -> tuple[str, int]:
    for cut, g, pts in [(90, "A+", 10), (80, "A", 9), (70, "B", 8), (60, "C", 7), (50, "D", 6), (40, "E", 5)]:
        if marks >= cut:
            return g, pts
    return "F", 0


def seed_students(conn: Connection, today: date) -> None:
    rnd = random.Random(11)
    branches = ["AI", "Computer Science", "Electronics", "Mechanical", "Civil"]
    courses = [
        ("GEN101", "Engineering Mathematics", "General", 4, "Dr. R. Menon"),
        ("GEN102", "Communication Skills", "General", 2, "Prof. S. Dutta"),
        ("AI201", "Machine Learning", "AI", 4, "Dr. A. Kapoor"),
        ("AI202", "Deep Learning", "AI", 4, "Dr. L. Fernandes"),
        ("CS201", "Data Structures", "Computer Science", 4, "Prof. V. Rao"),
        ("CS202", "Operating Systems", "Computer Science", 3, "Dr. P. Banerjee"),
        ("EC201", "Digital Electronics", "Electronics", 4, "Dr. N. Pillai"),
        ("EC202", "Signals and Systems", "Electronics", 3, "Prof. T. Hegde"),
        ("ME201", "Thermodynamics", "Mechanical", 4, "Dr. K. Sandhu"),
        ("ME202", "Fluid Mechanics", "Mechanical", 3, "Prof. D. Naik"),
        ("CE201", "Structural Analysis", "Civil", 4, "Dr. M. Bhatt"),
        ("CE202", "Surveying", "Civil", 3, "Prof. J. Kaur"),
    ]
    _insert(conn, "courses", [
        dict(code=c, title=t, branch=b, credits=cr, instructor=i, semester=3) for c, t, b, cr, i in courses])
    course_rows = conn.execute(text("SELECT id, branch FROM courses")).all()

    students, student_diligence = [], {}
    for n in range(1, 61):
        name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
        branch = rnd.choices(branches, weights=[24, 26, 18, 16, 16])[0]
        students.append(dict(
            enrollment_no=f"EN{2023000 + n}", full_name=name, email=f"student{n}@example.edu",
            phone=f"+91 90000 {10000 + n}", branch=branch, semester=rnd.choice([3, 3, 3, 5]),
            enrollment_year=rnd.choice([2023, 2024]), status=rnd.choices(["active", "on_leave"], weights=[95, 5])[0],
            cgpa=None))
        student_diligence[n] = rnd.uniform(0.55, 0.98)
    _insert(conn, "students", students)

    ids = [r[0] for r in conn.execute(text("SELECT id FROM students ORDER BY id"))]
    branch_of = dict(conn.execute(text("SELECT id, branch FROM students")).all())

    enrollments, attendance, records = [], [], []
    last_monday = today - timedelta(days=today.weekday())
    dates = [last_monday - timedelta(weeks=w) for w in range(10)]
    points: dict[int, list[int]] = {i: [] for i in ids}
    for sid in ids:
        pool = [cid for cid, b in course_rows if b in ("General", branch_of[sid])]
        dil = student_diligence[sid - ids[0] + 1]
        for cid in rnd.sample(pool, k=min(len(pool), rnd.choice([3, 4]))):
            enrollments.append(dict(student_id=sid, course_id=cid))
            for d in dates:
                r = rnd.random()
                status = "present" if r < dil else ("late" if r < dil + 0.06 else "absent")
                attendance.append(dict(student_id=sid, course_id=cid, attendance_date=d, status=status))
            marks = round(min(99, max(28, rnd.gauss(55 + dil * 28, 9))), 1)
            grade, pts = _grade(marks)
            points[sid].append(pts)
            records.append(dict(student_id=sid, course_id=cid, term="2025-26 Sem 1", marks=marks, max_marks=100, grade=grade))
    _insert(conn, "enrollments", enrollments)
    _insert(conn, "attendance", attendance)
    _insert(conn, "academic_records", records)
    for sid, pts in points.items():
        if pts:
            conn.execute(text("UPDATE students SET cgpa = :c WHERE id = :i"), {"c": round(sum(pts) / len(pts), 2), "i": sid})


def seed_ecommerce(conn: Connection, today: date) -> None:
    rnd = random.Random(23)
    now = datetime.combine(today, time(12, 0))
    cats = ["Electronics", "Home & Kitchen", "Books", "Fashion", "Sports", "Beauty"]
    _insert(conn, "categories", [dict(name=c, description=f"{c} products") for c in cats])
    cat_ids = {n: i for i, n in conn.execute(text("SELECT id, name FROM categories")).all()}
    catalog = {
        "Electronics": [("Wireless Earbuds", 2499), ("Bluetooth Speaker", 1799), ("Power Bank 20000mAh", 1599), ("Smartwatch", 3999), ("USB-C Hub", 1299)],
        "Home & Kitchen": [("Air Fryer", 5499), ("Mixer Grinder", 3299), ("Non-stick Pan Set", 1899), ("Electric Kettle", 999), ("Water Purifier Jug", 1499)],
        "Books": [("Python Crash Course", 699), ("Atomic Habits", 499), ("The Pragmatic Programmer", 899), ("Deep Work", 549), ("Clean Code", 799)],
        "Fashion": [("Cotton T-Shirt", 599), ("Denim Jacket", 2499), ("Running Shoes", 2999), ("Leather Wallet", 899), ("Backpack 30L", 1599)],
        "Sports": [("Yoga Mat", 799), ("Dumbbell Set 10kg", 1999), ("Badminton Racket", 1299), ("Cricket Bat", 2799), ("Skipping Rope", 249)],
        "Beauty": [("Vitamin C Serum", 699), ("Sunscreen SPF 50", 449), ("Hair Dryer", 1599), ("Face Wash", 249), ("Beard Trimmer", 1399)],
    }
    products, n = [], 0
    for cat, items in catalog.items():
        for name, price in items:
            n += 1
            products.append(dict(sku=f"SKU{1000 + n}", name=name, category_id=cat_ids[cat], price=price, is_active=1,
                                 created_at=now - timedelta(days=rnd.randint(60, 400))))
    _insert(conn, "products", products)
    pids = [r[0] for r in conn.execute(text("SELECT id FROM products ORDER BY id"))]
    price_of = dict(conn.execute(text("SELECT id, price FROM products")).all())
    low = set(rnd.sample(pids, 6))  # guarantee some low-stock rows
    _insert(conn, "inventory", [
        dict(product_id=p, warehouse=rnd.choice(["Bhopal", "Mumbai", "Bengaluru"]),
             quantity_in_stock=rnd.randint(0, 9) if p in low else rnd.randint(15, 220),
             reorder_level=10, updated_at=now - timedelta(days=rnd.randint(0, 5))) for p in pids])

    cities = ["Bhopal", "Indore", "Delhi", "Mumbai", "Pune", "Bengaluru", "Chennai", "Kolkata"]
    _insert(conn, "customers", [
        dict(name=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", email=f"cust{i}@example.com", city=rnd.choice(cities),
             joined_on=today - timedelta(days=rnd.randint(30, 700))) for i in range(1, 51)])
    cids = [r[0] for r in conn.execute(text("SELECT id FROM customers"))]

    orders, items = [], []
    oid = 0
    for back in range(0, 120):
        day = today - timedelta(days=back)
        for _ in range(rnd.randint(1, 4)):
            oid += 1
            when = datetime.combine(day, time(rnd.randint(8, 22), rnd.randint(0, 59)))
            status = rnd.choices(["pending", "paid", "shipped", "delivered", "cancelled"],
                                 weights=[8, 12, 15, 58, 7] if back > 7 else [30, 30, 25, 10, 5])[0]
            lines = [(rnd.choice(pids), rnd.randint(1, 3)) for _ in range(rnd.randint(1, 3))]
            total = sum(price_of[p] * q for p, q in lines)
            orders.append(dict(id=oid, customer_id=rnd.choice(cids), order_date=when, status=status,
                               payment_method=rnd.choice(["UPI", "Card", "Net Banking", "Cash on Delivery"]),
                               total_amount=total))
            items += [dict(order_id=oid, product_id=p, quantity=q, unit_price=price_of[p]) for p, q in lines]
    _insert(conn, "orders", orders)
    _insert(conn, "order_items", items)


def seed_helpdesk(conn: Connection, today: date) -> None:
    rnd = random.Random(37)
    now = datetime.combine(today, time(18, 0))
    teams = ["Technical Support", "Billing", "Account Management", "Infrastructure"]
    _insert(conn, "teams", [dict(name=t) for t in teams])
    tids = [r[0] for r in conn.execute(text("SELECT id FROM teams ORDER BY id"))]
    _insert(conn, "agents", [dict(name=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", team_id=tids[i % 4]) for i in range(8)])
    agents = conn.execute(text("SELECT id, team_id FROM agents")).all()
    statuses = [("Open", 1), ("In Progress", 1), ("Waiting on Customer", 1), ("Resolved", 0), ("Closed", 0)]
    _insert(conn, "ticket_statuses", [dict(name=n, is_open=o) for n, o in statuses])
    _insert(conn, "priorities", [dict(name=n, rank_order=r) for n, r in [("Low", 1), ("Medium", 2), ("High", 3), ("Critical", 4)]])
    sids = dict((n, (i, o)) for i, n, o in conn.execute(text("SELECT id, name, is_open FROM ticket_statuses")).all())
    pids = [r[0] for r in conn.execute(text("SELECT id FROM priorities ORDER BY rank_order"))]
    subjects = ["Cannot log in to dashboard", "Invoice shows wrong amount", "Password reset email not arriving",
                "App crashes on startup", "Request to upgrade plan", "Data export is failing", "Slow page load times",
                "Two-factor codes not working", "Refund not received", "Need access for new teammate",
                "API returns 500 errors", "Server unreachable from office network"]
    tickets = []
    for _ in range(140):
        created = now - timedelta(days=rnd.randint(0, 59), hours=rnd.randint(0, 9))
        status = rnd.choices(list(sids), weights=[22, 18, 10, 25, 25])[0]
        sid, is_open = sids[status]
        agent = rnd.choice(agents)
        tickets.append(dict(
            subject=rnd.choice(subjects), description="Customer reported the issue via the support portal.",
            requester_name=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", requester_email=f"user{rnd.randint(1, 999)}@example.com",
            status_id=sid, priority_id=rnd.choices(pids, weights=[25, 35, 25, 15])[0], team_id=agent[1],
            assignee_id=agent[0] if rnd.random() > 0.1 else None, created_at=created,
            resolved_at=None if is_open else created + timedelta(hours=rnd.randint(2, 96))))
    _insert(conn, "tickets", tickets)


SEEDERS = {"students": seed_students, "ecommerce": seed_ecommerce, "helpdesk": seed_helpdesk}
