"""
Library blueprint.

Mirrors the bookroom's book / assignment / fee model, with the differences
agreed for the library section specifically:

  - "type" (Fiction, Non-Fiction, Reference, ...) replaces "subject",
    stored the same way subject is on bookroom books: a plain text field,
    not a separate lookup table.
  - No grade_level on library books at all, so there is no cross-grade
    logic here - library books are eligible to every student equally.
  - A student may have several books issued in the same sitting (grouped
    by a batch_id for display), but must return EVERY unreturned library
    book before anything new can be issued to them. This is an immediate
    block, unlike the bookroom's rule, which only kicks in after the
    school year end date.
  - Only replacement fees exist here (no late fees, since there's no
    due-date concept for library loans).
  - An unpaid replacement fee also blocks new issues, same pattern as
    bookroom. An unpaid school contribution fee (shared student flag)
    blocks both sections, since it isn't book-specific - unless the
    admin-controlled contribution-fee override is turned on, same as
    on the bookroom side.
"""

import sqlite3
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from common import db, login_required, admin_required, find_students

library_bp = Blueprint("library", __name__, url_prefix="/library")

LIBRARY_STATUSES = ["Available", "Issued", "Lost"]


def init_library_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS library_books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        brn TEXT NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Available',
        replacement_fee REAL NOT NULL DEFAULT 0,
        UNIQUE(brn, title, type)
    );

    CREATE TABLE IF NOT EXISTS library_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        attendant_id INTEGER NOT NULL,
        batch_id TEXT NOT NULL,
        issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        returned_at TEXT,
        status TEXT NOT NULL DEFAULT 'Issued',
        FOREIGN KEY(book_id) REFERENCES library_books(id),
        FOREIGN KEY(student_id) REFERENCES students(id),
        FOREIGN KEY(attendant_id) REFERENCES attendants(id)
    );

    CREATE TABLE IF NOT EXISTS library_fees (
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
        FOREIGN KEY(assignment_id) REFERENCES library_assignments(id),
        FOREIGN KEY(book_id) REFERENCES library_books(id)
    );
    """)
    conn.commit()
    conn.close()


def _contribution_override_enabled(conn):
    """Reads the same 'contribution_override_enabled' setting the bookroom
    side (app.py's get_setting) reads/writes, without importing app.py
    (which would create a circular import with library.py)."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key='contribution_override_enabled'"
    ).fetchone()
    return bool(row and row["value"] == "1")


def library_block_reason(conn, student_id):
    """Same shape as the bookroom's student_block_reason(), but library-specific:
    any unpaid library fee, or any unreturned library book, blocks new issues -
    immediately, with no year-end grace period. The unpaid-contribution check
    is skipped while the admin-controlled contribution-fee override is on,
    matching the bookroom side."""
    student = conn.execute("SELECT contribution_paid FROM students WHERE id=?", (student_id,)).fetchone()
    if not student["contribution_paid"] and not _contribution_override_enabled(conn):
        return "This student's contribution fee has not been paid."
    unpaid = conn.execute(
        "SELECT COUNT(*) FROM library_fees WHERE student_id=? AND paid=0", (student_id,)
    ).fetchone()[0]
    if unpaid:
        return "This student has an outstanding library fee that must be paid before more books can be issued."
    outstanding_books = conn.execute(
        "SELECT COUNT(*) FROM library_assignments WHERE student_id=? AND status='Issued'", (student_id,)
    ).fetchone()[0]
    if outstanding_books:
        return "This student has library book(s) still out. All must be returned before new books can be issued."
    return None


def fetch_library_outstanding_fees(conn, type_=None):
    query = """
        SELECT f.id AS fee_id, f.description, f.amount, f.created_at,
               s.id AS student_id, s.student_number, s.first_name, s.last_name,
               s.grade_level, s.class_name,
               b.brn, b.title, b.type
        FROM library_fees f
        JOIN students s ON s.id = f.student_id
        LEFT JOIN library_books b ON b.id = f.book_id
        WHERE f.paid = 0
    """
    params = []
    if type_:
        query += " AND b.type LIKE ?"
        params.append(f"%{type_}%")
    query += " ORDER BY s.last_name, s.first_name"
    return conn.execute(query, params).fetchall()


@library_bp.route("/dashboard")
@login_required
def dashboard():
    conn = db()
    total_books = conn.execute("SELECT COUNT(*) FROM library_books").fetchone()[0]
    available = conn.execute("SELECT COUNT(*) FROM library_books WHERE status='Available'").fetchone()[0]
    issued = conn.execute("SELECT COUNT(*) FROM library_books WHERE status='Issued'").fetchone()[0]
    lost = conn.execute("SELECT COUNT(*) FROM library_books WHERE status='Lost'").fetchone()[0]
    outstanding_fees = fetch_library_outstanding_fees(conn)
    currently_out = conn.execute("""
        SELECT la.id AS assignment_id, la.issued_at, b.brn, b.title, b.type,
               s.student_number, s.first_name, s.last_name
        FROM library_assignments la
        JOIN library_books b ON b.id = la.book_id
        JOIN students s ON s.id = la.student_id
        WHERE la.status='Issued'
        ORDER BY la.issued_at
    """).fetchall()
    conn.close()
    return render_template(
        "library_dashboard.html",
        total_books=total_books, available=available, issued=issued, lost=lost,
        outstanding_fees=outstanding_fees, currently_out=currently_out,
    )


@library_bp.route("/books", methods=["GET", "POST"])
@login_required
def books():
    conn = db()
    if request.method == "POST":
        try:
            conn.execute("""
                INSERT INTO library_books(brn, title, author, type, replacement_fee)
                VALUES(?,?,?,?,?)
            """, (
                request.form["brn"].strip(),
                request.form["title"].strip(),
                request.form.get("author", "").strip(),
                request.form["type"].strip(),
                float(request.form.get("replacement_fee") or 0),
            ))
            conn.commit()
            flash("Book registered successfully.", "success")
        except sqlite3.IntegrityError:
            flash("A book with this BRN, title, and type already exists.", "error")

    q = request.args.get("q", "").strip()
    prefill_brn = request.args.get("brn", "").strip()
    type_ = request.args.get("type", "").strip()
    status = request.args.get("status", "").strip()
    query = "SELECT * FROM library_books WHERE 1=1"
    params = []
    if q:
        query += " AND (brn LIKE ? OR title LIKE ? OR author LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if type_:
        query += " AND type LIKE ?"
        params.append(f"%{type_}%")
    if status:
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY type, title"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template(
        "library_books.html", books=rows, statuses=LIBRARY_STATUSES, prefill_brn=prefill_brn
    )


@library_bp.route("/book/<brn>")
@login_required
def book_detail(brn):
    conn = db()
    matches = conn.execute(
        "SELECT * FROM library_books WHERE brn=? ORDER BY type, title", (brn,)
    ).fetchall()
    if not matches:
        conn.close()
        flash("Book not found.", "error")
        return redirect(url_for("library.books"))
    if len(matches) > 1:
        conn.close()
        return render_template(
            "library_books.html", books=matches, statuses=LIBRARY_STATUSES,
            prefill_brn=brn, brn_matches=matches,
        )
    book = matches[0]
    history_rows = conn.execute("""
        SELECT la.*, s.student_number, s.first_name, s.last_name, s.class_name,
               u.full_name AS attendant
        FROM library_assignments la
        JOIN students s ON s.id = la.student_id
        JOIN attendants u ON u.id = la.attendant_id
        WHERE la.book_id=? ORDER BY la.issued_at DESC
    """, (book["id"],)).fetchall()
    conn.close()
    return render_template("library_book_detail.html", book=book, history=history_rows)


@library_bp.route("/book/id/<int:book_id>")
@login_required
def book_detail_by_id(book_id):
    conn = db()
    book = conn.execute("SELECT * FROM library_books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        flash("Book not found.", "error")
        return redirect(url_for("library.books"))
    history_rows = conn.execute("""
        SELECT la.*, s.student_number, s.first_name, s.last_name, s.class_name,
               u.full_name AS attendant
        FROM library_assignments la
        JOIN students s ON s.id = la.student_id
        JOIN attendants u ON u.id = la.attendant_id
        WHERE la.book_id=? ORDER BY la.issued_at DESC
    """, (book_id,)).fetchall()
    conn.close()
    return render_template("library_book_detail.html", book=book, history=history_rows)


@library_bp.route("/book/id/<int:book_id>/edit", methods=["GET", "POST"])
@login_required
def edit_book_by_id(book_id):
    conn = db()
    book = conn.execute("SELECT * FROM library_books WHERE id=?", (book_id,)).fetchone()
    if not book:
        conn.close()
        flash("Book not found.", "error")
        return redirect(url_for("library.books"))

    if request.method == "POST":
        try:
            conn.execute("""
                UPDATE library_books SET brn=?, title=?, author=?, type=?, status=?, replacement_fee=?
                WHERE id=?
            """, (
                request.form["brn"].strip(),
                request.form["title"].strip(),
                request.form.get("author", "").strip(),
                request.form["type"].strip(),
                request.form["status"],
                float(request.form.get("replacement_fee") or 0),
                book_id,
            ))
            conn.commit()
            flash("Book updated.", "success")
            conn.close()
            return redirect(url_for("library.book_detail_by_id", book_id=book_id))
        except sqlite3.IntegrityError:
            flash("A book with this BRN, title, and type already exists.", "error")

    conn.close()
    return render_template("library_edit_book.html", book=book, statuses=LIBRARY_STATUSES)


@library_bp.route("/book/<brn>/edit", methods=["GET", "POST"])
@login_required
def edit_book(brn):
    conn = db()
    matches = conn.execute(
        "SELECT * FROM library_books WHERE brn=? ORDER BY type, title", (brn,)
    ).fetchall()
    conn.close()
    if not matches:
        flash("Book not found.", "error")
        return redirect(url_for("library.books"))
    if len(matches) > 1:
        return render_template(
            "library_books.html", books=matches, statuses=LIBRARY_STATUSES,
            prefill_brn=brn, brn_matches=matches,
        )
    return redirect(url_for("library.edit_book_by_id", book_id=matches[0]["id"]))


@library_bp.route("/book/id/<int:book_id>/delete", methods=["POST"])
@login_required
def delete_book_by_id(book_id):
    conn = db()
    book = conn.execute("SELECT * FROM library_books WHERE id=?", (book_id,)).fetchone()
    if not book:
        flash("Book not found.", "error")
        conn.close()
        return redirect(url_for("library.books"))
    try:
        conn.execute("DELETE FROM library_books WHERE id=?", (book_id,))
        conn.commit()
        flash(f"{book['title']} ({book['brn']}) deleted.", "success")
        conn.close()
        return redirect(url_for("library.books"))
    except sqlite3.IntegrityError:
        conn.close()
        flash("Cannot delete this book: it has issue history. Consider marking it Lost instead.", "error")
        return redirect(url_for("library.book_detail_by_id", book_id=book_id))


@library_bp.route("/book/<brn>/delete", methods=["POST"])
@login_required
def delete_book(brn):
    conn = db()
    matches = conn.execute(
        "SELECT * FROM library_books WHERE brn=? ORDER BY type, title", (brn,)
    ).fetchall()
    conn.close()
    if not matches:
        flash("Book not found.", "error")
        return redirect(url_for("library.books"))
    if len(matches) > 1:
        return render_template(
            "library_books.html", books=matches, statuses=LIBRARY_STATUSES,
            prefill_brn=brn, brn_matches=matches,
        )
    return delete_book_by_id(matches[0]["id"])


@library_bp.route("/issue-books")
@login_required
def issue_books_page():
    student_number = request.args.get("student_number", "").strip()
    student = None
    matches = []
    block_reason = None
    conn = db()
    if student_number:
        results = find_students(conn, student_number)
        if len(results) == 1:
            student = results[0]
            block_reason = library_block_reason(conn, student["id"])
        else:
            matches = results
    conn.close()
    return render_template(
        "library_issue.html",
        student_number=student_number, student=student, matches=matches,
        block_reason=block_reason,
    )


@library_bp.route("/students/<int:student_id>/issue", methods=["GET", "POST"])
@login_required
def issue(student_id):
    conn = db()
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        conn.close()
        flash("Student not found.", "error")
        return redirect(url_for("students"))

    block_reason = library_block_reason(conn, student_id)
    if block_reason:
        conn.close()
        flash(f"Cannot issue books: {block_reason}", "error")
        return redirect(url_for("library.dashboard"))

    error = None
    type_filter = request.values.get("type", "").strip()
    q = request.values.get("q", "").strip()

    if request.method == "POST":
        book_ids = request.form.getlist("book_ids")
        if not book_ids:
            error = "Select at least one book to issue."
        else:
            batch_id = uuid.uuid4().hex[:12]
            issued_titles = []
            skipped = False
            for raw_id in book_ids:
                try:
                    book_id = int(raw_id)
                except ValueError:
                    continue
                book = conn.execute(
                    "SELECT * FROM library_books WHERE id=? AND status='Available'", (book_id,)
                ).fetchone()
                if not book:
                    skipped = True
                    continue
                conn.execute("""
                    INSERT INTO library_assignments(book_id, student_id, attendant_id, batch_id)
                    VALUES(?,?,?,?)
                """, (book["id"], student_id, session["attendant_id"], batch_id))
                conn.execute("UPDATE library_books SET status='Issued' WHERE id=?", (book["id"],))
                issued_titles.append(f"{book['title']} ({book['brn']})")
            conn.commit()
            if skipped:
                error = "Some selected books were no longer available and were skipped."
            if issued_titles:
                flash(f"Issued {len(issued_titles)} book(s): {', '.join(issued_titles)}", "success")

    current_books = conn.execute("""
        SELECT la.id AS assignment_id, la.issued_at, b.brn, b.title, b.type
        FROM library_assignments la
        JOIN library_books b ON b.id = la.book_id
        WHERE la.student_id=? AND la.status='Issued'
        ORDER BY la.issued_at
    """, (student_id,)).fetchall()

    browse_query = "SELECT * FROM library_books WHERE status='Available'"
    browse_params = []
    if type_filter:
        browse_query += " AND type LIKE ?"
        browse_params.append(f"%{type_filter}%")
    if q:
        browse_query += " AND (title LIKE ? OR author LIKE ? OR brn LIKE ?)"
        browse_params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    browse_query += " ORDER BY type, title"
    available_books = conn.execute(browse_query, browse_params).fetchall()

    conn.close()
    return render_template(
        "library_issue.html",
        student=student, current_books=current_books, available_books=available_books,
        error=error, type_filter=type_filter, q=q,
    )


@library_bp.route("/return-books")
@login_required
def return_books_page():
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
                SELECT la.id AS assignment_id, la.issued_at, b.brn, b.title, b.type, b.replacement_fee
                FROM library_assignments la
                JOIN library_books b ON b.id = la.book_id
                WHERE la.student_id=? AND la.status='Issued'
                ORDER BY la.issued_at
            """, (student["id"],)).fetchall()
        else:
            matches = results
    conn.close()
    return render_template(
        "library_return.html",
        student_number=student_number, student=student, matches=matches,
        current_books=current_books,
    )


@library_bp.route("/return/<int:assignment_id>", methods=["POST"])
@login_required
def return_book(assignment_id):
    conn = db()
    assignment = conn.execute("SELECT * FROM library_assignments WHERE id=?", (assignment_id,)).fetchone()
    if not assignment or assignment["status"] != "Issued":
        flash("Assignment not found or already resolved.", "error")
        conn.close()
        return redirect(request.referrer or url_for("library.return_books_page"))

    conn.execute("""
        UPDATE library_assignments SET returned_at=CURRENT_TIMESTAMP, status='Returned' WHERE id=?
    """, (assignment_id,))
    conn.execute("UPDATE library_books SET status='Available' WHERE id=?", (assignment["book_id"],))
    conn.commit()
    conn.close()
    flash("Book returned.", "success")
    return redirect(request.referrer or url_for("library.return_books_page"))


@library_bp.route("/lost/<int:assignment_id>", methods=["POST"])
@login_required
def mark_lost(assignment_id):
    conn = db()
    assignment = conn.execute("SELECT * FROM library_assignments WHERE id=?", (assignment_id,)).fetchone()
    book = None
    if assignment:
        book = conn.execute("SELECT * FROM library_books WHERE id=?", (assignment["book_id"],)).fetchone()
    if not assignment or assignment["status"] != "Issued" or not book:
        flash("Assignment not found or already resolved.", "error")
        conn.close()
        return redirect(request.referrer or url_for("library.return_books_page"))

    amount_raw = request.form.get("amount")
    amount = float(amount_raw) if amount_raw else float(book["replacement_fee"] or 0)

    conn.execute("""
        UPDATE library_assignments SET returned_at=CURRENT_TIMESTAMP, status='Lost' WHERE id=?
    """, (assignment_id,))
    conn.execute("UPDATE library_books SET status='Lost' WHERE id=?", (book["id"],))
    conn.execute("""
        INSERT INTO library_fees(student_id, assignment_id, book_id, description, amount)
        VALUES(?,?,?,?,?)
    """, (
        assignment["student_id"], assignment_id, book["id"],
        f"Replacement for lost book: {book['title']} ({book['brn']})", amount,
    ))
    conn.commit()
    conn.close()
    flash(
        "Book marked as lost. A replacement fee has been charged; the loan is closed "
        "so the student can issue other library books once the fee is paid.", "error"
    )
    return redirect(request.referrer or url_for("library.return_books_page"))


@library_bp.route("/history")
@login_required
def history():
    brn = request.args.get("brn", "").strip()
    student = request.args.get("student", "").strip()
    conn = db()
    query = """
        SELECT la.*, b.brn, b.title, b.type,
               s.student_number, s.first_name, s.last_name, s.class_name,
               u.full_name AS attendant
        FROM library_assignments la
        JOIN library_books b ON b.id = la.book_id
        JOIN students s ON s.id = la.student_id
        JOIN attendants u ON u.id = la.attendant_id
        WHERE 1=1
    """
    params = []
    if brn:
        query += " AND b.brn LIKE ?"
        params.append(f"%{brn}%")
    if student:
        query += " AND (s.student_number LIKE ? OR s.first_name LIKE ? OR s.last_name LIKE ?)"
        params += [f"%{student}%"] * 3
    query += " ORDER BY la.issued_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("library_history.html", assignments=rows)


@library_bp.route("/reports/fees-outstanding")
@login_required
def report_fees_outstanding():
    type_ = request.args.get("type", "").strip()
    conn = db()
    rows = fetch_library_outstanding_fees(conn, type_ or None)
    conn.close()
    return render_template("library_fees.html", rows=rows, type_=type_)


# --- Admin-only: paying/deleting a fee is a financial action, same as on
# the bookroom side. Anyone can still *see* outstanding fees via the
# dashboard/report above; only resolving one requires the admin role.

@library_bp.route("/fees/<int:fee_id>/pay", methods=["POST"])
@admin_required
def pay_fee(fee_id):
    conn = db()
    conn.execute("UPDATE library_fees SET paid=1, paid_at=CURRENT_TIMESTAMP WHERE id=?", (fee_id,))
    conn.commit()
    conn.close()
    flash("Fee marked as paid.", "success")
    return redirect(request.referrer or url_for("library.dashboard"))


@library_bp.route("/fees/<int:fee_id>/delete", methods=["POST"])
@admin_required
def delete_fee(fee_id):
    conn = db()
    conn.execute("DELETE FROM library_fees WHERE id=?", (fee_id,))
    conn.commit()
    conn.close()
    flash("Fee deleted.", "success")
    return redirect(request.referrer or url_for("library.dashboard"))
