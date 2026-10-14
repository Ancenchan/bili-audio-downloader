import os
import re
import shutil
import tempfile
import threading
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
frontend_origin = os.environ.get("FRONTEND_ORIGIN", "*").strip()
CORS(app, resources={r"/api/*": {"origins": frontend_origin.split(",") if frontend_origin != "*" else "*"}})

download_lock = threading.Lock()
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", "10800"))
MAX_OUTPUT_MB = int(os.environ.get("MAX_OUTPUT_MB", "300"))

def validate_bilibili_url(raw_url: str) -> str:
    if not isinstance(raw_url, str) or len(raw_url) > 2048:
        raise ValueError("链接无效或过长。")
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").lower()
    allowed = host == "bilibili.com" or host.endswith(".bilibili.com") or host == "b23.tv"
    if parsed.scheme not in ("http", "https") or not allowed:
        raise ValueError("仅支持 bilibili.com 或 b23.tv 链接。")
    if parsed.username or parsed.password:
        raise ValueError("链接格式不合法。")
    return raw_url.strip()

@app.get("/")
def index():
    return jsonify({"service": "B站音频提取器 API", "status": "ok", "usage": "POST /api/download with JSON {\"url\":\"https://www.bilibili.com/video/BV...\"}"})

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.post("/api/download")
def download_audio():
    data = request.get_json(silent=True) or {}
    try:
        url = validate_bilibili_url(data.get("url", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not download_lock.acquire(blocking=False):
        return jsonify({"error": "当前有其他任务正在转换，请稍等片刻再试。"}), 429

    temp_dir = tempfile.mkdtemp(prefix="bili-audio-")
    try:
        opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "restrictfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 25,
            "retries": 1,
            "fragment_retries": 1,
            "max_filesize": MAX_OUTPUT_MB * 1024 * 1024,
            "match_filter": yt_dlp.utils.match_filter_func(f"duration > {MAX_DURATION_SECONDS}"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}],
            "http_headers": {"User-Agent": "Mozilla/5.0"},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "bilibili-audio"
            files = [os.path.join(temp_dir, n) for n in os.listdir(temp_dir) if n.lower().endswith(".mp3") and os.path.isfile(os.path.join(temp_dir, n))]
            if not files:
                raise RuntimeError("未生成 MP3 文件。请检查 FFmpeg 是否安装，或稍后重试。")
            output_path = max(files, key=os.path.getsize)
            if os.path.getsize(output_path) > MAX_OUTPUT_MB * 1024 * 1024:
                raise RuntimeError(f"音频文件超过 {MAX_OUTPUT_MB} MB 限制。")
            safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title).strip(" ._")[:100] or "bilibili-audio"
            response = send_file(output_path, mimetype="audio/mpeg", as_attachment=True, download_name=f"{safe_title}.mp3", conditional=False)
            response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
            return response
    except yt_dlp.utils.DownloadError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        message = str(exc)
        if "Sign in" in message or "login" in message.lower() or "cookie" in message.lower():
            message = "该视频可能需要登录或受到访问限制。本服务不会绕过登录或访问控制。"
        else:
            message = "解析或下载失败：视频可能不可访问、接口发生变化，或免费服务资源不足。请稍后重试。"
        return jsonify({"error": message}), 502
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.exception("Audio extraction failed")
        return jsonify({"error": str(exc)[:300]}), 500
    finally:
        download_lock.release()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
