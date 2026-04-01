import os
import uuid
import sqlite3
import threading
import time
from datetime import datetime

# ── ffmpeg PATH fix (must be before moviepy import) ───────────────────────────
_local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
if _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + os.pathsep + os.environ.get("PATH", "")
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, request, send_file, render_template, redirect, session, jsonify, Response
from moviepy import VideoFileClip, TextClip, CompositeVideoClip

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TEMPLATE_VIDEO = "assets/invite_template.mp4"
FONT_FILE      = "assets/myfont.ttf"
OUTPUT_DIR     = "generated"
DB_PATH        = "data/names.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "galkot2025")
ADMIN_ROUTE    = "/admin-dashboard-galkot"

NAME_Y        = 1200   # move UP = decrease, move DOWN = increase (canvas is 1920px tall)
NAME_X        = 540    # only used if you switch to (NAME_X, NAME_Y) positioning
NAME_FONTSIZE = 90
NAME_COLOR    = "white"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("data",     exist_ok=True)

# ─────────────────────────────────────────────
# IN-MEMORY JOB STORE
# key: job_id (str)
# value: dict with keys: status, path, error
# status: "pending" | "done" | "error"
# ─────────────────────────────────────────────
jobs: dict = {}
jobs_lock = threading.Lock()

# ─────────────────────────────────────────────
# DATABASE — name + timestamp only
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT    NOT NULL,
                created TEXT    NOT NULL
            )
        """)
        db.commit()

init_db()
_db_lock = threading.Lock()

def log_name(name: str):
    with _db_lock:
        with get_db() as db:
            db.execute(
                "INSERT INTO generations (name, created) VALUES (?, ?)",
                (name, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
            )
            db.commit()


# ─────────────────────────────────────────────
# VIDEO GENERATION  (runs in background thread)
# ─────────────────────────────────────────────
def render_video_job(job_id: str, name: str):
    """Runs in a daemon thread. Updates jobs[job_id] when done."""
    try:
        base = VideoFileClip(TEMPLATE_VIDEO)

        txt = (
            TextClip(
                text=name,
                font=FONT_FILE,
                font_size=NAME_FONTSIZE,
                color=NAME_COLOR,
                method="caption",
                size=(1000, None),
                text_align="center",
            )
            .with_duration(base.duration)
            .with_position(("center", NAME_Y))
        )

        composite = CompositeVideoClip([base, txt])
        out_path  = os.path.join(OUTPUT_DIR, f"{job_id}.mp4")

        composite.write_videofile(
            out_path,
            codec="libx264",
            audio_codec="aac",
            fps=24,
            bitrate="1500k",
            preset="ultrafast",
            threads=2,
            logger=None,
        )

        txt.close()
        composite.close()
        base.close()

        log_name(name)  # only logged after successful render

        with jobs_lock:
            jobs[job_id] = {"status": "done", "path": out_path, "name": name}

    except Exception as e:
        app.logger.error(f"[render_video_job] {e}")
        with jobs_lock:
            jobs[job_id] = {"status": "error", "path": None, "name": name, "error": str(e)}


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    """
    Accepts the form, immediately starts a background thread,
    and returns a job_id. The frontend polls /status/<job_id>.
    This means gunicorn's worker is FREE instantly — no timeout.
    """
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400
    if len(name) > 60:
        return jsonify({"error": "Name too long (max 60 characters)."}), 400

    job_id = uuid.uuid4().hex

    with jobs_lock:
        jobs[job_id] = {"status": "pending", "path": None, "name": name}

    t = threading.Thread(target=render_video_job, args=(job_id, name), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    """Frontend polls this every 3 seconds to check render progress."""
    with jobs_lock:
        job = jobs.get(job_id)

    if job is None:
        return jsonify({"status": "not_found"}), 404

    return jsonify({"status": job["status"]})


@app.route("/download/<job_id>")
def download(job_id):
    """Called by frontend once status == 'done'."""
    with jobs_lock:
        job = jobs.get(job_id)

    if not job or job["status"] != "done":
        return "Not ready or not found.", 404

    safe_name = job["name"].replace(" ", "_")
    return send_file(
        job["path"],
        mimetype="video/mp4",
        as_attachment=True,
        download_name=f"invite_{safe_name}.mp4",
    )


# ─────────────────────────────────────────────
# ADMIN — id | name | timestamp only
# ─────────────────────────────────────────────
@app.route(ADMIN_ROUTE, methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
        else:
            return render_template("admin.html", error="Wrong password.", rows=None)

    if not session.get("admin"):
        return render_template("admin.html", error=None, rows=None)

    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, created FROM generations ORDER BY id DESC"
        ).fetchall()

    return render_template("admin.html", error=None, rows=rows)


@app.route(f"{ADMIN_ROUTE}/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(ADMIN_ROUTE)


if __name__ == "__main__":
    app.run(debug=True)