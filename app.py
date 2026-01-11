import os
import re
import threading
import subprocess
import time
import uuid
from flask import Flask, request, jsonify, send_from_directory, render_template_string

app = Flask(__name__)

# ---------------------------
# Config
# ---------------------------
WEB_DOWNLOADER_DIR = os.path.join(os.getcwd(), "web_downloader")
os.makedirs(WEB_DOWNLOADER_DIR, exist_ok=True)

QUALITY_MAP = {
    "144p (Low)": "144",
    "240p": "240",
    "360p": "360",
    "480p": "480",
    "720p": "720",
    "1080p": "1080",
    "1440p": "1440",
    "2160p (4K - High)": "2160",
}
# Render-safe cookie options: default none; only use cookies.txt if present
COOKIE_SOURCES = ["none", "cookies.txt"]
SPEED_MAP = {
    "Slow": {"concurrent": "1", "chunk": "1M"},
    "Mid": {"concurrent": "10", "chunk": "1M"},
    "High": {"concurrent": "20", "chunk": "5M"},
    "Turbo": {"concurrent": "40", "chunk": "10M"},
    "Superfast": {"concurrent": "80", "chunk": "20M"},
}

# In-memory job tracking
web_jobs = {}  # job_id -> {"status": {...}, "filename": None, "done": False, "error": None}

# ---------------------------
# Helpers
# ---------------------------
def parse_progress_line(line: str):
    # Matches: 12.3% of ... at 1.23MiB/s ETA 00:12
    m = re.search(r"(\d{1,3}\.\d+|\d{1,3})%.*?at\s+([\d\.]+[KMG]?iB/s).*?ETA\s+(\d{2}:\d{2})", line)
    if m:
        return float(m.group(1)), m.group(2), m.group(3)
    m2 = re.search(r"(\d{1,3}\.\d+|\d{1,3})%.*?at\s+([\d\.]+[KMG]?iB/s)", line)
    if m2:
        return float(m2.group(1)), m2.group(2), None
    return None

def build_web_cmd(url, quality, fmt, cookies, speed):
    h = QUALITY_MAP.get(quality, "480")
    s = SPEED_MAP.get(speed, SPEED_MAP["Turbo"])

    # FFmpeg-safe formats: avoid external merging on Render
    if fmt == "MP4 - Video":
        fmt_str = f"best[height<={h}][ext=mp4]"
        base = ["yt-dlp", "-f", fmt_str]
    else:
        fmt_str = "bestaudio[ext=m4a]"
        base = ["yt-dlp", "-f", fmt_str]

    # Cookies: only use cookies.txt if present; otherwise none
    if cookies == "cookies.txt" and os.path.exists("cookies.txt"):
        cookie = ["--cookies", "cookies.txt"]
    else:
        cookie = []

    fast = [
        "--concurrent-fragments", s["concurrent"],
        "--http-chunk-size", s["chunk"],
        "--retries", "3",
        "--fragment-retries", "3",
        "--newline",
    ]

    out_tmpl = os.path.join(WEB_DOWNLOADER_DIR, "%(title)s.%(ext)s")
    return base + cookie + ["--no-playlist"] + fast + ["-o", out_tmpl, url.strip()]

def web_download_worker(job_id, cmd):
    web_jobs[job_id] = {
        "status": {"pct": 0.0, "speed": "--", "eta": "--", "text": "Starting..."},
        "filename": None,
        "done": False,
        "error": None
    }
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
        for raw in proc.stdout:
            line = raw.strip()
            # Always show latest line so user sees progress or errors
            web_jobs[job_id]["status"]["text"] = line[:160]

            parsed = parse_progress_line(line)
            if parsed:
                pct, speed, eta = parsed
                web_jobs[job_id]["status"].update({"pct": pct, "speed": speed, "eta": eta or "--"})

            if "Destination:" in line:
                m = re.search(r"Destination:\s(.+)", line)
                if m:
                    web_jobs[job_id]["filename"] = os.path.basename(m.group(1))

        ret = proc.wait()
        if ret == 0:
            web_jobs[job_id]["done"] = True
            # Fallback: pick newest file if Destination wasn't parsed
            if not web_jobs[job_id]["filename"]:
                files = sorted(
                    [f for f in os.listdir(WEB_DOWNLOADER_DIR) if os.path.isfile(os.path.join(WEB_DOWNLOADER_DIR, f))],
                    key=lambda f: os.path.getmtime(os.path.join(WEB_DOWNLOADER_DIR, f)),
                    reverse=True
                )
                if files:
                    web_jobs[job_id]["filename"] = files[0]
        else:
            web_jobs[job_id]["error"] = f"yt-dlp exited {ret}"
    except Exception as e:
        web_jobs[job_id]["error"] = str(e)

# ---------------------------
# Routes
# ---------------------------
@app.route("/")
def home():
    html = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>YouTube Downloader (Web)</title>
      <style>
        body {background:#0B1026;color:#FFFFFF;font-family:Segoe UI, Arial; margin:0;}
        .wrap {max-width:780px;margin:40px auto;padding:0 16px;}
        .card {background:#12183A;border-radius:14px;box-shadow:0 10px 30px rgba(0,0,0,0.35);padding:24px;}
        h1 {text-align:center;color:#FFD700;margin-top:0;}
        label {display:block;margin:10px 0 6px;color:#A3BE8C;}
        input, select {width:100%;padding:10px;border:none;border-radius:8px;background:#1B224A;color:#FFFFFF;}
        .row {display:flex;gap:12px;}
        .row > div {flex:1;}
        .btn {background:#00C853;color:#FFFFFF;border:none;border-radius:8px;padding:12px 16px;cursor:pointer;font-weight:bold;}
        .btn:hover {background:#33E07A;}
        .progress {margin-top:18px;background:#1B224A;border-radius:10px;overflow:hidden;height:22px;}
        .bar {height:100%;width:0%;background:#00B8D4;transition:width 0.3s ease;}
        .status {margin-top:8px;color:#D8DEE9;font-size:14px;min-height:22px;}
        .footer {text-align:center;color:#D8DEE9;margin-top:18px;}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <h1>YouTube Downloader (Web)</h1>
          <form id="dlForm">
            <label>Video URL</label>
            <input name="url" placeholder="https://www.youtube.com/watch?v=...">
            <div class="row">
              <div>
                <label>Quality</label>
                <select name="quality">%QUALITY_OPTIONS%</select>
              </div>
              <div>
                <label>Format</label>
                <select name="format"><option>MP4 - Video</option><option>MP3 - Audio</option></select>
              </div>
            </div>
            <div class="row">
              <div>
                <label>Cookies</label>
                <select name="cookies">%COOKIE_OPTIONS%</select>
              </div>
              <div>
                <label>Speed</label>
                <select name="speed">%SPEED_OPTIONS%</select>
              </div>
            </div>
            <div style="margin-top:16px;">
              <button class="btn" type="submit">Start Download</button>
            </div>
          </form>

          <div class="progress" id="progress" style="display:none;">
            <div class="bar" id="bar"></div>
          </div>
          <div class="status" id="status"></div>
        </div>
        <div class="footer">Files are stored temporarily and deleted shortly after you fetch them.</div>
      </div>

      <script>
        const form = document.getElementById('dlForm');
        const progress = document.getElementById('progress');
        const bar = document.getElementById('bar');
        const statusEl = document.getElementById('status');

        form.addEventListener('submit', async (e) => {
          e.preventDefault();
          statusEl.textContent = '';
          bar.style.width = '0%';
          progress.style.display = 'block';

          const fd = new FormData(form);
          const res = await fetch('/download', { method: 'POST', body: fd });
          const data = await res.json();
          if (!data.ok) {
            statusEl.textContent = 'Error: ' + (data.error || 'Unknown');
            return;
          }
          const job = data.job_id;
          statusEl.textContent = 'Job started: ' + job;

          const timer = setInterval(async () => {
            const sres = await fetch('/status?job=' + job);
            const sdata = await sres.json();
            if (!sdata.ok) {
              statusEl.textContent = 'Error: ' + (sdata.error || 'Unknown');
              clearInterval(timer);
              return;
            }
            const st = sdata.status || {};
            const pct = st.pct || 0;
            bar.style.width = pct + '%';
            statusEl.textContent = `Progress: ${pct.toFixed ? pct.toFixed(1) : pct}% | Speed: ${st.speed || '--'} | ETA: ${st.eta || '--'} | ${st.text || ''}`;

            if (sdata.error) {
              statusEl.textContent = 'Error: ' + sdata.error;
              clearInterval(timer);
              return;
            }

            if (sdata.done && sdata.filename) {
              clearInterval(timer);
              statusEl.textContent = 'Completed. Starting download...';
              // Auto-download
              window.location.href = "/fetch/" + encodeURIComponent(sdata.filename);
            }
          }, 1000);
        });
      </script>
    </body>
    </html>
    """
    quality_opts = "".join([f"<option>{q}</option>" for q in QUALITY_MAP.keys()])
    cookie_opts = "".join([f"<option>{c}</option>" for c in COOKIE_SOURCES])
    speed_opts = "".join([f"<option>{s}</option>" for s in SPEED_MAP.keys()])
    html = html.replace("%QUALITY_OPTIONS%", quality_opts).replace("%COOKIE_OPTIONS%", cookie_opts).replace("%SPEED_OPTIONS%", speed_opts)
    return render_template_string(html)

@app.route("/download", methods=["POST"])
def web_download():
    data = request.form or {}
    url = data.get("url")
    quality = data.get("quality", "480p")
    fmt = data.get("format", "MP4 - Video")
    cookies = data.get("cookies", "none")
    speed = data.get("speed", "Turbo")

    if not url:
        return jsonify({"ok": False, "error": "Missing URL"}), 400

    cmd = build_web_cmd(url, quality, fmt, cookies, speed)
    job_id = uuid.uuid4().hex[:12]
    threading.Thread(target=web_download_worker, args=(job_id, cmd), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/status")
def web_status():
    job_id = request.args.get("job")
    if not job_id or job_id not in web_jobs:
        return jsonify({"ok": False, "error": "Invalid job"}), 400
    job = web_jobs[job_id]
    return jsonify({
        "ok": True,
        "status": job["status"],
        "done": job["done"],
        "filename": job["filename"],
        "error": job["error"]
    })

@app.route("/fetch/<path:filename>")
def web_fetch(filename):
    full_path = os.path.join(WEB_DOWNLOADER_DIR, filename)
    if not os.path.exists(full_path):
        return "File not found", 404

    # Schedule deletion shortly after sending
    def delayed_delete(path):
        time.sleep(10)
        try:
            os.remove(path)
        except Exception:
            pass

    threading.Thread(target=delayed_delete, args=(full_path,), daemon=True).start()
    return send_from_directory(WEB_DOWNLOADER_DIR, filename, as_attachment=True)

# ---------------------------
# Entrypoint for Render
# ---------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
