import os
import uuid
import sqlite3
from datetime import datetime

# ── Make sure the static ffmpeg we installed during build is on PATH ──────────
# Render's gunicorn process may not inherit the build-time PATH, so we patch it
# explicitly before importing MoviePy (which probes for ffmpeg at import time).
_local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
if _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + os.pathsep + os.environ.get("PATH", "")
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, request, send_file, render_template, redirect, url_for, session, jsonify
from moviepy import VideoFileClip, TextClip, CompositeVideoClip

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TEMPLATE_VIDEO   = "assets/invite_template.mp4"
FONT_FILE        = "assets/myfont.ttf"
OUTPUT_DIR       = "generated"
DB_PATH          = "data/names.db"
ADMIN_PASSWORD   = os.environ.get("ADMIN_PASSWORD", "galkot2025")

# Name overlay position — tweak these to hit your Canva "name tag" area.
# (0,0) = top-left corner of the 1080×1920 frame.
# Example: x=540, y=960 places text dead-centre vertically and horizontally.
NAME_X           = 540          # horizontal centre (auto-centred with method="caption")
NAME_Y           = 1200         # vertical position  → move UP by decreasing, DOWN by increasing
NAME_FONTSIZE    = 90
NAME_COLOR       = "white"
NAME_DURATION    = None         # None = match the template video duration automatically

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs("data", exist_ok=True)


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                created   TEXT    NOT NULL
            )
        """)
        db.commit()

init_db()


def log_name(name: str):
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
    """
    Overlay `name` on the template video and return the output file path.

    Coordinate guide
    ────────────────
    The canvas is 1080 × 1920 pixels (width × height).
    • NAME_X controls horizontal position.  With method="caption" and
      size=(1080, None), the text is word-wrapped and centred automatically,
      so NAME_X only shifts the whole text block left/right from its anchor.
    • NAME_Y controls how far down the frame the baseline sits.
      Increase to move the name lower; decrease to move it higher.
    • Change `position=("center", NAME_Y)` to `position=(NAME_X, NAME_Y)`
      if you need pixel-perfect left/right placement instead of centring.
    """
    base = VideoFileClip(TEMPLATE_VIDEO)
    duration = base.duration

    txt = (
        TextClip(
            text=name,
            font=FONT_FILE,
            font_size=NAME_FONTSIZE,
            color=NAME_COLOR,
            method="caption",          # auto word-wrap
            size=(1000, None),         # max width = 1000 px, height auto
            text_align="center",
        )
        .with_duration(duration)
        .with_position(("center", NAME_Y))   # ← swap for (NAME_X, NAME_Y) if needed
    )

    composite = CompositeVideoClip([base, txt])

    out_path = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex}.mp4")
    composite.write_videofile(
        out_path,
        codec="libx264",
        audio_codec="aac",
        fps=24,                        # 24 fps is fine for a story invite
        bitrate="2000k",               # lower = faster render on free tier
        preset="ultrafast",            # sacrifices compression for speed
        threads=2,
        logger=None,
    )

    base.close()
    composite.close()
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
        return jsonify({"error": "Name is required"}), 400
    if len(name) > 60:
        return jsonify({"error": "Name is too long (max 60 characters)"}), 400

    log_name(name)

    try:
        video_path = make_video(name)
    except Exception as e:
        app.logger.error(f"Video generation failed: {e}")
        return jsonify({"error": "Video generation failed. Please try again."}), 500

    return send_file(
        video_path,
        mimetype="video/mp4",
        as_attachment=True,                          # forces download on iOS & Android
        download_name=f"invite_{name.replace(' ', '_')}.mp4",
    )


# ─────────────────────────────────────────────
# ADMIN DASHBOARD  (hidden route + password)
# ─────────────────────────────────────────────
ADMIN_ROUTE = "/admin-dashboard-galkot"

@app.route(ADMIN_ROUTE, methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
        else:
            return render_template("admin.html", error="Wrong password", rows=None)

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