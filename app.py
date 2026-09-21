import csv
import io
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        with env_file.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

from flask import Flask, g, jsonify, request, send_file
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    get_jwt_identity,
    jwt_required,
)
from jobspy import scrape_jobs
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = BASE_DIR / "jobspy.db"
EXPORT_DIR = BASE_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)

INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "skylearn-job-scraper-internal-key")

app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = os.getenv(
    "JWT_SECRET_KEY", "dev-only-change-this-secret"
)
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = int(
    os.getenv("JWT_EXPIRES_SECONDS", "86400")
)

CORS(app)
JWTManager(app)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        headline TEXT DEFAULT '',
        location TEXT DEFAULT '',
        skills TEXT DEFAULT '[]',
        profile_image_url TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        search_term TEXT NOT NULL,
        location TEXT DEFAULT '',
        sites TEXT NOT NULL DEFAULT '[]',
        filters TEXT NOT NULL DEFAULT '{}',
        results_count INTEGER NOT NULL DEFAULT 0,
        csv_file_id INTEGER,
        json_file_id INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS job_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_id INTEGER NOT NULL,
        site TEXT,
        title TEXT,
        company TEXT,
        location TEXT,
        job_type TEXT,
        is_remote INTEGER,
        min_amount REAL,
        max_amount REAL,
        interval TEXT,
        currency TEXT,
        date_posted TEXT,
        job_url TEXT,
        job_url_direct TEXT,
        description TEXT,
        raw_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(search_id) REFERENCES search_history(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        search_id INTEGER,
        file_name TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_type TEXT NOT NULL,
        file_size INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(search_id) REFERENCES search_history(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS saved_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        site TEXT,
        title TEXT,
        company TEXT,
        location TEXT,
        job_type TEXT,
        is_remote INTEGER,
        min_amount REAL,
        max_amount REAL,
        interval TEXT,
        currency TEXT,
        date_posted TEXT,
        job_url TEXT NOT NULL,
        job_url_direct TEXT,
        description TEXT,
        raw_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        UNIQUE(user_id, job_url)
    );

    CREATE INDEX IF NOT EXISTS idx_search_user ON search_history(user_id);
    CREATE INDEX IF NOT EXISTS idx_jobs_search ON job_results(search_id);
    CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id);
    CREATE INDEX IF NOT EXISTS idx_saved_jobs_user ON saved_jobs(user_id);
    """)
    db.commit()
    db.close()


def row_to_dict(row):
    return dict(row) if row else None


def require_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def get_or_create_system_admin():
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE email = 'system_admin@lms.local'").fetchone()
    if row:
        return row
    now = utc_now()
    cur = db.execute(
        """INSERT INTO users (name, email, password_hash, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("LMS Admin", "system_admin@lms.local", generate_password_hash("system_admin_secure_key"), now, now)
    )
    db.commit()
    return db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()


def current_user():
    if hasattr(g, "internal_user") and g.internal_user:
        return g.internal_user
    try:
        user_id = int(get_jwt_identity())
        return get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    except Exception:
        return None


def get_current_user_id():
    user = current_user()
    if user:
        return user["id"]
    try:
        return int(get_jwt_identity())
    except Exception:
        return None


def user_response(row):
    if not row:
        return None
    data = row_to_dict(row)
    data.pop("password_hash", None)
    try:
        data["skills"] = json.loads(data.get("skills") or "[]")
    except json.JSONDecodeError:
        data["skills"] = []
    return data


def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # 1. Check for internal service key
        secret_header = request.headers.get("x-internal-secret")
        if secret_header and secret_header == INTERNAL_API_SECRET:
            g.internal_user = get_or_create_system_admin()
            return fn(*args, **kwargs)

        # 2. Check standard JWT
        jwt_wrap = jwt_required()(lambda: None)
        try:
            jwt_wrap()
        except Exception as e:
            return jsonify({"error": "unauthorized", "details": str(e)}), 401

        user = current_user()
        if not user:
            return jsonify({"error": "user not found"}), 404
        return fn(*args, **kwargs)
    return wrapper


def clean_value(value):
    if value is None:
        return None
    try:
        if value != value:
            return None
    except Exception:
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def safe_filename(prefix, extension):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{extension}"


import pandas as pd

def write_exports(user_id, search_id, records):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_name = safe_filename(f"search_{search_id}", "csv")
    xlsx_name = safe_filename(f"search_{search_id}", "xlsx")
    csv_path = EXPORT_DIR / csv_name
    xlsx_path = EXPORT_DIR / xlsx_name

    columns = sorted({key for record in records for key in record.keys()})
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    # Use pandas to write Excel file
    if records:
        df = pd.DataFrame(records)
    else:
        df = pd.DataFrame(columns=columns)
        
    df.to_excel(str(xlsx_path), index=False)

    db = get_db()
    now = utc_now()
    cur1 = db.execute(
        """INSERT INTO files
        (user_id, search_id, file_name, file_path, file_type, file_size, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, search_id, csv_name, str(csv_path), "csv", csv_path.stat().st_size, now),
    )
    cur2 = db.execute(
        """INSERT INTO files
        (user_id, search_id, file_name, file_path, file_type, file_size, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, search_id, xlsx_name, str(xlsx_path), "xlsx", xlsx_path.stat().st_size, now),
    )
    db.commit()
    return cur1.lastrowid, cur2.lastrowid


@app.get("/")
def index():
    return jsonify({
        "name": "JobSpy Flask API",
        "version": "1.0.0",
        "status": "running",
        "github": "https://github.com/speedyapply/JobSpy",
        "docs": "See README.md",
    })


# ---------------- AUTH ----------------

@app.post("/api/auth/signup")
def signup():
    data = require_json()
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not name or not email or not password:
        return jsonify({"error": "name, email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400

    db = get_db()
    if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
        return jsonify({"error": "email already registered"}), 409

    now = utc_now()
    cur = db.execute(
        """INSERT INTO users
        (name, email, password_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)""",
        (name, email, generate_password_hash(password), now, now),
    )
    db.commit()

    user = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    token = create_access_token(identity=str(user["id"]))

    return jsonify({
        "message": "signup successful",
        "access_token": token,
        "user": user_response(user),
    }), 201


@app.post("/api/auth/login")
def login():
    data = require_json()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    user = get_db().execute(
        "SELECT * FROM users WHERE email = ?", (email,)
    ).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "invalid email or password"}), 401

    token = create_access_token(identity=str(user["id"]))
    return jsonify({
        "message": "login successful",
        "access_token": token,
        "user": user_response(user),
    })


@app.put("/api/auth/password")
@auth_required
def change_password():
    data = require_json()
    current_password = str(data.get("current_password", ""))
    new_password = str(data.get("new_password", ""))

    if not current_password or not new_password:
        return jsonify({"error": "current_password and new_password are required"}), 400

    if len(new_password) < 8:
        return jsonify({"error": "new_password must be at least 8 characters"}), 400

    user_id = int(get_jwt_identity())
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    if not check_password_hash(user["password_hash"], current_password):
        return jsonify({"error": "incorrect current password"}), 401

    new_hash = generate_password_hash(new_password)
    db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (new_hash, utc_now(), user_id)
    )
    db.commit()

    return jsonify({"message": "password updated successfully"})


# ---------------- PROFILE ----------------

@app.get("/api/profile")
@auth_required
def get_profile():
    return jsonify({"profile": user_response(current_user())})


@app.put("/api/profile")
@auth_required
def update_profile():
    data = require_json()
    allowed = ["name", "headline", "location", "profile_image_url"]

    user = current_user()
    updates = []
    values = []

    for key in allowed:
        if key in data:
            updates.append(f"{key} = ?")
            values.append(str(data[key]).strip())

    if "skills" in data:
        if not isinstance(data["skills"], list):
            return jsonify({"error": "skills must be an array"}), 400
        updates.append("skills = ?")
        values.append(json.dumps(data["skills"], ensure_ascii=False))

    if not updates:
        return jsonify({"error": "no profile fields supplied"}), 400

    updates.append("updated_at = ?")
    values.append(utc_now())
    values.append(user["id"])

    db = get_db()
    db.execute(
        f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    db.commit()

    return jsonify({
        "message": "profile updated",
        "profile": user_response(current_user()),
    })


# ---------------- SCRAPING ----------------

ALLOWED_SITES = {
    "linkedin",
    "indeed",
    "glassdoor",
    "google",
    "zip_recruiter",
    "bayt",
    "naukri",
    "bdjobs",
}


@app.post("/api/scraping/jobs")
@auth_required
def scrape():
    data = require_json()
    search_term = str(data.get("search_term", "")).strip()
    location = str(data.get("location", "")).strip()
    sites = data.get("sites", ["indeed", "linkedin", "naukri"])

    if not search_term:
        return jsonify({"error": "search_term is required"}), 400
    if not isinstance(sites, list) or not sites:
        return jsonify({"error": "sites must be a non-empty array"}), 400

    sites = [str(site).lower() for site in sites]
    invalid = [site for site in sites if site not in ALLOWED_SITES]
    if invalid:
        return jsonify({
            "error": "unsupported site(s)",
            "invalid_sites": invalid,
            "allowed_sites": sorted(ALLOWED_SITES),
        }), 400

    try:
        results_wanted = max(1, min(int(data.get("results_wanted", 10)), 500))
        hours_old = data.get("hours_old")
        hours_old = int(hours_old) if hours_old not in (None, "") else None
        if hours_old is not None:
            hours_old = max(1, min(hours_old, 720))
    except (ValueError, TypeError):
        return jsonify({"error": "invalid results_wanted or hours_old"}), 400

    kwargs = {
        "site_name": sites,
        "search_term": search_term,
        "location": location,
        "results_wanted": results_wanted,
        "verbose": 1,
    }

    if hours_old is not None:
        kwargs["hours_old"] = hours_old

    if "indeed" in sites or "glassdoor" in sites:
        kwargs["country_indeed"] = str(data.get("country_indeed", "India"))

    optional_map = {
        "distance": int,
        "offset": int,
        "job_type": str,
        "is_remote": bool,
        "easy_apply": bool,
        "linkedin_fetch_description": bool,
        "description_format": str,
        "google_search_term": str,
    }

    for key, caster in optional_map.items():
        if key in data and data[key] is not None:
            try:
                kwargs[key] = caster(data[key])
            except (ValueError, TypeError):
                return jsonify({"error": f"invalid value for {key}"}), 400

    # JobSpy documents that LinkedIn can fetch descriptions/direct URLs,
    # but it increases requests. Keep it opt-in.
    try:
        jobs = scrape_jobs(**kwargs)
    except Exception as exc:
        app.logger.exception("JobSpy failed")
        return jsonify({
            "error": "scraping failed",
            "details": str(exc),
        }), 502

    records = []
    for record in jobs.to_dict(orient="records"):
        records.append({key: clean_value(value) for key, value in record.items()})

    user_id = get_current_user_id() or 1
    db = get_db()
    now = utc_now()

    filters = {
        key: data.get(key)
        for key in [
            "hours_old", "results_wanted", "distance", "job_type",
            "is_remote", "easy_apply", "linkedin_fetch_description",
            "description_format", "google_search_term", "country_indeed"
        ]
        if key in data
    }

    cur = db.execute(
        """INSERT INTO search_history
        (user_id, search_term, location, sites, filters, results_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            search_term,
            location,
            json.dumps(sites),
            json.dumps(filters),
            len(records),
            now,
        ),
    )
    search_id = cur.lastrowid

    for record in records:
        db.execute(
            """INSERT INTO job_results
            (search_id, site, title, company, location, job_type, is_remote,
             min_amount, max_amount, interval, currency, date_posted,
             job_url, job_url_direct, description, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                search_id,
                record.get("site"),
                record.get("title"),
                record.get("company"),
                record.get("location"),
                record.get("job_type"),
                int(bool(record.get("is_remote"))) if record.get("is_remote") is not None else None,
                record.get("min_amount"),
                record.get("max_amount"),
                record.get("interval"),
                record.get("currency"),
                record.get("date_posted"),
                record.get("job_url"),
                record.get("job_url_direct"),
                record.get("description"),
                json.dumps(record, ensure_ascii=False, default=str),
                now,
            ),
        )

    db.commit()

    csv_file_id, json_file_id = write_exports(user_id, search_id, records)

    db.execute(
        "UPDATE search_history SET csv_file_id = ?, json_file_id = ? WHERE id = ?",
        (csv_file_id, json_file_id, search_id),
    )
    db.commit()

    return jsonify({
        "message": "scraping completed",
        "search_id": search_id,
        "count": len(records),
        "files": {
            "csv_file_id": csv_file_id,
            "json_file_id": json_file_id,
        },
        "jobs": records,
    }), 200


# Alias requested as a generic scraping API.
@app.post("/api/scrape")
@auth_required
def scrape_alias():
    return scrape()


# ---------------- HISTORY ----------------

@app.get("/api/history")
@auth_required
def history():
    user_id = get_current_user_id()
    db = get_db()

    if user_id:
        searches = db.execute(
            """SELECT id, search_term, location, sites, filters, results_count,
                      csv_file_id, json_file_id, created_at
               FROM search_history
               WHERE user_id = ?
               ORDER BY id DESC""",
            (user_id,),
        ).fetchall()
    else:
        searches = db.execute(
            """SELECT id, search_term, location, sites, filters, results_count,
                      csv_file_id, json_file_id, created_at
               FROM search_history
               ORDER BY id DESC"""
        ).fetchall()

    search_list = []
    for row in searches:
        item = row_to_dict(row)
        item["sites"] = json.loads(item["sites"] or "[]")
        item["filters"] = json.loads(item["filters"] or "{}")

        jobs = db.execute(
            """SELECT id, site, title, company, location, job_type, is_remote,
                      min_amount, max_amount, interval, currency, date_posted,
                      job_url, job_url_direct, description, created_at
               FROM job_results
               WHERE search_id = ?
               ORDER BY id DESC""",
            (item["id"],),
        ).fetchall()
        item["jobs"] = [row_to_dict(job) for job in jobs]

        file_rows = db.execute(
            """SELECT id, file_name, file_path, file_type, file_size, created_at
               FROM files WHERE search_id = ? ORDER BY id""",
            (item["id"],),
        ).fetchall()
        item["files"] = [row_to_dict(file) for file in file_rows]
        search_list.append(item)

    files = db.execute(
        """SELECT id, search_id, file_name, file_path, file_type, file_size, created_at
           FROM files WHERE user_id = ? ORDER BY id DESC""",
        (user_id,),
    ).fetchall()

    return jsonify({
        "searches": search_list,
        "files": [row_to_dict(file) for file in files],
    })


@app.get("/api/history/searches")
@auth_required
def search_history():
    user_id = get_current_user_id()
    if user_id:
        rows = get_db().execute(
            """SELECT id, search_term, location, sites, filters, results_count,
                      csv_file_id, json_file_id, created_at
               FROM search_history
               WHERE user_id = ?
               ORDER BY id DESC""",
            (user_id,),
        ).fetchall()
    else:
        rows = get_db().execute(
            """SELECT id, search_term, location, sites, filters, results_count,
                      csv_file_id, json_file_id, created_at
               FROM search_history
               ORDER BY id DESC"""
        ).fetchall()

    result = []
    for row in rows:
        item = row_to_dict(row)
        item["sites"] = json.loads(item["sites"] or "[]")
        item["filters"] = json.loads(item["filters"] or "{}")
        result.append(item)
    return jsonify({"searches": result})


@app.get("/api/history/files")
@auth_required
def file_history():
    user_id = int(get_jwt_identity())
    rows = get_db().execute(
        """SELECT id, search_id, file_name, file_path, file_type, file_size, created_at
           FROM files WHERE user_id = ? ORDER BY id DESC""",
        (user_id,),
    ).fetchall()
    return jsonify({"files": [row_to_dict(row) for row in rows]})


@app.get("/api/history/jobs/<int:search_id>")
@auth_required
def search_jobs_history(search_id):
    user_id = get_current_user_id()
    db = get_db()

    if user_id and not (hasattr(g, "internal_user") and g.internal_user):
        owns = db.execute(
            "SELECT id FROM search_history WHERE id = ? AND user_id = ?",
            (search_id, user_id),
        ).fetchone()
        if not owns:
            return jsonify({"error": "search not found"}), 404

    rows = db.execute(
        """SELECT * FROM job_results WHERE search_id = ? ORDER BY id DESC""",
        (search_id,),
    ).fetchall()

    jobs = []
    for row in rows:
        item = row_to_dict(row)
        try:
            item["raw_json"] = json.loads(item["raw_json"] or "{}")
        except json.JSONDecodeError:
            pass
        jobs.append(item)

    return jsonify({"search_id": search_id, "jobs": jobs})


# ---------------- SAVED JOBS ----------------

@app.get("/api/saved-jobs")
@auth_required
def get_saved_jobs():
    user_id = int(get_jwt_identity())
    db = get_db()
    rows = db.execute(
        "SELECT * FROM saved_jobs WHERE user_id = ? ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    
    jobs = []
    for row in rows:
        item = row_to_dict(row)
        try:
            item["raw_json"] = json.loads(item["raw_json"] or "{}")
        except json.JSONDecodeError:
            pass
        jobs.append(item)
    return jsonify({"saved_jobs": jobs})


@app.post("/api/saved-jobs")
@auth_required
def save_job():
    data = require_json()
    job_url = str(data.get("job_url", "")).strip()
    if not job_url:
        return jsonify({"error": "job_url is required"}), 400
        
    user_id = int(get_jwt_identity())
    db = get_db()
    
    existing = db.execute(
        "SELECT id FROM saved_jobs WHERE user_id = ? AND job_url = ?",
        (user_id, job_url)
    ).fetchone()
    
    if existing:
        return jsonify({"message": "Job already saved", "id": existing["id"]}), 200
        
    now = utc_now()
    try:
        cur = db.execute(
            """INSERT INTO saved_jobs
            (user_id, site, title, company, location, job_type, is_remote,
             min_amount, max_amount, interval, currency, date_posted,
             job_url, job_url_direct, description, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                data.get("site"),
                data.get("title"),
                data.get("company"),
                data.get("location"),
                data.get("job_type"),
                int(bool(data.get("is_remote"))) if data.get("is_remote") is not None else None,
                data.get("min_amount"),
                data.get("max_amount"),
                data.get("interval"),
                data.get("currency"),
                data.get("date_posted"),
                job_url,
                data.get("job_url_direct"),
                data.get("description"),
                json.dumps(data, ensure_ascii=False, default=str),
                now,
            )
        )
        db.commit()
        return jsonify({"message": "Job saved", "id": cur.lastrowid}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Integrity error saving job"}), 400


@app.delete("/api/saved-jobs/<int:job_id>")
@auth_required
def delete_saved_job(job_id):
    user_id = int(get_jwt_identity())
    db = get_db()
    cur = db.execute(
        "DELETE FROM saved_jobs WHERE id = ? AND user_id = ?",
        (job_id, user_id)
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "job not found or not owned by user"}), 404
    return jsonify({"message": "Job unsaved"}), 200


# ---------------- FILE DOWNLOAD ----------------

@app.get("/api/files/<int:file_id>")
@auth_required
def download_file(file_id):
    user_id = int(get_jwt_identity())
    row = get_db().execute(
        """SELECT * FROM files WHERE id = ? AND user_id = ?""",
        (file_id, user_id),
    ).fetchone()

    if not row:
        return jsonify({"error": "file not found"}), 404

    path = Path(row["file_path"]).resolve()
    export_root = EXPORT_DIR.resolve()

    if export_root not in path.parents:
        return jsonify({"error": "invalid file location"}), 400
    if not path.exists():
        return jsonify({"error": "file no longer exists"}), 404

    return send_file(path, as_attachment=True, download_name=row["file_name"])


# ---------------- INTERNAL API (FOR LMS BACKEND) ----------------

@app.get("/api/internal/health")
def internal_health():
    return jsonify({
        "status": "ok",
        "service": "JobScraper API",
        "port": int(os.getenv("PORT", 5005)),
        "time": utc_now(),
    })


@app.post("/api/internal/scrape")
@auth_required
def internal_scrape():
    return scrape()


@app.get("/api/internal/history")
@auth_required
def internal_history():
    db = get_db()
    searches = db.execute(
        """SELECT id, search_term, location, sites, filters, results_count,
                  csv_file_id, json_file_id, created_at
           FROM search_history
           ORDER BY id DESC"""
    ).fetchall()

    search_list = []
    for row in searches:
        item = row_to_dict(row)
        try:
            item["sites"] = json.loads(item["sites"] or "[]")
        except Exception:
            item["sites"] = []
        try:
            item["filters"] = json.loads(item["filters"] or "{}")
        except Exception:
            item["filters"] = {}
        search_list.append(item)

    return jsonify({"searches": search_list})


@app.get("/api/internal/history/jobs/<int:search_id>")
@auth_required
def internal_search_jobs(search_id):
    db = get_db()
    rows = db.execute(
        """SELECT * FROM job_results WHERE search_id = ? ORDER BY id DESC""",
        (search_id,),
    ).fetchall()

    jobs = []
    for row in rows:
        item = row_to_dict(row)
        try:
            item["raw_json"] = json.loads(item["raw_json"] or "{}")
        except json.JSONDecodeError:
            pass
        jobs.append(item)

    return jsonify({"search_id": search_id, "jobs": jobs})


@app.get("/api/internal/files/<int:file_id>")
@auth_required
def internal_download_file(file_id):
    row = get_db().execute(
        """SELECT * FROM files WHERE id = ?""",
        (file_id,),
    ).fetchone()

    if not row:
        return jsonify({"error": "file not found"}), 404

    path = Path(row["file_path"]).resolve()
    export_root = EXPORT_DIR.resolve()

    if export_root not in path.parents:
        return jsonify({"error": "invalid file location"}), 400
    if not path.exists():
        return jsonify({"error": "file no longer exists"}), 404

    return send_file(path, as_attachment=True, download_name=row["file_name"])


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "route not found"}), 404


@app.errorhandler(500)
def server_error(error):
    return jsonify({"error": "internal server error"}), 500


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5005))
    print(f"[*] JobScraper Flask API running on port {port}")
    app.run(host="127.0.0.1", port=port, debug=False)

