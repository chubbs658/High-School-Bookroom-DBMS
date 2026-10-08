from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import sqlite3
import shutil
import sys
import os
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from datetime import date, datetime
from werkzeug.security import generate_password_hash, check_password_hash

from common import DB, db, _ensure_column, login_required, admin_required, find_students, BASE_DIR, app_now, app_today
from library import library_bp, init_library_db
import license_check
import license_config

def _resource_dir(*parts):
    """Base folder for bundled, read-only resources (templates, static
    files) added at build time via PyInstaller's --add-data.

    Running as a plain script, this is just the folder app.py is in - the
    normal relative 'templates'/'static' lookup. When PyInstaller freezes
    the app, --onefile extracts bundled data into a temporary folder given
    by sys._MEIPASS (--onedir places it next to the .exe instead, and
    still sets sys._MEIPASS to that same folder), so resources must be
    looked up there rather than via a relative path that no longer means
    anything once bundled. This is unrelated to BASE_DIR in common.py,
    which points at a persistent, writable location for the database -
    these bundled resources are read-only and get re-extracted every run.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base.joinpath(*parts))


app = Flask(
    __name__,
    template_folder=_resource_dir("templates"),
    static_folder=_resource_dir("static"),
)
app.register_blueprint(library_bp)


_THEME_KEYS = [
    "bg", "surface", "text", "muted", "brand", "brand_dark",
    "accent", "tint", "tint_light", "primary", "primary_dark",
]


@app.context_processor
def _inject_brand():
    """Makes brand_name/brand_abbrev/theme available in every template
    without passing them explicitly on each render_template() call.
    Sourced from license_config.py, which build_for_school.py
    regenerates per school - so swapping schools only ever means
    rebuilding, never editing a template by hand.

    theme is a full palette (not just brand/accent) - comparing two real
    schools' stylesheets showed the background/text/muted tones differ
    per school too, not just the brand color. It's only populated when
    license_config.py actually has THEME_* values (a real per-school
    build); running app.py directly against the committed template/local
    dev config leaves it empty, and base.html falls back to style.css's
    own built-in defaults in that case."""
    theme = {}
    if hasattr(license_config, "THEME_BG"):
        theme = {
            key: getattr(license_config, f"THEME_{key.upper()}")
            for key in _THEME_KEYS
        }
    return {
        "brand_name": getattr(license_config, "SCHOOL_NAME", "Bookroom"),
        "brand_abbrev": getattr(license_config, "SCHOOL_ABBREV", "MHS"),
        "theme": theme,
    }


def _load_or_create_secret_key():
    """A hardcoded secret_key is fine for local development, but shouldn't
    ship as-is in a distributable .exe - anyone could read it straight out
    of the binary and forge session cookies. Instead, generate a random key
    once and store it next to the database (BASE_DIR: persistent, and not
    inside the app bundle itself), reusing the same key on every future
    launch so existing sessions don't get invalidated on every restart."""
    key_file = BASE_DIR / "secret_key.txt"
    if key_file.exists():
        existing = key_file.read_text().strip()
        if existing:
            return existing
    new_key = secrets.token_hex(32)
    key_file.write_text(new_key)
    return new_key


app.secret_key = _load_or_create_secret_key()

CONDITIONS = ["Excellent", "Good", "Fair", "Poor", "Damaged", "Lost"]

def condition_index(cond):
    """Position of `cond` in the ordered CONDITIONS scale (0 = best). Returns
    None for an unrecognized value so callers can fail safe instead of
    raising."""
    try:
        return CONDITIONS.index(cond)
    except ValueError:
        return None

def condition_degrees_worse(issue_condition, return_condition):
    """How many steps worse (positive) or better (negative) `return_condition`
    is relative to `issue_condition`, measured by position in CONDITIONS.
    Returns 0 if either value isn't recognized, so an unknown condition never
    accidentally triggers the mismatch/replacement flow."""
    i = condition_index(issue_condition)
    r = condition_index(return_condition)
    if i is None or r is None:
        return 0
    return r - i

# Color used for each condition in the pie-chart / legend on the Books
# Issued report. Kept next to CONDITIONS so the two stay easy to keep in
# sync; any condition value not in this map falls back to a neutral gray.
CONDITION_COLORS = {
    "Excellent": "#2e7d32",
    "Good": "#66bb6a",
    "Fair": "#fbc02d",
    "Poor": "#fb8c00",
    "Damaged": "#e53935",
    "Lost": "#6d4c41",
}

# Gender codes captured at student registration. Required going forward
# (see _validate_gender); existing students registered before this field
# existed keep an empty string until their record is next edited.
GENDERS = ["M", "F"]
GENDER_LABELS = {"M": "Male", "F": "Female"}
GENDER_COLORS = {"M": "#1e88e5", "F": "#d81b60"}

# Canonical list of book statuses. Shared by the edit-book form and the
# assign-book browse filter so both stay in sync with a single source of
# truth instead of two separately maintained lists.
BOOK_STATUSES = ["Available", "Issued", "Awaiting Decision", "Awaiting Replacement", "Lost"]

# The two login roles. "admin" can do everything "staff" can, plus the
# functions gated behind @admin_required (due dates/fee amounts, marking or
# deleting a fee, resolving a replacement decision, the contribution-fee
# override, and managing logins). Staff cannot reach those routes at all -
# the check lives in common.admin_required, not just in the templates.
ROLES = ["staff", "admin"]

# Grade-level groupings for cross-grade book assignment.
UPPER_FORM_GRADES = {10, 11}   # 4th/5th form - always share books, no checkbox needed
LOWER_FORM_GRADES = {7, 8, 9}  # 1st-3rd form - share only if book is flagged cross_grade

FORM_LABELS = ["1st Form", "2nd Form", "3rd Form", "4th Form", "5th Form", "6th Form"]

def form_label(grade_level):
    """Grade 7 -> '1st Form' ... Grade 12 -> '6th Form'. Falls back to the raw
    grade level for anything outside that range so unexpected data never crashes."""
    idx = grade_level - 7
    if 0 <= idx < len(FORM_LABELS):
        return FORM_LABELS[idx]
    return f"Grade {grade_level}"

def normalize_grade_level(grade_level):
    """Accepts either the raw Form number (1-6, as a person would naturally type
    '2' for 2nd Form) or this system's internal grade_level encoding (7-12) and
    always returns the 7-12 encoding, so 'Grade 2' and '2nd Form' can never end
    up stored as two different values again."""
    if 1 <= grade_level <= 6:
        return grade_level + 6
    return grade_level

def normalize_grade_filter(raw):
    """Same Form-number-to-grade_level conversion as normalize_grade_level(),
    but safe for a raw filter query-string value: blank input stays blank
    (meaning 'no filter'), and non-numeric input is passed through unchanged
    (it simply won't match any row) instead of raising. Without this, typing
    a Form number like '3' into a grade filter would never match stored rows
    (which use the 7-12 grade_level encoding), while typing '9' would - so
    filters only worked for the raw 7-12 range and never for 1-6 form input."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return str(normalize_grade_level(int(raw)))
    except ValueError:
        return raw

def _validate_gender(form):
    """Gender is required at registration and on every edit going forward.
    Existing students saved before this field existed simply carry an empty
    string until their record is next touched - we don't retroactively force
    a value onto rows nobody is editing right now."""
    gender = (form.get("gender") or "").strip().upper()
    if gender not in GENDERS:
        return None, "Gender is required (select M or F)."
    return gender, None

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS attendants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_number TEXT UNIQUE NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        grade_level INTEGER NOT NULL,
        class_name TEXT NOT NULL,
        gender TEXT NOT NULL DEFAULT '',
        contribution_paid INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        brn TEXT NOT NULL,
        title TEXT NOT NULL,
        subject TEXT NOT NULL,
        grade_level INTEGER NOT NULL,
        condition_status TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Available',
        UNIQUE(brn, title, subject, grade_level)
    );

    CREATE TABLE IF NOT EXISTS assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        attendant_id INTEGER NOT NULL,
        issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        returned_at TEXT,
        issue_condition TEXT NOT NULL,
        return_condition TEXT,
        return_note TEXT,
        FOREIGN KEY(book_id) REFERENCES books(id),
        FOREIGN KEY(student_id) REFERENCES students(id),
        FOREIGN KEY(attendant_id) REFERENCES attendants(id)
    );

    CREATE TABLE IF NOT EXISTS fees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        assignment_id INTEGER,
        book_id INTEGER,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        paid INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        paid_at TEXT,
        FOREIGN KEY(student_id) REFERENCES students(id),
        FOREIGN KEY(assignment_id) REFERENCES assignments(id),
        FOREIGN KEY(book_id) REFERENCES books(id)
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    _ensure_brn_rules(conn)

    _ensure_column(conn, "assignments", "mismatch_pending", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "assignments", "return_note", "TEXT")
    _ensure_column(conn, "books", "status", "TEXT NOT NULL DEFAULT 'Available'")
    _ensure_column(conn, "books", "cross_grade", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "fees", "fee_type", "TEXT NOT NULL DEFAULT 'other'")
    # Gender on students. Existing rows (registered before this field
    # existed) come back as '' - they display as "Not set" in reports until
    # the attendant edits that student and picks M or F.
    _ensure_column(conn, "students", "gender", "TEXT NOT NULL DEFAULT ''")
    # Login role. Existing rows (from before this column existed) default to
    # 'staff'; the one-time UPDATE below promotes the original seeded admin
    # account specifically, so upgrading an existing install doesn't strand
    # everyone at 'staff' with no way to reach the admin-only pages.
    _ensure_column(conn, "attendants", "role", "TEXT NOT NULL DEFAULT 'staff'")

    if not conn.execute("SELECT 1 FROM attendants LIMIT 1").fetchone():
        conn.execute(
            "INSERT INTO attendants(username,password_hash,full_name,role) VALUES(?,?,?,?)",
            ("admin", generate_password_hash("admin123"), "System Administrator", "admin")
        )
    # Idempotent: on a fresh DB this is a no-op (the INSERT above already set
    # it); on an existing DB being upgraded, this promotes the original
    # admin login to the new 'admin' role.
    conn.execute("UPDATE attendants SET role='admin' WHERE username='admin'")

    if not conn.execute("SELECT 1 FROM settings WHERE key='school_year_end'").fetchone():
        today = date.today()
        year = today.year if today <= date(today.year, 6, 30) else today.year + 1
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('school_year_end', ?)",
            (date(year, 6, 30).isoformat(),)
        )
    if not conn.execute("SELECT 1 FROM settings WHERE key='late_fee_amount'").fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES('late_fee_amount', '0')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='contribution_fee_amount'").fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES('contribution_fee_amount', '0')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='contribution_override_enabled'").fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES('contribution_override_enabled', '0')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='auto_backup_enabled'").fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES('auto_backup_enabled', '0')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='auto_backup_frequency_hours'").fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES('auto_backup_frequency_hours', '24')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='auto_backup_retention'").fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES('auto_backup_retention', '7')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='auto_backup_last_run'").fetchone():
        conn.execute("INSERT INTO settings(key,value) VALUES('auto_backup_last_run', '')")
    conn.execute("UPDATE fees SET fee_type='replacement' WHERE fee_type='other'")
    # One-time (but safely repeatable) fix for rows entered with a raw Form
    # number instead of this system's 7-12 grade_level encoding. Once every
    # affected row is corrected these UPDATEs simply match zero rows.
    conn.execute("UPDATE books SET grade_level = grade_level + 6 WHERE grade_level BETWEEN 1 AND 6")
    conn.execute("UPDATE students SET grade_level = grade_level + 6 WHERE grade_level BETWEEN 1 AND 6")
    conn.commit()
    conn.close()

def _ensure_brn_rules(conn):
    """Migrate older databases from a globally unique BRN to the allowed
    composite uniqueness rule: the same BRN may be reused when at least one
    of title, subject, or grade level differs.
    """
    indexes = conn.execute("PRAGMA index_list(books)").fetchall()
    has_brn_only_unique = False
    has_composite_unique = False

    for index in indexes:
        index_name = index[1]
        is_unique = bool(index[2])
        if not is_unique:
            continue
        columns = [row[2] for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall()]
        if columns == ["brn"]:
            has_brn_only_unique = True
        if columns == ["brn", "title", "subject", "grade_level"]:
            has_composite_unique = True

    if has_brn_only_unique or not has_composite_unique:
        # Rebuild the books table so SQLite can remove the old UNIQUE(brn)
        # constraint while preserving book IDs referenced by assignments/fees.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS books_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brn TEXT NOT NULL,
                    title TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    grade_level INTEGER NOT NULL,
                    condition_status TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Available',
                    UNIQUE(brn, title, subject, grade_level)
                )
            """)
            conn.execute("""
                INSERT INTO books_new(id, brn, title, subject, grade_level, condition_status, status)
                SELECT id, brn, title, subject, grade_level, condition_status, status
                FROM books
            """)
            conn.execute("DROP TABLE books")
            conn.execute("ALTER TABLE books_new RENAME TO books")
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            conn.execute("DROP TABLE IF EXISTS books_new")
            conn.execute("PRAGMA foreign_keys = ON")
            raise


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def get_fee_amount_setting(conn, key, default="0"):
    val = get_setting(conn, key, default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)

def is_past_due(conn):
    school_year_end = get_setting(conn, "school_year_end")
    # Uses app_today() rather than date.today() directly, so an admin
    # date/time override (Settings) is honored here too - "past due" should
    # follow whatever the app currently considers "today", not always the
    # real system clock.
    return bool(school_year_end and app_today(conn).isoformat() > school_year_end)

def contribution_override_enabled(conn):
    return get_setting(conn, "contribution_override_enabled", "0") == "1"

def grades_compatible(book, student_grade_level):
    """True if `book` may be issued to a student in `student_grade_level`.

    - Exact grade match is always allowed.
    - Grades 10/11 (4th/5th form) always share books with each other.
    - Grades 7-9 (1st-3rd form) share books with each other only when the
      book has been explicitly flagged as cross_grade at registration/edit.
    """
    book_grade = book["grade_level"]
    if book_grade == student_grade_level:
        return True
    if book_grade in UPPER_FORM_GRADES and student_grade_level in UPPER_FORM_GRADES:
        return True
    if book["cross_grade"] and book_grade in LOWER_FORM_GRADES and student_grade_level in LOWER_FORM_GRADES:
        return True
    return False

def sync_late_fees(conn):
    """Auto-create a flat late fee for any overdue, unreturned assignment that doesn't already have one."""
    if not is_past_due(conn):
        return
    late_fee_amount = get_fee_amount_setting(conn, "late_fee_amount", "0")
    overdue_assignments = conn.execute("""
        SELECT a.id AS assignment_id, a.student_id, a.book_id, b.title, b.brn
        FROM assignments a
        JOIN books b ON b.id = a.book_id
        WHERE a.returned_at IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM fees f WHERE f.assignment_id = a.id AND f.fee_type = 'late'
        )
    """).fetchall()
    for row in overdue_assignments:
        conn.execute("""
            INSERT INTO fees(student_id, assignment_id, book_id, description, amount, fee_type)
            VALUES(?,?,?,?,?, 'late')
        """, (
            row["student_id"], row["assignment_id"], row["book_id"],
            f"Late return fee for {row['title']} ({row['brn']})", late_fee_amount
        ))

def fetch_outstanding_fees(conn, grade=None, class_name=None, subject=None):
    """Unified list of unpaid fees: replacement/late fee rows plus a synthesized row
    per student with an unpaid contribution."""
    contribution_fee_amount = get_fee_amount_setting(conn, "contribution_fee_amount", "0")
    query = """
        SELECT * FROM (
            SELECT f.id AS fee_id, f.fee_type, f.description, f.amount, f.created_at,
                   s.id AS student_id, s.student_number, s.first_name, s.last_name,
                   s.grade_level AS grade_level, s.class_name AS class_name,
                   b.brn, b.title, b.subject AS subject
            FROM fees f
            JOIN students s ON s.id = f.student_id
            LEFT JOIN books b ON b.id = f.book_id
            WHERE f.paid = 0

            UNION ALL

            SELECT NULL AS fee_id, 'contribution' AS fee_type,
                   'School contribution fee' AS description, ? AS amount, NULL AS created_at,
                   s.id AS student_id, s.student_number, s.first_name, s.last_name,
                   s.grade_level AS grade_level, s.class_name AS class_name,
                   NULL AS brn, NULL AS title, NULL AS subject
            FROM students s
            WHERE s.contribution_paid = 0
        ) WHERE 1=1
    """
    params = [contribution_fee_amount]
    if grade:
        query += " AND grade_level=?"; params.append(grade)
    if class_name:
        query += " AND class_name LIKE ?"; params.append(f"%{class_name}%")
    if subject:
        query += " AND subject LIKE ?"; params.append(f"%{subject}%")
    query += " ORDER BY fee_type, last_name, first_name"
    return conn.execute(query, params).fetchall()

def student_block_reason(conn, student_id):
    student = conn.execute("SELECT contribution_paid FROM students WHERE id=?", (student_id,)).fetchone()
    # Unpaid contribution fee blocks assignment unless the admin-controlled
    # system-wide override is turned on (Settings). The fee itself stays
    # unpaid either way - this only lifts the block on issuing books.
    if not student["contribution_paid"] and not contribution_override_enabled(conn):
        return "This student's contribution fee has not been paid."
    unpaid = conn.execute(
        "SELECT COUNT(*) FROM fees WHERE student_id=? AND paid=0", (student_id,)
    ).fetchone()[0]
    if unpaid:
        return "This student has an outstanding fee that must be paid before more books can be assigned."
    if is_past_due(conn):
        outstanding_books = conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE student_id=? AND returned_at IS NULL", (student_id,)
        ).fetchone()[0]
        if outstanding_books:
            return "This student has an outstanding book (overdue, not yet returned) that must be returned before more books can be assigned."
    return None

@app.before_request
def _enforce_license_hook():
    """Runs before every request. Blocks access with a locked screen once
    current_state() reports 'locked' (explicit revoke, or the cached
    check-in has aged past the grace period with the server unreachable).
    Static assets are excluded so locked.html can still load its CSS;
    nothing else is exempted, including the login page, so a locked
    install can't be used at all until access is restored."""
    if request.endpoint == "static":
        return
    if license_check.is_locked():
        return render_template("locked.html"), 403


@app.before_request
def _sync_late_fees_hook():
    if "attendant_id" in session:
        conn = db()
        sync_late_fees(conn)
        conn.commit()
        conn.close()

@app.route("/")
def home():
    return redirect(url_for("dashboard"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        conn = db()
        user = conn.execute(
            "SELECT * FROM attendants WHERE username=? AND active=1", (username,)
        ).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["attendant_id"] = user["id"]
            session["attendant_name"] = user["full_name"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def account_password():
    """Allow any signed-in user (admin or staff) to change their own password."""
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_password or not new_password or not confirm_password:
            flash("Current password, new password, and confirmation are all required.", "error")
            return render_template("change_password.html")

        if len(new_password) < 8:
            flash("New password must be at least 8 characters long.", "error")
            return render_template("change_password.html")

        if new_password != confirm_password:
            flash("The new passwords do not match.", "error")
            return render_template("change_password.html")

        if current_password == new_password:
            flash("Your new password must be different from your current password.", "error")
            return render_template("change_password.html")

        conn = db()
        user = conn.execute(
            "SELECT password_hash FROM attendants WHERE id=? AND active=1",
            (session["attendant_id"],)
        ).fetchone()

        if not user or not check_password_hash(user["password_hash"], current_password):
            conn.close()
            flash("Your current password is incorrect.", "error")
            return render_template("change_password.html")

        conn.execute(
            "UPDATE attendants SET password_hash=? WHERE id=?",
            (generate_password_hash(new_password), session["attendant_id"])
        )
        conn.commit()
        conn.close()
        flash("Your password has been changed successfully.", "success")
        return redirect(url_for("account_password"))

    return render_template("change_password.html")

# Backwards-compatible endpoint name so any template still calling
# url_for('change_password') keeps working.
app.add_url_rule("/account/change-password", endpoint="change_password",
                 view_func=account_password, methods=["GET", "POST"])


@app.route("/dashboard")
@login_required
def dashboard():
    conn = db()
    school_year_end = get_setting(conn, "school_year_end")
    past_due = is_past_due(conn)

    books_issued_rows = conn.execute("""
        SELECT s.grade_level AS grade_level, COUNT(*) AS total
        FROM assignments a
        JOIN students s ON s.id = a.student_id
        WHERE a.returned_at IS NULL
        GROUP BY s.grade_level
    """).fetchall()
    issued_counts = {}
    for row in books_issued_rows:
        try:
            g = int(row["grade_level"])
        except (TypeError, ValueError):
            continue
        issued_counts[g] = row["total"]
    issued_grades_to_show = sorted(set(range(7, 13)) | set(issued_counts.keys()))
    books_issued_by_grade = [
        {"grade_level": g, "form_label": form_label(g), "total": issued_counts.get(g, 0)}
        for g in issued_grades_to_show
    ]

    books_outstanding_by_grade = []
    if past_due:
        outstanding_rows = conn.execute("""
            SELECT s.grade_level AS grade_level, COUNT(*) AS total
            FROM assignments a
            JOIN students s ON s.id = a.student_id
            WHERE a.returned_at IS NULL
            GROUP BY s.grade_level
            ORDER BY s.grade_level
        """).fetchall()
        books_outstanding_by_grade = [
            {"grade_level": row["grade_level"], "form_label": form_label(row["grade_level"]), "total": row["total"]}
            for row in outstanding_rows
        ]

    outstanding_fees = fetch_outstanding_fees(conn)

    pending_decisions = conn.execute("""
        SELECT a.*, b.brn, b.title, s.id AS student_id, s.student_number, s.first_name, s.last_name
        FROM assignments a
        JOIN books b ON b.id=a.book_id
        JOIN students s ON s.id=a.student_id
        WHERE a.mismatch_pending=1
        ORDER BY a.returned_at
    """).fetchall()

    conn.close()
    return render_template(
        "dashboard.html",
        books_issued_by_grade=books_issued_by_grade,
        books_outstanding_by_grade=books_outstanding_by_grade,
        outstanding_fees=outstanding_fees,
        pending_decisions=pending_decisions,
        school_year_end=school_year_end,
        is_past_due=past_due,
    )

def _breakdown(rows, key, canonical_order, color_map):
    """Groups the already-filtered report rows by an arbitrary field,
    returning entries in canonical order (any unexpected value falls in
    after) with count/percent/color/cumulative-position, ready to both
    render as a legend and feed a CSS conic-gradient pie with no rounding
    gaps (the gradient stops use the raw fraction; only the displayed
    percent is rounded). Generic version shared by the condition and gender
    breakdowns below."""
    counts = {}
    for r in rows:
        counts[r[key]] = counts.get(r[key], 0) + 1
    total = len(rows)
    order = [c for c in canonical_order if c in counts] + [c for c in counts if c not in canonical_order]
    breakdown = []
    cursor = 0.0
    for c in order:
        n = counts[c]
        raw_percent = (n / total * 100) if total else 0
        start = cursor
        cursor += raw_percent
        breakdown.append({
            "label": c,
            "count": n,
            "percent": round(raw_percent, 1),
            "start": round(start, 4),
            "end": round(cursor, 4),
            "color": color_map.get(c, "#999999"),
        })
    return breakdown

def _condition_breakdown(rows, key):
    return _breakdown(rows, key, CONDITIONS, CONDITION_COLORS)

def _gender_breakdown(rows, key="gender"):
    """Same shape as _condition_breakdown, but for the student gender field.
    A row with no gender set (legacy student, never edited since this field
    was added) is grouped under 'Not set' rather than silently dropped."""
    breakdown = _breakdown(rows, key, GENDERS, GENDER_COLORS)
    for item in breakdown:
        item["label"] = GENDER_LABELS.get(item["label"], item["label"] or "Not set")
    return breakdown

def _condition_gradient(breakdown):
    if not breakdown:
        return "#eeeeee 0% 100%"
    return ", ".join(f"{item['color']} {item['start']}% {item['end']}%" for item in breakdown)

@app.route("/reports/books-issued")
@login_required
def report_books_issued():
    grade = normalize_grade_filter(request.args.get("grade", ""))
    class_name = request.args.get("class_name", "").strip()
    subject = request.args.get("subject", "").strip()
    title = request.args.get("title", "").strip()
    student = request.args.get("student", "").strip()
    conn = db()
    query = """
        SELECT b.brn, b.title, b.subject, b.grade_level,
               b.condition_status AS book_condition, a.issue_condition,
               s.id AS student_id, s.student_number, s.first_name, s.last_name,
               s.class_name, s.grade_level AS student_grade_level, s.gender,
               a.id AS assignment_id, a.issued_at
        FROM assignments a
        JOIN books b ON b.id = a.book_id
        JOIN students s ON s.id = a.student_id
        WHERE a.returned_at IS NULL
    """
    params = []
    if grade:
        query += " AND b.grade_level=?"; params.append(grade)
    if class_name:
        query += " AND s.class_name LIKE ?"; params.append(f"%{class_name}%")
    if subject:
        query += " AND b.subject LIKE ?"; params.append(f"%{subject}%")
    if title:
        query += " AND b.title LIKE ?"; params.append(f"%{title}%")
    if student:
        query += " AND (s.student_number LIKE ? OR s.first_name LIKE ? OR s.last_name LIKE ?)"
        params += [f"%{student}%"] * 3
    # Sorted by student first (last name, first name) so each student's own
    # books sit together, then by subject/title within that student so
    # alike books cluster instead of being scattered - this ordering is
    # what the vertical grouped display below relies on.
    query += " ORDER BY s.last_name, s.first_name, s.id, b.subject, b.title"
    rows = conn.execute(query, params).fetchall()

    # Second, independent dataset: assignments that HAVE been returned,
    # with the same filters applied. This is deliberately not derived from
    # `rows` above (which is scoped to a.returned_at IS NULL, i.e. only
    # what's still checked out) - the "Return Condition" and "Returned by
    # gender" pies need to reflect what actually came back, which is a
    # disjoint set of assignment rows from what's currently issued.
    returned_query = """
        SELECT b.brn, b.title, b.subject, b.grade_level,
               a.return_condition, a.issue_condition,
               s.id AS student_id, s.student_number, s.first_name, s.last_name,
               s.class_name, s.grade_level AS student_grade_level, s.gender,
               a.id AS assignment_id, a.issued_at, a.returned_at
        FROM assignments a
        JOIN books b ON b.id = a.book_id
        JOIN students s ON s.id = a.student_id
        WHERE a.returned_at IS NOT NULL
    """
    returned_params = []
    if grade:
        returned_query += " AND b.grade_level=?"; returned_params.append(grade)
    if class_name:
        returned_query += " AND s.class_name LIKE ?"; returned_params.append(f"%{class_name}%")
    if subject:
        returned_query += " AND b.subject LIKE ?"; returned_params.append(f"%{subject}%")
    if title:
        returned_query += " AND b.title LIKE ?"; returned_params.append(f"%{title}%")
    if student:
        returned_query += " AND (s.student_number LIKE ? OR s.first_name LIKE ? OR s.last_name LIKE ?)"
        returned_params += [f"%{student}%"] * 3
    returned_rows = conn.execute(returned_query, returned_params).fetchall()
    conn.close()

    # Vertical grouping: one card per student (in the sorted order above),
    # each listing that student's books already sorted by subject/title, so
    # a class or grade filter with many students reads as a clean list of
    # legible groups instead of one wide, hard-to-scan table.
    grouped_by_student = []
    current_group = None
    for r in rows:
        if current_group is None or current_group["student_id"] != r["student_id"]:
            current_group = {
                "student_id": r["student_id"],
                "student_number": r["student_number"],
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "class_name": r["class_name"],
                "form_label": form_label(r["student_grade_level"]),
                "gender": r["gender"],
                "gender_label": GENDER_LABELS.get(r["gender"], "Not set"),
                "books": [],
            }
            grouped_by_student.append(current_group)
        current_group["books"].append(r)

    # "Return Condition" pie: what condition books actually came back in,
    # drawn from returned_rows (not `rows` - currently-issued books haven't
    # been returned yet, so they have no return_condition to show here).
    return_condition_breakdown = _condition_breakdown(returned_rows, "return_condition")
    return_condition_gradient = _condition_gradient(return_condition_breakdown)
    condition_at_issue = _condition_breakdown(rows, "issue_condition")
    condition_at_issue_gradient = _condition_gradient(condition_at_issue)
    # Gender split of books currently issued (unreturned) vs. gender split
    # of books that have been returned - two separate pies over two
    # separate row sets, so a student only shows up in whichever one
    # matches their book's actual status.
    gender_breakdown = _gender_breakdown(rows)
    gender_breakdown_gradient = _condition_gradient(gender_breakdown)
    returned_gender_breakdown = _gender_breakdown(returned_rows)
    returned_gender_gradient = _condition_gradient(returned_gender_breakdown)

    # Context header reflects the most specific filter actually submitted -
    # student is most specific, then class, then grade - falling back to the
    # next coarser filter if the more specific one wasn't used. This is
    # driven by which filters were supplied, not by whether the result set
    # happens to contain only one student, so a class filter that legitimately
    # matches several students correctly identifies the class, not one of
    # its students. Subject/title are appended as qualifiers regardless of
    # which primary filter matched.
    student_ids_seen = {g["student_id"] for g in grouped_by_student}
    context_primary = None
    context_student_id = None
    if student:
        if len(student_ids_seen) == 1 and rows:
            r0 = rows[0]
            context_primary = (
                f"{r0['first_name']} {r0['last_name']} ({r0['student_number']}) "
                f"\u2014 {form_label(r0['student_grade_level'])} \u00b7 {r0['class_name']}"
            )
            context_student_id = r0["student_id"]
        elif len(student_ids_seen) > 1:
            context_primary = f"{len(student_ids_seen)} students matching \u201c{student}\u201d"
        else:
            context_primary = f"No students matching \u201c{student}\u201d"
    elif class_name:
        distinct_classes = {r["class_name"] for r in rows}
        if len(distinct_classes) == 1 and rows:
            context_primary = (
                f"Class {next(iter(distinct_classes))} "
                f"\u2014 {len(student_ids_seen)} student(s) with books issued"
            )
        else:
            context_primary = f"Class matching \u201c{class_name}\u201d"
    elif grade:
        try:
            g = int(grade)
            context_primary = f"{form_label(g)} (Grade {g})"
        except ValueError:
            context_primary = f"Grade \u201c{grade}\u201d"

    context_qualifiers = []
    if subject:
        context_qualifiers.append(f"Subject: {subject}")
    if title:
        context_qualifiers.append(f"Title: {title}")

    return render_template(
        "report_books_issued.html",
        rows=rows, returned_rows=returned_rows, grouped_by_student=grouped_by_student,
        grade=grade, class_name=class_name, subject=subject, title=title, student=student,
        tally_total=len(rows),
        return_condition_breakdown=return_condition_breakdown, return_condition_gradient=return_condition_gradient,
        condition_at_issue=condition_at_issue, condition_at_issue_gradient=condition_at_issue_gradient,
        gender_breakdown=gender_breakdown, gender_breakdown_gradient=gender_breakdown_gradient,
        returned_gender_breakdown=returned_gender_breakdown, returned_gender_gradient=returned_gender_gradient,
        context_primary=context_primary,
        context_student_id=context_student_id,
        context_qualifiers=context_qualifiers,
    )

@app.route("/reports/books-outstanding")
@login_required
def report_books_outstanding():
    grade = normalize_grade_filter(request.args.get("grade", ""))
    class_name = request.args.get("class_name", "").strip()
    subject = request.args.get("subject", "").strip()
    conn = db()
    school_year_end = get_setting(conn, "school_year_end")
    rows = []
    if is_past_due(conn):
        query = """
            SELECT b.brn, b.title, b.subject, b.grade_level,
                   s.id AS student_id, s.student_number, s.first_name, s.last_name, s.class_name,
                   a.id AS assignment_id, a.issued_at
            FROM assignments a
            JOIN books b ON b.id = a.book_id
            JOIN students s ON s.id = a.student_id
            WHERE a.returned_at IS NULL
        """
        params = []
        if grade:
            query += " AND b.grade_level=?"; params.append(grade)
        if class_name:
            query += " AND s.class_name LIKE ?"; params.append(f"%{class_name}%")
        if subject:
            query += " AND b.subject LIKE ?"; params.append(f"%{subject}%")
        query += " ORDER BY s.last_name, s.first_name"
        rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template(
        "report_books_outstanding.html",
        rows=rows, grade=grade, class_name=class_name, subject=subject,
        school_year_end=school_year_end,
    )


@app.route("/reports/fees-outstanding")
@login_required
def report_fees_outstanding():
    grade = normalize_grade_filter(request.args.get("grade", ""))
    class_name = request.args.get("class_name", "").strip()
    subject = request.args.get("subject", "").strip()
    conn = db()
    rows = fetch_outstanding_fees(conn, grade or None, class_name or None, subject or None)
    conn.close()
    return render_template(
        "report_fees_outstanding.html",
        rows=rows, grade=grade, class_name=class_name, subject=subject,
    )

# --- Settings: any signed-in user can open this page (staff see only the
# Account tab, for changing their own password). Due dates, fee amounts,
# overrides, backups and user management are policy decisions and stay
# admin-only - the POST handler below and each /settings/* action route
# enforce that on the server, not just by hiding tabs in the template.

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    # Every signed-in user can open Settings (staff get the Account tab only);
    # everything that changes system policy stays admin-only.
    is_admin = session.get("role") == "admin"
    if request.method == "POST" and not is_admin:
        flash("Only an administrator can change these settings.", "error")
        return redirect(url_for("settings"))
    conn = db()
    if request.method == "POST":
        for key in ("school_year_end", "late_fee_amount", "contribution_fee_amount"):
            if key in request.form:
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, request.form[key])
                )
        conn.commit()
        flash("Settings updated.", "success")
    school_year_end = get_setting(conn, "school_year_end")
    late_fee_amount = get_setting(conn, "late_fee_amount", "0")
    contribution_fee_amount = get_setting(conn, "contribution_fee_amount", "0")
    override_enabled = contribution_override_enabled(conn)

    # Date/time override state, for the Date & Time panel. current_app_time
    # is what app_now() actually resolves to right now (system clock, or
    # the override if one is active) - shown next to the real system_time
    # so an admin can see at a glance whether an override is in effect.
    time_override_enabled = get_setting(conn, "time_override_enabled", "0") == "1"
    time_override_value = get_setting(conn, "time_override_value", "")
    current_app_time = app_now(conn)
    system_time = datetime.now()

    auto_backup_enabled = get_setting(conn, "auto_backup_enabled", "0") == "1"
    auto_backup_frequency_hours = get_setting(conn, "auto_backup_frequency_hours", "24")
    auto_backup_retention = get_setting(conn, "auto_backup_retention", "7")
    auto_backup_last_run = get_setting(conn, "auto_backup_last_run", "")

    conn.close()
    return render_template(
        "settings.html",
        school_year_end=school_year_end,
        late_fee_amount=late_fee_amount,
        contribution_fee_amount=contribution_fee_amount,
        contribution_override_enabled=override_enabled,
        time_override_enabled=time_override_enabled,
        time_override_value=time_override_value,
        current_app_time=current_app_time,
        system_time=system_time,
        auto_backup_enabled=auto_backup_enabled,
        auto_backup_frequency_hours=auto_backup_frequency_hours,
        auto_backup_retention=auto_backup_retention,
        auto_backup_last_run=auto_backup_last_run,
        license_info=license_check.status_info(),
    )

@app.route("/settings/contribution-override", methods=["POST"])
@login_required
@admin_required
def toggle_contribution_override():
    conn = db()
    new_val = "0" if contribution_override_enabled(conn) else "1"
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('contribution_override_enabled', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (new_val,)
    )
    conn.commit()
    conn.close()
    flash(f"Contribution fee override turned {'ON' if new_val == '1' else 'OFF'}.", "success")
    return redirect(url_for("settings"))

@app.route("/settings/time-override", methods=["POST"])
@login_required
@admin_required
def set_time_override():
    conn = db()
    if request.form.get("use_system_time"):
        # Clear the override - app_now() falls straight back to the real
        # system clock the moment this flag is off, regardless of whatever
        # stale value is still sitting in time_override_value.
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('time_override_enabled', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("0",)
        )
        conn.commit()
        conn.close()
        flash("Date/time override cleared. The app is using the real system clock again.", "success")
        return redirect(url_for("settings"))

    override_date = request.form.get("override_date", "").strip()
    override_time = request.form.get("override_time", "").strip() or "00:00"
    if not override_date:
        conn.close()
        flash("Choose a date for the override, or use \u201cUse system time\u201d instead.", "error")
        return redirect(url_for("settings"))
    try:
        override_dt = datetime.fromisoformat(f"{override_date}T{override_time}")
    except ValueError:
        conn.close()
        flash("That date/time could not be understood.", "error")
        return redirect(url_for("settings"))

    conn.execute(
        "INSERT INTO settings(key,value) VALUES('time_override_enabled', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("1",)
    )
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('time_override_value', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (override_dt.isoformat(),)
    )
    conn.commit()
    conn.close()
    flash(f"Date/time override set to {override_dt.strftime('%Y-%m-%d %H:%M')}.", "success")
    return redirect(url_for("settings"))

def create_database_backup(prefix="backup"):
    """Create a consistent, self-contained SQLite backup in BASE_DIR/backups.

    sqlite3.Connection.backup() is used instead of copying bookroom.db directly,
    so the snapshot remains valid even when SQLite is using a WAL journal.
    Returns the created Path.
    """
    backups_dir = BASE_DIR / "backups"
    backups_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = backups_dir / f"{prefix}-{timestamp}.db"

    source = sqlite3.connect(DB)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    return destination


def prune_automatic_backups(retention):
    """Keep only the newest `retention` automatic backups."""
    backups_dir = BASE_DIR / "backups"
    if not backups_dir.exists():
        return
    files = sorted(backups_dir.glob("auto-backup-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old_file in files[max(1, retention):]:
        try:
            old_file.unlink()
        except OSError:
            pass


def perform_automatic_backup():
    """Run one automatic backup and record its timestamp in settings."""
    destination = create_database_backup("auto-backup")
    conn = db()
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('auto_backup_last_run', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (datetime.now().isoformat(timespec="seconds"),)
    )
    retention_raw = get_setting(conn, "auto_backup_retention", "7")
    try:
        retention = max(1, min(100, int(retention_raw)))
    except ValueError:
        retention = 7
    conn.commit()
    conn.close()
    prune_automatic_backups(retention)
    return destination


def automatic_backup_worker():
    """Background worker for the packaged application.

    It checks the setting once per minute. Backups therefore happen while the
    Windows app is running; the worker is a daemon so it never prevents the
    application from closing.
    """
    while True:
        try:
            conn = db()
            enabled = get_setting(conn, "auto_backup_enabled", "0") == "1"
            frequency_raw = get_setting(conn, "auto_backup_frequency_hours", "24")
            last_run_raw = get_setting(conn, "auto_backup_last_run", "")
            conn.close()

            if enabled:
                try:
                    frequency_hours = max(1, min(720, int(frequency_raw)))
                except ValueError:
                    frequency_hours = 24

                due = True
                if last_run_raw:
                    try:
                        last_run = datetime.fromisoformat(last_run_raw)
                        due = (datetime.now() - last_run).total_seconds() >= frequency_hours * 3600
                    except ValueError:
                        due = True

                if due:
                    try:
                        perform_automatic_backup()
                    except Exception:
                        # A failed automatic backup should never crash the
                        # bookroom application. The next scheduler check will
                        # try again.
                        pass
        except Exception:
            pass
        time.sleep(60)


def start_automatic_backup_worker():
    if getattr(app, "_auto_backup_worker_started", False):
        return
    app._auto_backup_worker_started = True
    worker = threading.Thread(target=automatic_backup_worker, daemon=True, name="bookroom-auto-backup")
    worker.start()


@app.route("/settings/backup", methods=["POST"])
@login_required
@admin_required
def backup_database():
    destination = create_database_backup("bookroom-backup")
    return send_file(destination, as_attachment=True, download_name=destination.name)

@app.route("/settings/automatic-backup", methods=["POST"])
@login_required
@admin_required
def configure_automatic_backup():
    enabled = "1" if request.form.get("auto_backup_enabled") == "1" else "0"
    frequency = request.form.get("auto_backup_frequency_hours", "24")
    retention = request.form.get("auto_backup_retention", "7")

    try:
        frequency_value = max(1, min(720, int(frequency)))
    except ValueError:
        frequency_value = 24
    try:
        retention_value = max(1, min(100, int(retention)))
    except ValueError:
        retention_value = 7

    conn = db()
    for key, value in (
        ("auto_backup_enabled", enabled),
        ("auto_backup_frequency_hours", str(frequency_value)),
        ("auto_backup_retention", str(retention_value)),
    ):
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
    conn.commit()
    conn.close()

    prune_automatic_backups(retention_value)
    if enabled:
        try:
            destination = perform_automatic_backup()
            flash(f"Automatic backups enabled ({frequency_value} hour interval). First backup created: {destination.name}", "success")
        except Exception as e:
            flash(f"Automatic backups were enabled, but the first backup could not be created: {e}", "error")
    else:
        flash("Automatic backups disabled. Existing backups were kept.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/automatic-backup/run", methods=["POST"])
@login_required
@admin_required
def run_automatic_backup_now():
    try:
        destination = perform_automatic_backup()
        flash(f"Automatic backup created: {destination.name}", "success")
    except Exception as e:
        flash(f"Backup failed: {e}", "error")
    return redirect(url_for("settings"))


ALLOWED_RESTORE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}

@app.route("/settings/restore", methods=["POST"])
@login_required
@admin_required
def restore_database():
    file = request.files.get("backup_file")
    if not file or not file.filename:
        flash("Choose a backup file to restore.", "error")
        return redirect(url_for("settings"))

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_RESTORE_EXTENSIONS:
        flash("That doesn't look like a database backup file (expected .db, .sqlite, or .sqlite3).", "error")
        return redirect(url_for("settings"))

    # Read the whole upload into memory and validate the SQLite file header
    # BEFORE touching anything on disk, so a bad or unrelated file can
    # never leave the app mid-way through overwriting its own live database.
    file_bytes = file.read()
    if not file_bytes.startswith(b"SQLite format 3\x00"):
        flash("That file isn't a valid SQLite database. Restore cancelled - nothing was changed.", "error")
        return redirect(url_for("settings"))

    # Safety copy of the current database before it's overwritten, so a bad
    # or unwanted restore can always be undone by an admin with file-system
    # access, even though there's no in-app "undo" for this action.
    backups_dir = BASE_DIR / "backups"
    backups_dir.mkdir(exist_ok=True)
    safety_copy = backups_dir / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    try:
        shutil.copy2(DB, safety_copy)
    except OSError as e:
        flash(f"Restore cancelled: could not save a safety copy of the current database first ({e}).", "error")
        return redirect(url_for("settings"))

    try:
        with open(DB, "wb") as f:
            f.write(file_bytes)
    except OSError as e:
        flash(
            f"Restore failed while writing the new database ({e}). "
            f"The previous database was safely backed up to backups/{safety_copy.name} and left in place.",
            "error"
        )
        return redirect(url_for("settings"))

    # The connection this request was using, and every other logged-in
    # session, is now pointing at data that may not match what's actually
    # in the file (old IDs, a stale role, etc.) - clearing the session and
    # sending everyone back to login is the simplest way to guarantee a
    # clean state after the underlying data has been swapped out entirely.
    flash(
        f"Database restored from {file.filename}. The previous database was saved to "
        f"backups/{safety_copy.name} in case you need to roll back. Please log in again.",
        "success"
    )
    session.clear()
    return redirect(url_for("login"))

@app.route("/fees/<int:fee_id>/pay", methods=["POST"])
@login_required
@admin_required
def pay_fee(fee_id):
    conn = db()
    conn.execute(
        "UPDATE fees SET paid=1, paid_at=CURRENT_TIMESTAMP WHERE id=?", (fee_id,)
    )
    conn.commit()
    conn.close()
    flash("Fee marked as paid.", "success")
    return redirect(request.referrer or url_for("dashboard"))

@app.route("/issue-book")
@login_required
def issue_book_page():
    student_number = request.args.get("student_number", "").strip()
    brn = request.args.get("brn", "").strip()
    student = None
    matches = []
    block_reason = None
    conn = db()
    if student_number:
        results = find_students(conn, student_number)
        if len(results) == 1:
            student = results[0]
            block_reason = student_block_reason(conn, student["id"])
        else:
            matches = results
    conn.close()
    return render_template(
        "issue_book.html",
        student_number=student_number,
        brn=brn,
        student=student,
        matches=matches,
        block_reason=block_reason,
        genders=GENDERS,
    )

@app.route("/issue-book/new-student", methods=["POST"])
@login_required
def issue_book_new_student():
    conn = db()
    gender, gender_error = _validate_gender(request.form)
    if gender_error:
        conn.close()
        flash(gender_error, "error")
        return redirect(url_for("issue_book_page", student_number=request.form.get("student_number", "").strip()))
    try:
        cur = conn.execute("""
            INSERT INTO students(student_number,first_name,last_name,grade_level,class_name,gender,contribution_paid)
            VALUES(?,?,?,?,?,?,?)
        """, (
            request.form["student_number"].strip(),
            request.form["first_name"].strip(),
            request.form["last_name"].strip(),
            normalize_grade_level(int(request.form["grade_level"])),
            request.form["class_name"].strip(),
            gender,
            1 if request.form.get("contribution_paid") else 0
        ))
        conn.commit()
        student_id = cur.lastrowid
        conn.close()
        flash("Student registered successfully.", "success")
        brn = request.form.get("brn", "").strip()
        return redirect(url_for("assign", student_id=student_id, brn=brn) if brn else url_for("assign", student_id=student_id))
    except sqlite3.IntegrityError:
        conn.close()
        flash("That student number already exists.", "error")
        return redirect(url_for("issue_book_page", student_number=request.form.get("student_number", "").strip()))

@app.route("/return-book")
@login_required
def return_book_page():
    student_number = request.args.get("student_number", "").strip()
    student = None
    matches = []
    current_books = []
    conn = db()
    if student_number:
        results = find_students(conn, student_number)
        if len(results) == 1:
            student = results[0]
            current_books = conn.execute("""
                SELECT a.id AS assignment_id, a.issued_at, a.issue_condition, b.brn, b.title, b.subject
                FROM assignments a
                JOIN books b ON b.id = a.book_id
                WHERE a.student_id=? AND a.returned_at IS NULL
                ORDER BY a.issued_at
            """, (student["id"],)).fetchall()
        else:
            matches = results
    conn.close()
    return render_template(
        "return_book.html",
        student_number=student_number,
        student=student,
        matches=matches,
        current_books=current_books,
        conditions=CONDITIONS,
    )

@app.route("/books", methods=["GET", "POST"])
@login_required
def books():
    conn = db()
    if request.method == "POST":
        try:
            conn.execute("""
                INSERT INTO books(brn,title,subject,grade_level,condition_status,cross_grade)
                VALUES(?,?,?,?,?,?)
            """, (
                request.form["brn"].strip(),
                request.form["title"].strip(),
                request.form["subject"].strip(),
                normalize_grade_level(int(request.form["grade_level"])),
                request.form["condition_status"],
                1 if request.form.get("cross_grade") else 0
            ))
            conn.commit()
            flash("Book registered successfully.", "success")
        except sqlite3.IntegrityError:
            flash("A book with this BRN, title, subject, and grade level already exists.", "error")

    q = request.args.get("q", "").strip()
    prefill_brn = request.args.get("brn", "").strip()
    grade = normalize_grade_filter(request.args.get("grade", ""))
    subject = request.args.get("subject", "").strip()
    condition = request.args.get("condition", "").strip()
    query = "SELECT * FROM books WHERE 1=1"
    params = []
    if q:
        query += " AND (brn LIKE ? OR title LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if grade:
        query += " AND grade_level=?"; params.append(grade)
    if subject:
        query += " AND subject LIKE ?"; params.append(f"%{subject}%")
    if condition:
        query += " AND condition_status=?"; params.append(condition)
    query += " ORDER BY grade_level, subject, title"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("books.html", books=rows, conditions=CONDITIONS, prefill_brn=prefill_brn)

@app.route("/students", methods=["GET", "POST"])
@login_required
def students():
    conn = db()
    if request.method == "POST":
        gender, gender_error = _validate_gender(request.form)
        if gender_error:
            flash(gender_error, "error")
        else:
            try:
                conn.execute("""
                    INSERT INTO students(student_number,first_name,last_name,grade_level,class_name,gender,contribution_paid)
                    VALUES(?,?,?,?,?,?,?)
                """, (
                    request.form["student_number"].strip(),
                    request.form["first_name"].strip(),
                    request.form["last_name"].strip(),
                    normalize_grade_level(int(request.form["grade_level"])),
                    request.form["class_name"].strip(),
                    gender,
                    1 if request.form.get("contribution_paid") else 0
                ))
                conn.commit()
                flash("Student registered successfully.", "success")
            except sqlite3.IntegrityError:
                flash("That student number already exists.", "error")

    q = request.args.get("q", "").strip()
    grade = normalize_grade_filter(request.args.get("grade", ""))
    class_name = request.args.get("class_name", "").strip()
    gender_filter = request.args.get("gender", "").strip().upper()
    query = "SELECT * FROM students WHERE 1=1"
    params = []
    if q:
        query += " AND (student_number LIKE ? OR first_name LIKE ? OR last_name LIKE ?)"
        params += [f"%{q}%"] * 3
    if grade:
        query += " AND grade_level=?"; params.append(grade)
    if class_name:
        query += " AND class_name LIKE ?"; params.append(f"%{class_name}%")
    if gender_filter in GENDERS:
        query += " AND gender=?"; params.append(gender_filter)
    query += " ORDER BY grade_level, class_name, last_name, first_name"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("students.html", students=rows, genders=GENDERS)

@app.route("/students/<int:student_id>/toggle-fee", methods=["POST"])
@login_required
def toggle_fee(student_id):
    conn = db()
    conn.execute("""
        UPDATE students
        SET contribution_paid = CASE contribution_paid WHEN 1 THEN 0 ELSE 1 END
        WHERE id=?
    """, (student_id,))
    conn.commit(); conn.close()
    return redirect(request.referrer or url_for("students"))

@app.route("/return/<int:assignment_id>", methods=["POST"])
@login_required
def return_book(assignment_id):
    condition = request.form["return_condition"]
    # Optional free-text note (e.g. what damage was found). Never required;
    # stored as NULL when left blank so History shows a dash.
    return_note = request.form.get("return_note", "").strip()[:1000] or None
    decision = request.form.get("decision")
    conn = db()
    assignment = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    book = conn.execute("SELECT * FROM books WHERE id=?", (assignment["book_id"],)).fetchone() if assignment else None

    # How many steps worse (positive) or better (negative) the returned
    # condition is vs. the condition it was issued in, measured by position
    # in CONDITIONS. A decision (no-replacement vs. replace) is only
    # required once that separation reaches 2 degrees in either direction;
    # a replacement fee is only allowed when the book came back 2+ degrees
    # WORSE (a much-improved return should never carry a fee).
    degrees = condition_degrees_worse(assignment["issue_condition"], condition) if assignment else 0
    needs_decision = abs(degrees) >= 2
    replace_allowed = degrees >= 2

    if not assignment or assignment["returned_at"]:
        flash("Assignment not found or already returned.", "error")
    elif not needs_decision:
        # Same condition, or within 1 degree either way - no decision required.
        conn.execute("""
            UPDATE assignments SET returned_at=CURRENT_TIMESTAMP, return_condition=?, return_note=?
            WHERE id=?
        """, (condition, return_note, assignment_id))
        conn.execute("""
            UPDATE books SET status='Available', condition_status=? WHERE id=?
        """, (condition, assignment["book_id"]))
        conn.commit()
        flash("Book returned within acceptable condition range.", "success")
    elif decision == "no_replacement":
        conn.execute("""
            UPDATE assignments SET returned_at=CURRENT_TIMESTAMP, return_condition=?, return_note=?
            WHERE id=?
        """, (condition, return_note, assignment_id))
        conn.execute("""
            UPDATE books SET status='Available', condition_status=? WHERE id=?
        """, (condition, assignment["book_id"]))
        conn.commit()
        flash("Book returned with a significant condition change, but no replacement is needed. Book is available again.", "success")
    elif decision == "replace" and replace_allowed:
        try:
            amount = float(request.form.get("amount") or 0)
        except ValueError:
            amount = 0
        conn.execute("""
            UPDATE assignments SET returned_at=CURRENT_TIMESTAMP, return_condition=?, return_note=?
            WHERE id=?
        """, (condition, return_note, assignment_id))
        conn.execute("""
            INSERT INTO fees(student_id, assignment_id, book_id, description, amount, fee_type)
            VALUES(?,?,?,?,?, 'replacement')
        """, (
            assignment["student_id"], assignment_id, book["id"],
            f"Replacement for {book['title']} ({book['brn']})", amount
        ))
        conn.execute("UPDATE books SET status='Awaiting Replacement' WHERE id=?", (book["id"],))
        conn.commit()
        flash("Book was returned significantly worse than issued. A replacement fee has been added; the student is blocked from new assignments until it is paid.", "error")
    elif decision == "replace" and not replace_allowed:
        # Defensive: the UI disables this combination, but never trust the
        # client - a book returned in a much BETTER condition (or only
        # mildly worse) cannot carry a replacement fee even if the form is
        # submitted directly.
        flash("Replacement is not applicable: the book was not returned in a worse condition by 2 or more degrees.", "error")
    else:
        conn.execute("""
            UPDATE assignments SET returned_at=CURRENT_TIMESTAMP, return_condition=?, return_note=?, mismatch_pending=1
            WHERE id=?
        """, (condition, return_note, assignment_id))
        conn.execute("""
            UPDATE books SET status='Awaiting Decision' WHERE id=?
        """, (assignment["book_id"],))
        conn.commit()
        flash("Book returned with a significant condition change. A replacement decision is required.", "error")
    conn.close()
    return redirect(request.referrer or url_for("return_book_page"))

@app.route("/assignments/<int:assignment_id>/resolve-condition", methods=["POST"])
@login_required
@admin_required
def resolve_condition(assignment_id):
    decision = request.form["decision"]
    conn = db()
    assignment = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
    if not assignment or not assignment["mismatch_pending"]:
        flash("Nothing pending for that assignment.", "error")
        conn.close()
        return redirect(request.referrer or url_for("dashboard"))

    book = conn.execute("SELECT * FROM books WHERE id=?", (assignment["book_id"],)).fetchone()

    # Same 2-degrees-worse gate applies when resolving a pending mismatch
    # from the dashboard - "replace" is only accepted if the return really
    # was 2+ degrees worse than the issue condition.
    degrees = condition_degrees_worse(assignment["issue_condition"], assignment["return_condition"])
    replace_allowed = degrees >= 2

    if decision == "no_replacement":
        conn.execute("""
            UPDATE books SET status='Available', condition_status=? WHERE id=?
        """, (assignment["return_condition"], book["id"]))
        conn.execute("UPDATE assignments SET mismatch_pending=0 WHERE id=?", (assignment_id,))
        conn.commit()
        flash("Marked as no replacement needed. Book is available again.", "success")
    elif decision == "replace" and replace_allowed:
        amount = float(request.form.get("amount") or 0)
        conn.execute("""
            INSERT INTO fees(student_id, assignment_id, book_id, description, amount, fee_type)
            VALUES(?,?,?,?,?, 'replacement')
        """, (
            assignment["student_id"], assignment_id, book["id"],
            f"Replacement for {book['title']} ({book['brn']})", amount
        ))
        conn.execute("UPDATE books SET status='Awaiting Replacement' WHERE id=?", (book["id"],))
        conn.execute("UPDATE assignments SET mismatch_pending=0 WHERE id=?", (assignment_id,))
        conn.commit()
        flash("Replacement fee added. Student is blocked from new assignments until it is paid.", "success")
    elif decision == "replace" and not replace_allowed:
        flash("Replacement is not applicable: the book was not returned in a worse condition by 2 or more degrees.", "error")
    else:
        flash("Invalid decision.", "error")
    conn.close()
    return redirect(request.referrer or url_for("dashboard"))

@app.route("/history")
@login_required
def history():
    brn = request.args.get("brn", "").strip()
    student = request.args.get("student", "").strip()
    conn = db()
    query = """
        SELECT a.*, b.brn,b.title,b.subject,b.grade_level,
               s.student_number,s.first_name,s.last_name,s.class_name,
               u.full_name AS attendant
        FROM assignments a
        JOIN books b ON b.id=a.book_id
        JOIN students s ON s.id=a.student_id
        JOIN attendants u ON u.id=a.attendant_id
        WHERE 1=1
    """
    params=[]
    if brn:
        query += " AND b.brn LIKE ?"; params.append(f"%{brn}%")
    if student:
        query += """ AND (s.student_number LIKE ? OR s.first_name LIKE ? OR s.last_name LIKE ?)"""
        params += [f"%{student}%"] * 3
    query += " ORDER BY a.issued_at DESC"
    rows=conn.execute(query,params).fetchall()
    conn.close()
    return render_template("history.html", assignments=rows, conditions=CONDITIONS)

@app.route("/book/<brn>")
@login_required
def book_detail(brn):
    conn = db()
    books_with_brn = conn.execute(
        "SELECT * FROM books WHERE brn=? ORDER BY grade_level, subject, title",
        (brn,)
    ).fetchall()

    if not books_with_brn:
        conn.close()
        flash("Book not found.", "error")
        return redirect(url_for("books"))

    # A BRN is no longer globally unique. If it identifies more than one
    # book, do not guess which record the user meant. Show the matching books.
    if len(books_with_brn) > 1:
        conn.close()
        return render_template(
            "books.html",
            books=books_with_brn,
            conditions=CONDITIONS,
            prefill_brn=brn,
            brn_matches=books_with_brn,
        )

    book = books_with_brn[0]
    history_rows = conn.execute("""
        SELECT a.*,s.student_number,s.first_name,s.last_name,s.class_name,
               u.full_name AS attendant
        FROM assignments a
        JOIN students s ON s.id=a.student_id
        JOIN attendants u ON u.id=a.attendant_id
        WHERE a.book_id=? ORDER BY a.issued_at DESC
    """, (book["id"],)).fetchall()
    conn.close()
    return render_template("book_detail.html", book=book, history=history_rows, conditions=CONDITIONS)

@app.route("/book/id/<int:book_id>")
@login_required
def book_detail_by_id(book_id):
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        flash("Book not found.", "error")
        return redirect(url_for("books"))
    history_rows = conn.execute("""
        SELECT a.*,s.student_number,s.first_name,s.last_name,s.class_name,
               u.full_name AS attendant
        FROM assignments a
        JOIN students s ON s.id=a.student_id
        JOIN attendants u ON u.id=a.attendant_id
        WHERE a.book_id=? ORDER BY a.issued_at DESC
    """, (book_id,)).fetchall()
    conn.close()
    return render_template("book_detail.html", book=book, history=history_rows, conditions=CONDITIONS)

@app.route("/book/id/<int:book_id>/edit", methods=["GET", "POST"])
@login_required
def edit_book_by_id(book_id):
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        flash("Book not found.", "error")
        return redirect(url_for("books"))

    if request.method == "POST":
        try:
            conn.execute("""
                UPDATE books SET brn=?, title=?, subject=?, grade_level=?, condition_status=?, status=?, cross_grade=?
                WHERE id=?
            """, (
                request.form["brn"].strip(),
                request.form["title"].strip(),
                request.form["subject"].strip(),
                normalize_grade_level(int(request.form["grade_level"])),
                request.form["condition_status"],
                request.form["status"],
                1 if request.form.get("cross_grade") else 0,
                book_id
            ))
            conn.commit()
            flash("Book updated.", "success")
            conn.close()
            return redirect(url_for("book_detail_by_id", book_id=book_id))
        except sqlite3.IntegrityError:
            flash("A book with this BRN, title, subject, and grade level already exists.", "error")

    conn.close()
    return render_template("edit_book.html", book=book, conditions=CONDITIONS,
                           statuses=BOOK_STATUSES)

@app.route("/book/<brn>/edit", methods=["GET", "POST"])
@login_required
def edit_book(brn):
    conn = db()
    matches = conn.execute(
        "SELECT * FROM books WHERE brn=? ORDER BY grade_level, subject, title",
        (brn,)
    ).fetchall()
    conn.close()
    if not matches:
        flash("Book not found.", "error")
        return redirect(url_for("books"))
    if len(matches) > 1:
        return render_template(
            "books.html", books=matches, conditions=CONDITIONS,
            prefill_brn=brn, brn_matches=matches
        )
    return redirect(url_for("edit_book_by_id", book_id=matches[0]["id"]))

@app.route("/book/id/<int:book_id>/delete", methods=["POST"])
@login_required
def delete_book_by_id(book_id):
    conn = db()
    book = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not book:
        flash("Book not found.", "error")
        conn.close()
        return redirect(url_for("books"))
    try:
        conn.execute("DELETE FROM books WHERE id=?", (book_id,))
        conn.commit()
        flash(f"{book['title']} ({book['brn']}) deleted.", "success")
        conn.close()
        return redirect(url_for("books"))
    except sqlite3.IntegrityError:
        conn.close()
        flash("Cannot delete this book: it has assignment history. Consider marking it Lost instead.", "error")
        return redirect(url_for("book_detail_by_id", book_id=book_id))

@app.route("/book/<brn>/delete", methods=["POST"])
@login_required
def delete_book(brn):
    conn = db()
    matches = conn.execute(
        "SELECT * FROM books WHERE brn=? ORDER BY grade_level, subject, title",
        (brn,)
    ).fetchall()
    conn.close()
    if not matches:
        flash("Book not found.", "error")
        return redirect(url_for("books"))
    if len(matches) > 1:
        return render_template(
            "books.html", books=matches, conditions=CONDITIONS,
            prefill_brn=brn, brn_matches=matches
        )
    return delete_book_by_id(matches[0]["id"])

@app.route("/students/<int:student_id>")
@login_required
def student_detail(student_id):
    conn = db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close(); flash("Student not found.", "error"); return redirect(url_for("students"))
    current_books = conn.execute("""
        SELECT a.id AS assignment_id, a.issued_at, b.brn, b.title, b.subject
        FROM assignments a
        JOIN books b ON b.id = a.book_id
        WHERE a.student_id=? AND a.returned_at IS NULL
        ORDER BY a.issued_at
    """, (student_id,)).fetchall()
    fees = conn.execute("""
        SELECT * FROM fees WHERE student_id=? ORDER BY created_at DESC
    """, (student_id,)).fetchall()
    conn.close()
    return render_template("student_detail.html", student=student, current_books=current_books, fees=fees,
                           gender_label=GENDER_LABELS.get(student["gender"], "Not set"))

@app.route("/students/<int:student_id>/edit", methods=["GET", "POST"])
@login_required
def edit_student(student_id):
    conn = db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close(); flash("Student not found.", "error"); return redirect(url_for("students"))
    if request.method == "POST":
        gender, gender_error = _validate_gender(request.form)
        if gender_error:
            flash(gender_error, "error")
        else:
            try:
                conn.execute("""
                    UPDATE students SET student_number=?, first_name=?, last_name=?, grade_level=?, class_name=?, gender=?
                    WHERE id=?
                """, (
                    request.form["student_number"].strip(),
                    request.form["first_name"].strip(),
                    request.form["last_name"].strip(),
                    normalize_grade_level(int(request.form["grade_level"])),
                    request.form["class_name"].strip(),
                    gender,
                    student_id
                ))
                conn.commit()
                flash("Student updated.", "success")
                conn.close()
                return redirect(url_for("student_detail", student_id=student_id))
            except sqlite3.IntegrityError:
                flash("That student number already exists.", "error")
    conn.close()
    return render_template("edit_student.html", student=student, genders=GENDERS)

@app.route("/students/<int:student_id>/delete", methods=["POST"])
@login_required
def delete_student(student_id):
    conn = db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        flash("Student not found.", "error")
        conn.close()
        return redirect(url_for("students"))
    try:
        conn.execute("DELETE FROM fees WHERE student_id=?", (student_id,))
        conn.execute("DELETE FROM students WHERE id=?", (student_id,))
        conn.commit()
        flash(f"{student['first_name']} {student['last_name']} deleted.", "success")
        conn.close()
        return redirect(url_for("students"))
    except sqlite3.IntegrityError:
        conn.close()
        flash("Cannot delete this student: they have assignment history.", "error")
        return redirect(url_for("student_detail", student_id=student_id))

@app.route("/fees/<int:fee_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_fee(fee_id):
    conn = db()
    conn.execute("DELETE FROM fees WHERE id=?", (fee_id,))
    conn.commit()
    conn.close()
    flash("Fee deleted.", "success")
    return redirect(request.referrer or url_for("dashboard"))

@app.route("/inventory")
@login_required
def inventory():
    conn = db()
    by_title = conn.execute("""
        SELECT title, subject, grade_level,
               COUNT(*) AS total,
               SUM(CASE WHEN status='Available' THEN 1 ELSE 0 END) AS available,
               SUM(CASE WHEN status='Issued' THEN 1 ELSE 0 END) AS issued,
               SUM(CASE WHEN status NOT IN ('Available','Issued') THEN 1 ELSE 0 END) AS other
        FROM books
        GROUP BY title, subject, grade_level
        ORDER BY subject, grade_level, title
    """).fetchall()
    by_subject = conn.execute("""
        SELECT subject,
               COUNT(*) AS total,
               SUM(CASE WHEN status='Available' THEN 1 ELSE 0 END) AS available,
               SUM(CASE WHEN status='Issued' THEN 1 ELSE 0 END) AS issued,
               SUM(CASE WHEN status NOT IN ('Available','Issued') THEN 1 ELSE 0 END) AS other
        FROM books
        GROUP BY subject
        ORDER BY subject
    """).fetchall()
    totals = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='Available' THEN 1 ELSE 0 END) AS available,
               SUM(CASE WHEN status='Issued' THEN 1 ELSE 0 END) AS issued,
               SUM(CASE WHEN status NOT IN ('Available','Issued') THEN 1 ELSE 0 END) AS other
        FROM books
    """).fetchone()
    conn.close()
    return render_template("inventory.html", by_title=by_title, by_subject=by_subject, totals=totals)


@app.route("/students/<int:student_id>/assign", methods=["GET", "POST"])
@login_required
def assign(student_id):
    conn = db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close(); flash("Student not found.", "error"); return redirect(url_for("students"))

    block_reason = student_block_reason(conn, student_id)
    if block_reason:
        conn.close()
        flash(f"Cannot assign books: {block_reason}", "error")
        return redirect(url_for("student_detail", student_id=student_id))

    error = None
    added_book = None
    prefill_brn = request.args.get("brn", "").strip()
    register_brn = ""
    brn_matches = []

    browse_grade_raw = request.values.get("browse_grade", "").strip()
    browse_subject = request.values.get("browse_subject", "").strip()
    browse_title = request.values.get("browse_title", "").strip()
    # Status filter for the browse panel. Defaults to "Available" so the
    # page's out-of-the-box behavior is unchanged; pick "All" (or a specific
    # status like "Issued") to also see/filter unavailable books.
    browse_status = request.values.get("browse_status", "").strip() or "Available"

    if browse_grade_raw:
        try:
            browse_grade = normalize_grade_level(int(browse_grade_raw))
        except ValueError:
            browse_grade = student["grade_level"]
    else:
        browse_grade = student["grade_level"]

    if request.method == "POST":
        brn = request.form.get("brn", "").strip()
        selected_book_id = request.form.get("book_id", "").strip()
        prefill_brn = brn

        if selected_book_id:
            try:
                selected_book_id = int(selected_book_id)
            except ValueError:
                selected_book_id = None

        if selected_book_id:
            book = conn.execute(
                "SELECT * FROM books WHERE id=? AND brn=?",
                (selected_book_id, brn)
            ).fetchone()
            if not book:
                error = "The selected book is no longer available. Please search the BRN again."
        else:
            brn_match_rows = conn.execute(
                "SELECT * FROM books WHERE brn=? ORDER BY grade_level, subject, title",
                (brn,)
            ).fetchall()
            if not brn_match_rows:
                error = f"BRN '{brn}' is not currently registered."
                register_brn = brn
                book = None
            elif len(brn_match_rows) > 1:
                # Never guess when one BRN identifies multiple books. Let the
                # attendant choose the exact title/subject/grade combination.
                # grade_ok reflects grades_compatible() (exact match, 4th/5th
                # form sharing, and flagged 7-9 cross-grade books) rather than
                # a raw grade_level equality check, so the choices offered
                # here match what would actually be allowed if attempted.
                brn_matches = []
                for row in brn_match_rows:
                    row_dict = dict(row)
                    row_dict["grade_ok"] = grades_compatible(row, student["grade_level"])
                    brn_matches.append(row_dict)
                error = (
                    f"BRN '{brn}' is attached to multiple books. "
                    "Select the correct book below before assigning it."
                )
                book = None
            else:
                book = brn_match_rows[0]

        if book is not None and error is None:
            if book["status"] != "Available":
                error = f"{book['title']} ({book['brn']}) is not available for assignment."
            elif not grades_compatible(book, student["grade_level"]):
                error = (
                    f"Grade mismatch: '{book['title']}' is for Grade {book['grade_level']}, "
                    f"student is Grade {student['grade_level']}."
                )
            else:
                duplicate_title = conn.execute("""
                    SELECT b.brn, b.title, b.subject, b.grade_level
                    FROM assignments a
                    JOIN books b ON b.id = a.book_id
                    WHERE a.student_id=?
                      AND a.returned_at IS NULL
                      AND LOWER(TRIM(b.title)) = LOWER(TRIM(?))
                    LIMIT 1
                """, (student["id"], book["title"])).fetchone()

                if duplicate_title:
                    error = (
                        f"Cannot assign '{book['title']}'. This student already has a copy "
                        f"of this book title (BRN: {duplicate_title['brn']})."
                    )
                else:
                    conn.execute("""
                        INSERT INTO assignments(book_id, student_id, attendant_id, issue_condition)
                        VALUES(?,?,?,?)
                    """, (book["id"], student["id"], session["attendant_id"], book["condition_status"]))
                    conn.execute("UPDATE books SET status='Issued' WHERE id=?", (book["id"],))
                    conn.commit()
                    added_book = f"{book['title']} ({book['brn']})"
                    brn_matches = []

    current_books = conn.execute("""
        SELECT a.id AS assignment_id, a.issued_at, b.brn, b.title, b.subject
        FROM assignments a
        JOIN books b ON b.id = a.book_id
        WHERE a.student_id=? AND a.returned_at IS NULL
        ORDER BY a.issued_at
    """, (student_id,)).fetchall()

    # Browse/filter panel: lets staff find a book by grade (defaulting to the
    # student's own grade and honoring cross-grade eligibility the same way
    # grades_compatible() does for actual assignment), subject, title, and
    # status - instead of requiring a known BRN up front. Status defaults to
    # "Available" (matching prior behavior); switching it to "All" or to a
    # specific status (e.g. "Issued") lets staff see why a book isn't
    # assignable without leaving this page.
    current_titles = {(row["title"] or "").strip().lower() for row in current_books}
    browse_query = "SELECT * FROM books WHERE 1=1"
    browse_params = []
    if browse_status != "All":
        browse_query += " AND status=?"; browse_params.append(browse_status)
    if browse_subject:
        browse_query += " AND subject LIKE ?"; browse_params.append(f"%{browse_subject}%")
    if browse_title:
        browse_query += " AND title LIKE ?"; browse_params.append(f"%{browse_title}%")
    browse_query += " ORDER BY subject, title"
    browse_candidates = conn.execute(browse_query, browse_params).fetchall()
    browsable_books = [
        b for b in browse_candidates
        if grades_compatible(b, browse_grade)
        and (b["title"] or "").strip().lower() not in current_titles
    ]

    # These two feed the informational "contribution fee unpaid" banner on
    # the assign page. Note that if the fee is unpaid AND the override is
    # off, student_block_reason() above already redirected away before this
    # point - so by the time we get here, either the fee is paid, or the
    # override is on (and this banner explains why the page let them in).
    contribution_unpaid = not student["contribution_paid"]
    global_contribution_override = contribution_override_enabled(conn)

    conn.close()
    return render_template(
        "assign.html",
        student=student,
        current_books=current_books,
        error=error,
        added_book=added_book,
        prefill_brn=prefill_brn,
        register_brn=register_brn,
        brn_matches=brn_matches,
        browsable_books=browsable_books,
        browse_grade=browse_grade,
        browse_grade_label=form_label(browse_grade),
        browse_subject=browse_subject,
        browse_title=browse_title,
        browse_status=browse_status,
        browse_statuses=BOOK_STATUSES,
        form_labels_by_grade=[(g, form_label(g)) for g in range(7, 13)],
        contribution_unpaid=contribution_unpaid,
        global_contribution_override=global_contribution_override,
    )


# --- Admin-only: creating/deactivating logins.

@app.route("/users", methods=["GET", "POST"])
@login_required
@admin_required
def users():
    conn = db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        role = request.form.get("role", "staff").strip()
        if role not in ROLES:
            role = "staff"
        if not username or not password or not full_name:
            flash("Username, full name, and password are all required.", "error")
        else:
            try:
                conn.execute(
                    "INSERT INTO attendants(username,password_hash,full_name,role) VALUES(?,?,?,?)",
                    (username, generate_password_hash(password), full_name, role)
                )
                conn.commit()
                flash(f"Login '{username}' created.", "success")
            except sqlite3.IntegrityError:
                flash("That username already exists.", "error")
    rows = conn.execute("SELECT * FROM attendants ORDER BY active DESC, username").fetchall()
    conn.close()
    return render_template("users.html", users=rows, roles=ROLES)

@app.route("/users/<int:attendant_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def reset_user_password(attendant_id):
    """Allow an admin to set a new password for any login, including their own."""
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if len(new_password) < 8:
        flash("New password must be at least 8 characters long.", "error")
        return redirect(url_for("users"))

    if new_password != confirm_password:
        flash("The new passwords do not match.", "error")
        return redirect(url_for("users"))

    conn = db()
    user = conn.execute(
        "SELECT username FROM attendants WHERE id=?", (attendant_id,)
    ).fetchone()
    if not user:
        conn.close()
        flash("That user could not be found.", "error")
        return redirect(url_for("users"))

    conn.execute(
        "UPDATE attendants SET password_hash=? WHERE id=?",
        (generate_password_hash(new_password), attendant_id)
    )
    conn.commit()
    conn.close()
    flash(f"Password reset for '{user['username']}'.", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:attendant_id>/toggle-active", methods=["POST"])
@login_required
@admin_required
def toggle_user_active(attendant_id):
    conn = db()
    if attendant_id == session.get("attendant_id"):
        flash("You can't deactivate the login you're currently signed in with.", "error")
    else:
        conn.execute("""
            UPDATE attendants
            SET active = CASE active WHEN 1 THEN 0 ELSE 1 END
            WHERE id=?
        """, (attendant_id,))
        conn.commit()
        flash("Login updated.", "success")
    conn.close()
    return redirect(url_for("users"))


if __name__ == "__main__":
    init_db()
    init_library_db()

    # The packaged Windows application keeps automatic backups running in the
    # background for as long as the application is open.
    if getattr(sys, "frozen", False) or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_automatic_backup_worker()
        license_check.start_background_worker()

    if getattr(sys, "frozen", False):
        # Packaged .exe: Flask's own dev server (app.run(debug=True)) is
        # not safe to ship - its auto-reloader re-spawns the process via
        # sys.executable, which under a frozen build just launches a second
        # copy of the whole app rather than reloading a script. Waitress is
        # a small, pure-Python production WSGI server with no reloader and
        # no such issue. The browser is opened automatically on a short
        # delay (giving the server a moment to start listening) since a
        # double-clicked .exe has no console for a librarian to read a
        # "Running on http://..." message from.
        from waitress import serve
        host, port = "127.0.0.1", 5000
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
        serve(app, host=host, port=port)
    else:
        app.run(debug=True)
