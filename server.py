"""YouTube audio API. Run with: python server.py."""
import logging
import re
import shutil
import time
from functools import lru_cache
import urllib.error
import urllib.request

import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI()
logger = logging.getLogger(__name__)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "HEAD"],
    allow_headers=["*"], expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def validate_id(video_id):
    if not VIDEO_ID.fullmatch(video_id):
        raise HTTPException(400, "Invalid YouTube video ID")


@app.get("/health")
def health():
    return {"status": "ok"}


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
        "js_runtimes": {"node": {}} if shutil.which("node") else {},
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        if not info or not info.get("url"):
            raise ValueError("No playable audio format")
        return info
    except Exception as exc:
        logger.exception("Audio extraction failed for %s", video_id)
        raise HTTPException(502, "Cannot extract this audio. Update yt-dlp; the video may be restricted or unavailable.") from exc


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
    uvicorn.run(app, host="0.0.0.0", port=8000)
