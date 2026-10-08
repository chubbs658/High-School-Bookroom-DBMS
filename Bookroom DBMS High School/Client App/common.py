"""
Shared internals used by both the bookroom (app.py) and the library
(library.py) blueprints: the DB connection, a couple of small schema
helpers, the login/role decorators, the student lookup used by both
sections, and the shared "what time is it" helper used by the admin
date/time override in Settings.
"""

import sys
from pathlib import Path
import sqlite3
from datetime import datetime
from functools import wraps
from flask import session, redirect, url_for, flash


def _persistent_app_dir():
    """Folder used for anything the app must write to and expects to still
    be there on the next launch: the database, the backups/ folder, the
    generated secret key file.

    When running as a plain Python script this is just the folder this
    file lives in. When running as a PyInstaller-built .exe, __file__
    instead resolves *inside* the temporary folder PyInstaller extracts
    bundled resources into - which, in --onefile mode, is recreated fresh
    and then deleted again on every single launch. Writing the database
    there would silently wipe all data on every restart, so persistent
    data must live next to the actual .exe on disk instead, which is what
    sys.executable points at once the app is frozen.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _persistent_app_dir()
DB = BASE_DIR / "bookroom.db"


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn, table, column, decl):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "attendant_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    """Same login check as login_required, plus a role check. Routes that
    only the master admin should reach (changing due dates/fee amounts,
    marking/deleting a fee, resolving a replacement decision, toggling the
    contribution-fee override, the date/time override, backup/restore, and
    managing logins) use this decorator. It is self-contained - it checks
    session["attendant_id"] itself - so it works whether or not it's
    stacked underneath @login_required.

    Staff who hit one of these routes directly (not just via a hidden nav
    link/button) are redirected home with a flash message rather than
    getting a raw 403, which is friendlier and still fully enforces the
    restriction, since hiding a button in a template is only ever a UX
    nicety - the real boundary has to live here, at the route.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "attendant_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("You don't have permission to do that. Ask an administrator.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapper


def find_students(conn, q):
    """Look up students by exact student number first; if that misses, fall back to a
    partial match across student number, first name, and last name."""
    q = (q or "").strip()
    if not q:
        return []
    exact = conn.execute("SELECT * FROM students WHERE student_number=?", (q,)).fetchall()
    if exact:
        return exact
    like = f"%{q}%"
    return conn.execute("""
        SELECT * FROM students
        WHERE student_number LIKE ? OR first_name LIKE ? OR last_name LIKE ?
        ORDER BY last_name, first_name
    """, (like, like, like)).fetchall()


def _get_setting_value(conn, key, default=None):
    """Internal helper: reads a single value straight from the shared
    settings table, without depending on app.py's own get_setting()
    (importing that here would create a circular import, since app.py
    already imports from this module - same reason library.py has its own
    small _contribution_override_enabled() instead of importing app.py's)."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def app_now(conn):
    """The datetime the whole app should treat as 'now'.

    Normally this is just the real system clock. If an admin has turned on
    the date/time override in Settings, the stored override value is used
    instead, so every part of the app that needs "today" (bookroom due
    dates, late fees, report defaults, and any date logic added later)
    sees one consistent value rather than each call site reading the real
    clock independently.

    Falls back to the real system clock whenever the override is off, or
    the stored value is missing or unparseable, so a bad or cleared
    setting can never silently freeze the app on a stale date.
    """
    if _get_setting_value(conn, "time_override_enabled", "0") == "1":
        raw = _get_setting_value(conn, "time_override_value")
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                pass
    return datetime.now()


def app_today(conn):
    """Date-only convenience wrapper around app_now(), for the call sites
    that only ever compared against date.today()."""
    return app_now(conn).date()
