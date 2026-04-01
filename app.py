import os
import uuid
import sqlite3
import threading
from datetime import datetime

# ── ffmpeg PATH fix (must be before moviepy import) ───────────────────────────
_local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
if _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + os.pathsep + os.environ.get("PATH", "")
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, request, send_file, render_template, redirect, session, jsonify
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

NAME_Y        = 1200
NAME_X        = 540
NAME_FONTSIZE = 90
NAME_COLOR    = "white"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("data",     exist_ok=True)

# ─────────────────────────────────────────────
# DATABASE — stores ONLY name + timestamp
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
# VIDEO GENERATION
# ─────────────────────────────────────────────
def make_video(name: str) -> str:
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
    out_path  = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex}.mp4")

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
    return out_path

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate():
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400
    if len(name) > 60:
        return jsonify({"error": "Name too long (max 60 characters)."}), 400

    try:
        video_path = make_video(name)
    except Exception as e:
        app.logger.error(f"[make_video] {e}")
        return jsonify({"error": "Video generation failed. Please try again."}), 500

    log_name(name)   # only saved after successful render

    return send_file(
        video_path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=f"invite_{name.replace(' ', '_')}.mp4",
    )

# ─────────────────────────────────────────────
# ADMIN — shows id | name | timestamp ONLY
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