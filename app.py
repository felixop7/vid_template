import os
import uuid
import sqlite3
import threading
import time
import subprocess
import tempfile
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

# ── ffmpeg PATH fix ───────────────────────────────────────────────
_local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
if _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _local_bin + os.pathsep + os.environ.get("PATH", "")
# ──────────────────────────────────────────────────────────────────

from flask import Flask, request, send_file, render_template, redirect, session, jsonify, Response

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", " ")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TEMPLATE_VIDEO = "assets/invite_template.mp4"
FONT_FILE      = "assets/myfont.ttf"
OUTPUT_DIR     = "generated"
DB_PATH        = "data/names.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", " ")
ADMIN_ROUTE    = "/admin-dashboard-galkot"

NAME_Y        = 1460   # move UP = decrease, move DOWN = increase (canvas is 1920px tall)
NAME_X        = 540    # only used if you switch to (NAME_X, NAME_Y) positioning
NAME_FONTSIZE = 85
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
                (name, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            )
            db.commit()


# ─────────────────────────────────────────────
# VIDEO GENERATION  (runs in background thread)
# ─────────────────────────────────────────────
def render_video_job(job_id: str, name: str):
    """Runs in a daemon thread. Uses PIL for text + ffmpeg overlay for ultra-fast rendering."""
    temp_text_img = None
    try:
        out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp4")
        
        # Step 1: Create text image with PIL
        # Create a transparent image with video dimensions (1080x1920)
        text_img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        draw = ImageDraw.Draw(text_img)
        
        # Load font
        font = ImageFont.truetype(FONT_FILE, NAME_FONTSIZE)
        
        # Get text bounding box to center it horizontally
        bbox = draw.textbbox((0, 0), name, font=font)
        text_width = bbox[2] - bbox[0]
        text_x = (1080 - text_width) // 2
        text_y = NAME_Y
        
        # Draw text with shadow effect for better visibility
        shadow_color = (0, 0, 0, 200)
        text_color = (255, 255, 255, 255)
        
        # Draw shadow
        draw.text((text_x + 2, text_y + 2), name, font=font, fill=shadow_color)
        # Draw main text
        draw.text((text_x, text_y), name, font=font, fill=text_color)
        
        # Save text image temporarily
        temp_text_img = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
        text_img.save(temp_text_img.name, 'PNG')
        temp_text_img.close()
        
        # Step 2: Use ffmpeg to overlay the text image and encode
        # Optimized for Render free tier (weak CPU) while maintaining HIGH quality:
        # - Use fixed bitrate instead of CRF for faster encoding on weak CPU
        # - 3500k bitrate: ~3x faster than CRF 20, excellent quality, 12-15MB output
        # - Expected times: Local ~1.4s, Render ~30-60s (vs 5min with CRF 20)
        
        # Build ffmpeg command with proper filter_complex and stream mapping
        cmd = [
            "ffmpeg",
            "-i", TEMPLATE_VIDEO,
            "-i", temp_text_img.name,
            "-filter_complex", "[0:v][1:v]overlay=0:0[outv]",
            "-map", "[outv]",
            "-map", "0:a",
            "-c:v", "libx264",
            "-b:v", "3500k",  # High bitrate = excellent quality, faster on weak CPU
            "-maxrate", "4000k",  # Cap bitrate spikes
            "-bufsize", "8000k",  # Buffer for rate control
            "-preset", "ultrafast",  # Maximum encoding speed
            "-r", "30",  # Keep original FPS (30 fps)
            "-c:a", "aac",
            "-b:a", "128k",  # Match original audio bitrate
            "-y",  # Overwrite output file
            out_path
        ]
        
        # Run ffmpeg
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120  # 2 minute timeout
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr}")
        
        log_name(name)  # only logged after successful render
        
        with jobs_lock:
            jobs[job_id] = {"status": "done", "path": out_path, "name": name}
    
    except subprocess.TimeoutExpired:
        app.logger.error(f"[render_video_job] FFmpeg timeout for job {job_id}")
        with jobs_lock:
            jobs[job_id] = {"status": "error", "path": None, "name": name, "error": "Rendering timeout"}
    
    except Exception as e:
        app.logger.error(f"[render_video_job] {e}")
        with jobs_lock:
            jobs[job_id] = {"status": "error", "path": None, "name": name, "error": str(e)}
    
    finally:
        # Clean up temporary file
        if temp_text_img and os.path.exists(temp_text_img.name):
            try:
                os.unlink(temp_text_img.name)
            except:
                pass


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