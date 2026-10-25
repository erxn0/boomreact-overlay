import json, os, subprocess, tempfile, threading
from flask import Flask, request, Response, abort

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

SECRET = os.environ.get("SECRET", "")
OVERLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.mp4")
MAX_SIDE = 1280      # long side cap (keeps the free 0.1 CPU instance fast)
FFMPEG_TIMEOUT = 100  # seconds, the bot waits max 120
lock = threading.Lock()  # 512 MB RAM: one job at a time


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, timeout=20, check=True).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(v.get("tags", {}).get("rotate", 0) or 0)
    if abs(rot) % 180 == 90:      # ffmpeg autorotates, so use the displayed size
        w, h = h, w
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    return w, h, dur, has_audio


@app.get("/")
def health():
    return "ok"


@app.post("/overlay")
def overlay():
    if SECRET and request.headers.get("X-Secret") != SECRET:
        abort(403)
    data = request.get_data()
    if not data:
        abort(400, "empty body")
    with lock, tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.mp4"), os.path.join(d, "out.mp4")
        ov_path = OVERLAY
        n = int(request.headers.get("X-Overlay-Len", "0") or 0)
        if n > 0:  # the bot sent the overlay video first, then the main video
            ov_path = os.path.join(d, "overlay.mp4")
            with open(ov_path, "wb") as f:
                f.write(data[:n])
            data = data[n:]
        with open(src, "wb") as f:
            f.write(data)
        try:
            w, h, dur, a_main = probe(src)
            _, _, ov_dur, a_ov = probe(ov_path)
        except Exception as e:
            abort(400, "cannot read video: %s" % e)

        scale = min(1.0, MAX_SIDE / max(w, h))
        W, H = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
        t = round(dur / 2, 3)               # middle of the video
        ms = int(t * 1000)

        fc = (
            f"[0:v]scale={W}:{H},setsar=1,fps=30[bg];"
            f"[1:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,"
            f"setpts=PTS-STARTPTS+{t}/TB[ov];"
            f"[bg][ov]overlay=enable='between(t,{t},{t + ov_dur})':eof_action=pass[v]"
        )
        maps = ["-map", "[v]"]
        if a_main and a_ov:
            fc += (f";[1:a]adelay={ms}|{ms}[oa];"
                   f"[0:a][oa]amix=inputs=2:duration=first:normalize=0[a]")
            maps += ["-map", "[a]"]
        elif a_main:
            maps += ["-map", "0:a"]
        elif a_ov:
            fc += f";[1:a]adelay={ms}|{ms}[a]"
            maps += ["-map", "[a]"]

        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-i", ov_path,
               "-filter_complex", fc, *maps,
               "-t", str(dur),
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            abort(504, "ffmpeg timeout")
        if r.returncode != 0:
            abort(500, r.stderr[-500:])
        with open(dst, "rb") as f:
            out = f.read()
    return Response(out, mimetype="video/mp4",
                    headers={"X-Duration": str(dur), "X-Overlay-At": str(t)})
