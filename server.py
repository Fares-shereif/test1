"""YouTube audio API. Run with: python server.py."""
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
import urllib.error
import urllib.request

import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(version="1.2.0")
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "HEAD"],
    allow_headers=["*"], expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@lru_cache(maxsize=1)
def runtime_status():
    """Public, non-sensitive runtime versions; never expose paths or environment values."""
    runtimes = {}
    for name, minimum in (("node", (22, 0, 0)), ("deno", (2, 3, 0))):
        executable = shutil.which(name)
        installed_version = None
        supported = False
        if executable:
            try:
                result = subprocess.run(
                    [executable, "--version"], capture_output=True, text=True,
                    timeout=3, check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
                if match:
                    installed_version = match.group(0)
                    supported = tuple(map(int, match.groups())) >= minimum
            except (OSError, subprocess.SubprocessError):
                pass
        runtimes[name] = {"version": installed_version, "supported": supported}
    try:
        ejs = version("yt-dlp-ejs")
    except PackageNotFoundError:
        ejs = None
    return {"yt_dlp": version("yt-dlp"), "ejs": ejs, "javascript": runtimes}


def extraction_error(exc):
    """Classify observed failures without returning raw exceptions, tokens or URLs."""
    message = str(exc).lower()
    if "confirm you're not a bot" in message or "confirm you’re not a bot" in message:
        return "YOUTUBE_VERIFICATION_REQUIRED", "The audio provider requires verification from this server."
    if any(text in message for text in ("javascript runtime", "js runtime", "challenge solver", "ejs component")):
        return "JS_RUNTIME_ERROR", "Check the supported JavaScript runtime and yt-dlp-ejs installation on the server."
    if "requested format is not available" in message or "no playable audio format" in message:
        return "AUDIO_FORMAT_UNAVAILABLE", "No compatible audio format was returned. Check runtime versions and provider restrictions."
    if any(text in message for text in ("private video", "video unavailable", "not available in your country", "age-restricted", "confirm your age", "login required")):
        return "VIDEO_RESTRICTED", "The audio provider reports that this video is restricted or unavailable."
    if "timed out" in message or "timeout" in message:
        return "UPSTREAM_TIMEOUT", "The server timed out while connecting to the audio provider."
    if "429" in message or "too many requests" in message:
        return "UPSTREAM_RATE_LIMITED", "The audio provider is rate-limiting this server."
    return "EXTRACTION_FAILED", "Audio extraction failed. Match the error_id with the server logs for the underlying exception."


@app.exception_handler(HTTPException)
async def api_exception_handler(request, exc):
    if isinstance(exc.detail, dict) and "error_code" in exc.detail:
        # Keep detail a string for existing clients; add structured diagnostics alongside it.
        return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


def validate_id(video_id):
    if not VIDEO_ID.fullmatch(video_id):
        raise HTTPException(400, "Invalid YouTube video ID")


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok", "api_version": app.version, "runtime": runtime_status()}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/search")
def search_youtube(q: str = Query(..., min_length=1, max_length=300)):
    query = q.strip()
    if not query:
        raise HTTPException(400, "Enter a search term")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "noplaylist": True, "socket_timeout": 20}) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query}", download=False)
        return [
            {"id": e["id"], "title": e.get("title") or "YouTube",
             "channel": e.get("uploader") or e.get("channel") or "YouTube",
             "duration": e.get("duration"),
             "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"}
            for e in (info or {}).get("entries", [])
            if e and VIDEO_ID.fullmatch(e.get("id", ""))
        ]
    except Exception as exc:
        logger.exception("YouTube search failed")
        raise HTTPException(502, "YouTube search failed. Check the server connection and update yt-dlp.") from exc


@lru_cache(maxsize=128)
def extract_audio(video_id, saver, time_bucket):
    # Prefer browser-compatible progressive M4A; avoid manifest-only streams.
    quality = "worstaudio" if saver else "bestaudio"
    options = {
        "format": f"{quality}[ext=m4a][protocol=https]/{quality}[protocol=https]",
        "noplaylist": True, "quiet": True, "socket_timeout": 20,
        "js_runtimes": {
            name: {} for name, runtime in runtime_status()["javascript"].items()
            if runtime["supported"]
        },
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info or not info.get("url"):
            raise ValueError("No playable audio format")
        return info
    except Exception as exc:
        error_id = uuid.uuid4().hex[:12]
        error_code, detail = extraction_error(exc)
        logger.exception("Audio extraction failed id=%s video=%s code=%s", error_id, video_id, error_code)
        raise HTTPException(502, {
            "detail": detail, "error_code": error_code,
            "error_id": error_id, "runtime": runtime_status(),
        }) from exc


def audio_info(video_id, saver):
    validate_id(video_id)
    return extract_audio(video_id, saver, int(time.time() // 120))


@app.get("/stream/{video_id}")
def get_audio_stream(video_id: str, saver: bool = True):
    info = audio_info(video_id, saver)
    return {
        "audio_url": f"/audio/{video_id}?saver={str(saver).lower()}",
        "title": info.get("title"), "ext": info.get("ext", "m4a"),
        "filesize_mb": round((info.get("filesize") or info.get("filesize_approx") or 0) / (1024 * 1024), 2),
    }


@app.api_route("/audio/{video_id}", methods=["GET", "HEAD"])
@app.api_route("/download/{video_id}", methods=["GET", "HEAD"])
def proxy_audio(video_id: str, request: Request, saver: bool = True):
    info = audio_info(video_id, saver)
    headers = dict(info.get("http_headers") or {})
    headers.setdefault("User-Agent", "Mozilla/5.0")
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    upstream_request = urllib.request.Request(info["url"], headers=headers, method=request.method)
    try:
        upstream = urllib.request.urlopen(upstream_request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            response_headers = {}
            if exc.headers.get("Content-Range"):
                response_headers["Content-Range"] = exc.headers["Content-Range"]
            exc.close()
            raise HTTPException(416, "Requested range is unavailable", headers=response_headers) from exc
        exc.close()
        extract_audio.cache_clear()
        raise HTTPException(502, "Audio provider refused the stream. Try playing the track again.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(502, "Cannot connect to the audio provider") from exc

    response_headers = {
        name: upstream.headers[name]
        for name in ("Content-Length", "Content-Range", "Accept-Ranges")
        if upstream.headers.get(name)
    }
    media_type = upstream.headers.get("Content-Type") or ("audio/mp4" if info.get("ext") == "m4a" else "audio/webm")
    status = upstream.status

    def chunks():
        try:
            if request.method != "HEAD":
                while chunk := upstream.read(64 * 1024):
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(chunks(), status_code=status, headers=response_headers, media_type=media_type)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
