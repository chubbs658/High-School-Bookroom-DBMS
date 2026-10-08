# School Bookroom DBMS

A complete starter web DBMS for a high-school book rental/bookroom system.

## Stack
- Python 3
- Flask
- SQLite (zero setup; easy to move to MySQL later)
- HTML/CSS/vanilla JavaScript

## Features
- Book registration with BRN, subject, intended grade and condition
- Student registration with grade/class
- Contribution-fee paid/unpaid flag
- Book issue/tagging by BRN and student number
- Grade-level validation before issue
- Rental/assignment history
- Search/filter books and students
- Book condition tracking
- Book return workflow
- Simple attendant login/logout
- Dashboard statistics

## Run in VS Code

1. Open this folder in VS Code.
2. Create/activate a virtual environment:
   - Windows: `python -m venv .venv`
   - Windows PowerShell: `.venv\Scripts\Activate.ps1`
3. Install dependencies:
   `pip install -r requirements.txt`
4. Start:
   `python app.py`
5. Open `http://127.0.0.1:5000`

Default demo login:
- Username: `admin`
- Password: `admin123`

Change the demo password before real school use.

## Important
This is a functional starter system. For production deployment, use a stronger authentication setup, HTTPS, backups, CSRF protection, role permissions, and preferably PostgreSQL/MySQL.
