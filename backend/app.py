import os
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
frontend_origin = os.environ.get("FRONTEND_ORIGIN", "*").strip()
CORS(app, resources={r"/api/*": {"origins": frontend_origin.split(",") if frontend_origin != "*" else "*"}}, expose_headers=["Content-Disposition"])

download_lock = threading.Lock()
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", "10800"))
MAX_OUTPUT_MB = int(os.environ.get("MAX_OUTPUT_MB", "300"))


def duration_filter(info, *, incomplete):
    """Reject videos longer than the configured limit; allow unknown duration."""
    duration = info.get("duration")
    if duration is not None and duration > MAX_DURATION_SECONDS:
        return f"视频时长超过 {MAX_DURATION_SECONDS} 秒限制"
    return None

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
        mode = data.get("mode", "audio")
        if mode not in ("audio", "video", "av_pitch"):
            raise ValueError("请选择音频、视频或音视频升降调模式。")
        pitch = data.get("pitch", 0)
        if isinstance(pitch, bool) or not isinstance(pitch, int) or pitch < -12 or pitch > 12:
            raise ValueError("升降调参数必须为 -12 到 +12 的整数半音。")
        if mode != "av_pitch":
            pitch = 0
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not download_lock.acquire(blocking=False):
        return jsonify({"error": "当前有其他任务正在转换，请稍等片刻再试。"}), 429

    temp_dir = tempfile.mkdtemp(prefix="bili-audio-")
    try:
        opts = {
            "format": ("bestaudio/best" if mode == "audio" else "bestvideo*+bestaudio/best"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "ffmpeg_location": shutil.which("ffmpeg") or "/usr/bin/ffmpeg",
            "restrictfilenames": True,
            "quiet": False,
            "no_warnings": False,
            "verbose": True,
            "socket_timeout": 25,
            "retries": 1,
            "fragment_retries": 1,
            "max_filesize": MAX_OUTPUT_MB * 1024 * 1024,
            "match_filter": duration_filter,
            "http_headers": {"User-Agent": "Mozilla/5.0"},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            app.logger.info("Starting yt-dlp; host=%s; version=%s", urlparse(url).hostname, yt_dlp.version.__version__)
            info = ydl.extract_info(url, download=True)
            app.logger.info("yt-dlp returned: extractor=%s id=%s duration=%s requested_downloads=%s files=%s",
                            info.get("extractor_key"), info.get("id"), info.get("duration"),
                            len(info.get("requested_downloads") or []), os.listdir(temp_dir))
            title = info.get("title") or "bilibili-audio"
            safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title).strip(" ._")[:100] or "bilibili-audio"
            ffmpeg = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
            downloaded = [
                os.path.join(temp_dir, name)
                for name in os.listdir(temp_dir)
                if os.path.isfile(os.path.join(temp_dir, name))
                and not name.lower().endswith((".part", ".json", ".description", ".jpg", ".png", ".webp"))
            ]
            if not downloaded:
                app.logger.error("yt-dlp completed without output; temp files: %s", os.listdir(temp_dir))
                raise RuntimeError("没有找到下载文件，请稍后重试。")
            source_path = max(downloaded, key=os.path.getsize)

            if mode == "audio":
                output_path = os.path.join(temp_dir, f"{safe_title}.mp3")
                subprocess.run(
                    [ffmpeg, "-y", "-i", source_path, "-vn", "-map", "0:a:0",
                     "-ac", "2", "-ar", "48000", "-c:a", "libmp3lame",
                     "-b:a", "320k", "-id3v2_version", "3", output_path],
                    check=True, capture_output=True, text=True, timeout=600
                )
                mime_type = "audio/mpeg"
            elif mode == "video":
                output_path = os.path.join(temp_dir, f"{safe_title}.mp4")
                if os.path.abspath(source_path) != os.path.abspath(output_path):
                    shutil.copyfile(source_path, output_path)
                mime_type = "video/mp4"
            else:
                if not pitch:
                    output_path = os.path.join(temp_dir, f"{safe_title}.mp4")
                    if os.path.abspath(source_path) != os.path.abspath(output_path):
                        shutil.copyfile(source_path, output_path)
                else:
                    wav_path = os.path.join(temp_dir, "audio-source.wav")
                    shifted_path = os.path.join(temp_dir, "audio-pitch-shifted.wav")
                    aac_path = os.path.join(temp_dir, "audio-pitch-shifted.m4a")
                    output_path = os.path.join(temp_dir, f"{safe_title}.mp4")
                    subprocess.run(
                        [ffmpeg, "-y", "-i", source_path, "-vn", "-map", "0:a:0",
                         "-ac", "2", "-ar", "48000", "-c:a", "pcm_s24le", wav_path],
                        check=True, capture_output=True, text=True, timeout=600
                    )
                    rubberband = shutil.which("rubberband")
                    if not rubberband:
                        raise RuntimeError("升降调组件未安装，请等待后端重新部署完成。")
                    subprocess.run(
                        [rubberband, "-3", "-F", "-p", str(pitch), wav_path, shifted_path],
                        check=True, capture_output=True, text=True, timeout=600
                    )
                    subprocess.run(
                        [ffmpeg, "-y", "-i", shifted_path, "-c:a", "aac", "-b:a", "320k", aac_path],
                        check=True, capture_output=True, text=True, timeout=600
                    )
                    subprocess.run(
                        [ffmpeg, "-y", "-i", source_path, "-i", aac_path,
                         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
                         "-movflags", "+faststart", "-shortest", output_path],
                        check=True, capture_output=True, text=True, timeout=600
                    )
                mime_type = "video/mp4"

            if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("没有生成有效的输出文件，请稍后重试。")
            if os.path.getsize(output_path) > MAX_OUTPUT_MB * 1024 * 1024:
                raise RuntimeError(f"文件超过 {MAX_OUTPUT_MB} MB 限制。")
            response = send_file(output_path, mimetype=mime_type, as_attachment=True, download_name=os.path.basename(output_path), conditional=False)
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
