import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
frontend_origin = os.environ.get("FRONTEND_ORIGIN", "*").strip()
CORS(app, resources={r"/api/*": {"origins": frontend_origin.split(",") if frontend_origin != "*" else "*"}}, expose_headers=["Content-Disposition"])

download_lock = threading.Lock()
progress_lock = threading.Lock()
task_progress = {}
task_logs = {}
task_files = {}
MAX_LOG_LINES = 600

MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", "10800"))
MAX_OUTPUT_MB = int(os.environ.get("MAX_OUTPUT_MB", "300"))
# 转码等子进程的最长执行时间（秒），大视频升降调可能需要较久
SUBPROCESS_TIMEOUT = int(os.environ.get("SUBPROCESS_TIMEOUT", "1800"))
# 处理完成的文件在服务器上保留多久供浏览器（重新）下载，超时惰性清理
READY_TTL_SECONDS = int(os.environ.get("READY_TTL_SECONDS", "1800"))
TASK_ID_RE = re.compile(r"[a-zA-Z0-9_-]+")


def add_task_log(task_id, message, level="INFO"):
    from datetime import datetime
    line = f"{datetime.utcnow().strftime('%H:%M:%S')} [{level}] {str(message).rstrip()}"
    with progress_lock:
        lines = task_logs.setdefault(task_id, [])
        lines.append(line[:2000])
        if len(lines) > MAX_LOG_LINES:
            del lines[:-MAX_LOG_LINES]


def duration_filter(info, *, incomplete):
    """Reject videos longer than the configured limit; allow unknown duration."""
    duration = info.get("duration")
    if duration is not None and duration > MAX_DURATION_SECONDS:
        return f"视频时长超过 {MAX_DURATION_SECONDS} 秒限制"
    return None


def video_fallback_formats(quality):
    """视频流被 CDN 拒绝（HTTP 503）时的兜底格式链：先换 AVC 编码（走不同线路），再退回单文件。"""
    height = f"[height<={quality}]" if quality != "best" else ""
    if height:
        return [f"bv*{height}[vcodec^=avc]+ba/b{height}", f"b{height}/best/b"]
    return ["bv*[vcodec^=avc]+ba/b", "b/best"]


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

@app.get("/api/progress/<task_id>")
def get_progress(task_id):
    with progress_lock:
        state = task_progress.get(task_id)
    if state is None:
        return jsonify({"stage": "waiting", "percent": 0, "state": "waiting", "message": "等待后端接收任务"})
    return jsonify(state)

@app.get("/api/logs/<task_id>")
def get_task_logs(task_id):
    with progress_lock:
        lines = list(task_logs.get(task_id, []))
    return jsonify({"task_id": task_id, "logs": lines})


def cleanup_stale_tasks(now=None):
    """清理已就绪但超过保留期限、一直没有被取走的文件。"""
    now = now if now is not None else time.time()
    with progress_lock:
        stale = [
            (tid, dict(rec))
            for tid, rec in task_files.items()
            if now - rec.get("ready_at", 0) > READY_TTL_SECONDS
        ]
    for tid, rec in stale:
        with progress_lock:
            task_files.pop(tid, None)
        shutil.rmtree(rec.get("temp_dir", ""), ignore_errors=True)
        add_task_log(tid, f"文件超过 {READY_TTL_SECONDS} 秒未被取走，已清理", "INFO")


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
        if mode == "video":
            pitch = 0
        task_id = str(data.get("task_id", ""))[:80]
        if not task_id or not TASK_ID_RE.fullmatch(task_id):
            raise ValueError("任务编号无效，请刷新网页后重试。")
        quality = str(data.get("quality", "best"))
        if quality not in ("best", "1080", "720", "480", "360", "240"):
            raise ValueError("画质选项无效。")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not download_lock.acquire(blocking=False):
        return jsonify({"error": "当前有其他任务正在转换，请稍等片刻再试。"}), 429

    # 拿到锁说明没有正在处理的任务，顺手清理上一个任务遗留的过期文件
    cleanup_stale_tasks()

    temp_dir = tempfile.mkdtemp(prefix="bili-audio-")
    with progress_lock:
        task_logs[task_id] = []
        task_files.pop(task_id, None)
        task_progress[task_id] = {
            "stage": "排队", "percent": 0.0, "message": "任务已接收，等待处理", "state": "running"
        }
    add_task_log(task_id, f"任务开始：mode={mode}, pitch={pitch}, quality={quality}, url={url}")

    try:
        worker = threading.Thread(
            target=process_download,
            args=(task_id, url, mode, pitch, quality, temp_dir),
            daemon=True,
        )
        worker.start()
    except Exception as exc:
        download_lock.release()
        shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.exception("Failed to start worker thread")
        return jsonify({"error": f"后端任务启动失败：{exc}"}), 500

    # 立即返回；前端通过 /api/progress 轮询，完成后从 /api/file/<task_id> 原生下载
    return jsonify({"task_id": task_id, "status": "processing"}), 202


def process_download(task_id, url, mode, pitch, quality, temp_dir):
    """后台线程：下载 → 转码 → 登记可下载文件。全程不依赖请求上下文。"""

    def set_progress(stage, percent, message, state="running", speed=None, eta=None, **extra):
        payload = {"stage": stage, "percent": round(float(percent), 1), "message": message, "state": state}
        if speed:
            payload["speed"] = speed
        if eta is not None:
            payload["eta"] = eta
        payload.update(extra)
        with progress_lock:
            task_progress[task_id] = payload
        if state == "running":
            add_task_log(task_id, f"阶段={stage}, 进度={payload['percent']}%, {message}")

    def mark_error(message):
        with progress_lock:
            task_progress[task_id] = {
                "stage": "失败", "percent": 0.0, "message": message, "state": "error", "error": message
            }
        add_task_log(task_id, message, "ERROR")

    class TaskLogger:
        def debug(self, msg):
            if msg:
                add_task_log(task_id, msg, "DEBUG")
        def info(self, msg):
            if msg: add_task_log(task_id, msg, "INFO")
        def warning(self, msg):
            if msg: add_task_log(task_id, msg, "WARN")
        def error(self, msg):
            if msg: add_task_log(task_id, msg, "ERROR")

    def run_logged(args, check=True, timeout=SUBPROCESS_TIMEOUT, capture_output=True, text=True, **kwargs):
        started = time.monotonic()
        add_task_log(task_id, "执行命令：" + " ".join(str(x) for x in args))
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1, **kwargs)
        output_lines = []
        try:
            for line in proc.stdout:
                clean = line.rstrip()
                if clean:
                    output_lines.append(clean)
                    add_task_log(task_id, clean, "FFMPEG")
                if time.monotonic() - started > timeout:
                    proc.kill()
                    raise subprocess.TimeoutExpired(args, timeout, output="\n".join(output_lines))
            return_code = proc.wait(timeout=max(1, timeout - (time.monotonic() - started)))
        except Exception:
            if proc.poll() is None: proc.kill()
            proc.wait()
            raise
        output = "\n".join(output_lines)
        add_task_log(task_id, f"命令结束：returncode={return_code}, 用时={time.monotonic()-started:.1f}s")
        if check and return_code:
            raise subprocess.CalledProcessError(return_code, args, output=output, stderr=output)
        return subprocess.CompletedProcess(args, return_code, output, output)

    try:
        set_progress("解析链接", 1, "正在解析 B 站视频信息")
        def progress_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                percent = (done / total * 100) if total else 0
                if total and (not getattr(progress_hook, "last_log_percent", -10) or percent - getattr(progress_hook, "last_log_percent", -10) >= 5):
                    add_task_log(task_id, f"下载进度 {percent:.1f}% ({done}/{total} bytes)")
                    progress_hook.last_log_percent = percent
                speed = d.get("_speed_str")
                eta = d.get("eta")
                set_progress("下载源文件", min(percent, 99), f"源文件下载中：{percent:.1f}%", speed=speed, eta=eta)
            elif d.get("status") == "finished":
                set_progress("处理源文件", 100, "源文件下载完成，开始转换")
        opts = {
            "format": ("bestaudio/best" if mode == "audio" else (f"bv*[height<={quality}]+ba/b[height<={quality}]/bv*+ba/b" if quality != "best" else "bv*+ba/b")),
            "logger": TaskLogger(),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "ffmpeg_location": shutil.which("ffmpeg") or "/usr/bin/ffmpeg",
            "restrictfilenames": True,
            "quiet": False,
            "no_warnings": False,
            "verbose": True,
            "socket_timeout": 45,
            "retries": 8,
            "extractor_retries": 5,
            "ignore_no_formats_error": False,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "retry_sleep_functions": {"http": lambda n: min(2 * n, 15), "fragment": lambda n: min(2 * n, 15), "extractor": lambda n: min(2 * n, 15)},
            "concurrent_fragment_downloads": 1,
            "progress_hooks": [progress_hook],
            "max_filesize": MAX_OUTPUT_MB * 1024 * 1024,
            "match_filter": duration_filter,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com/",
            },
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            app.logger.info("Starting yt-dlp; host=%s; version=%s", urlparse(url).hostname, yt_dlp.version.__version__)
            try:
                info = ydl.extract_info(url, download=True)
            except yt_dlp.utils.DownloadError as first_error:
                # B 站 CDN 偶尔会对视频流返回 503（云端 IP 常见），同线路重试无效，换格式/线路再试
                if mode == "audio" or "503" not in str(first_error):
                    raise
                info = None
                last_error = first_error
                for fmt in video_fallback_formats(quality):
                    add_task_log(task_id, f"视频源 503，切换下载线路重试：{fmt}", "WARN")
                    set_progress("下载源文件", 1, "源线路不可用，正在切换线路重试")
                    for stale in os.listdir(temp_dir):
                        stale_path = os.path.join(temp_dir, stale)
                        if os.path.isfile(stale_path):
                            os.remove(stale_path)
                    try:
                        with yt_dlp.YoutubeDL(dict(opts, format=fmt)) as ydl_retry:
                            info = ydl_retry.extract_info(url, download=True)
                        break
                    except yt_dlp.utils.DownloadError as exc:
                        last_error = exc
                if info is None:
                    raise last_error
            set_progress("音视频转换", 100, "源文件下载完成，正在转换输出文件")
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

            # 视频类模式最终大小与源文件接近，先快速失败，避免转码十几分钟后才提示超限
            if mode != "audio" and os.path.getsize(source_path) > MAX_OUTPUT_MB * 1024 * 1024:
                raise RuntimeError(
                    f"源文件超过 {MAX_OUTPUT_MB} MB 上限，请降低视频画质（如 720p / 480p）后重试。"
                )

            if mode == "audio":
                set_progress("音频转换", 100, "正在生成 MP3" + (" 并调整音高" if pitch else ""))
                output_path = os.path.join(temp_dir, f"{safe_title}.mp3")
                encode_source = source_path
                if pitch:
                    wav_path = os.path.join(temp_dir, "audio-source.wav")
                    shifted_path = os.path.join(temp_dir, "audio-pitch-shifted.wav")
                    run_logged(
                        [ffmpeg, "-y", "-i", source_path, "-vn", "-map", "0:a:0", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s24le", wav_path],
                        check=True
                    )
                    rubberband = shutil.which("rubberband")
                    if not rubberband:
                        raise RuntimeError("升降调组件未安装，请等待后端重新部署完成。")
                    run_logged([rubberband, "-3", "-F", "-p", str(pitch), wav_path, shifted_path], check=True)
                    encode_source = shifted_path
                run_logged(
                    [ffmpeg, "-y", "-i", encode_source, "-vn", "-map", "0:a:0", "-ac", "2", "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3", output_path],
                    check=True
                )
                mime_type = "audio/mpeg"
            elif mode == "video":
                set_progress("封装视频", 100, "正在整理 MP4 文件")
                output_path = os.path.join(temp_dir, f"{safe_title}.mp4")
                if os.path.abspath(source_path) != os.path.abspath(output_path):
                    shutil.copyfile(source_path, output_path)
                mime_type = "video/mp4"
            else:
                set_progress("处理音视频", 100, "正在合成 MP4" + (" 并调整音高" if pitch else ""))
                if not pitch:
                    output_path = os.path.join(temp_dir, f"{safe_title}.mp4")
                    if os.path.abspath(source_path) != os.path.abspath(output_path):
                        shutil.copyfile(source_path, output_path)
                else:
                    wav_path = os.path.join(temp_dir, "audio-source.wav")
                    shifted_path = os.path.join(temp_dir, "audio-pitch-shifted.wav")
                    aac_path = os.path.join(temp_dir, "audio-pitch-shifted.m4a")
                    output_path = os.path.join(temp_dir, f"{safe_title}.mp4")
                    run_logged(
                        [ffmpeg, "-y", "-i", source_path, "-vn", "-map", "0:a:0",
                         "-ac", "2", "-ar", "48000", "-c:a", "pcm_s24le", wav_path],
                        check=True
                    )
                    rubberband = shutil.which("rubberband")
                    if not rubberband:
                        raise RuntimeError("升降调组件未安装，请等待后端重新部署完成。")
                    run_logged(
                        [rubberband, "-3", "-F", "-p", str(pitch), wav_path, shifted_path],
                        check=True
                    )
                    run_logged(
                        [ffmpeg, "-y", "-i", shifted_path, "-c:a", "aac", "-b:a", "320k", aac_path],
                        check=True
                    )
                    run_logged(
                        [ffmpeg, "-y", "-i", source_path, "-i", aac_path,
                         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "copy",
                         "-movflags", "+faststart", "-shortest", output_path],
                        check=True
                    )
                mime_type = "video/mp4"

            if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
                raise RuntimeError("没有生成有效的输出文件，请稍后重试。")
            if os.path.getsize(output_path) > MAX_OUTPUT_MB * 1024 * 1024:
                raise RuntimeError(f"文件超过 {MAX_OUTPUT_MB} MB 限制，请降低画质后重试。")

            filename = os.path.basename(output_path)
            with progress_lock:
                task_files[task_id] = {
                    "temp_dir": temp_dir,
                    "path": output_path,
                    "mime": mime_type,
                    "filename": filename,
                    "ready_at": time.time(),
                }
            set_progress(
                "完成", 100, "文件已生成，正在发送到浏览器",
                state="ready", filename=filename
            )
    except subprocess.TimeoutExpired as exc:
        message = f"服务端处理超时（超过 {SUBPROCESS_TIMEOUT} 秒），视频可能过大，请降低画质或改较短的视频后重试。"
        mark_error(message)
        add_task_log(task_id, str(exc), "ERROR")
        shutil.rmtree(temp_dir, ignore_errors=True)
    except yt_dlp.utils.DownloadError as exc:
        add_task_log(task_id, str(exc), "ERROR")
        shutil.rmtree(temp_dir, ignore_errors=True)
        raw_message = str(exc)
        if "Sign in" in raw_message or "login" in raw_message.lower() or "cookie" in raw_message.lower():
            message = "该视频可能需要登录或受到访问限制。本服务不会绕过登录或访问控制。"
        elif "503" in raw_message:
            message = "B 站视频源暂时拒绝服务（HTTP 503），已自动切换下载线路仍未成功。请稍后重试，或降低画质后再试。"
        else:
            message = "解析或下载失败：视频可能不可访问、接口发生变化，或免费服务资源不足。请稍后重试。"
        mark_error(message)
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        app.logger.exception("Audio extraction failed")
        mark_error(str(exc)[:300] or "处理失败")
    finally:
        try:
            download_lock.release()
        except RuntimeError:
            pass


@app.get("/api/file/<task_id>")
def get_result_file(task_id):
    if not TASK_ID_RE.fullmatch(task_id or ""):
        return jsonify({"error": "任务编号无效。"}), 400
    with progress_lock:
        record = dict(task_files.get(task_id) or {})
        state = dict(task_progress.get(task_id) or {})
    if record:
        path = record.get("path")
        if path and os.path.isfile(path):
            return send_file(
                path,
                mimetype=record.get("mime", "application/octet-stream"),
                as_attachment=True,
                download_name=record.get("filename", os.path.basename(path)),
                conditional=False,
            )
        return jsonify({"error": "文件已过期或已被清理，请重新发起下载。"}), 410
    if state.get("state") == "error":
        return jsonify({"error": state.get("error") or "任务处理失败。"}), 410
    if state:
        return jsonify({"error": "文件尚未处理完成，请稍候在任务完成后再下载。"}), 409
    return jsonify({"error": "任务不存在，请刷新页面后重新提交。"}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
