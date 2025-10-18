import asyncio
import aiohttp
from aiohttp import web, ClientTimeout
import yt_dlp
import traceback
import shutil
import os
import logging
from urllib.parse import quote_plus
import collections
import re
import html
import random
import mimetypes

# basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Config
HOST = "0.0.0.0"
PORT = 5000
CHUNK_SIZE = 64 * 1024
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# ---------------- yt-dlp helpers (run in thread) ----------------
def _ydl_extract_info(url, opts=None):
    opts = opts or {
        "quiet": True,
        "skip_download": True,
        "dump_single_json": True,
        "http_headers": {"User-Agent": USER_AGENT},
        "socket_timeout": 10,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def search_youtube_music_sync(query, max_results=20):
    # use non-flat extraction so yt-dlp returns thumbnails/uploader/title reliably
    expr = f"ytsearch{max_results}:{query}"
    opts = {
        "quiet": True,
        "dump_single_json": True,
        # do not use extract_flat here so yt-dlp fetches metadata including thumbnails
        "http_headers": {"User-Agent": USER_AGENT},
        "socket_timeout": 10,
    }
    results = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(expr, download=False)
        entries = data.get("entries") or []
        for entry in entries:
            # prefer canonical webpage_url or id/url fields
            vid = entry.get("id") or entry.get("url") or entry.get("webpage_url") or ""
            # normalize to just the video id if possible
            m = re.search(r"[?&]v=([^&]+)", str(vid))
            vid_only = m.group(1) if m else (str(vid).strip() if vid and not str(vid).startswith("http") else "")
            # thumbnail: try yt-dlp thumbnails, fall back to ytimg constructed URL
            thumb = ""
            try:
                if entry.get("thumbnails"):
                    # pick best (last) thumbnail if available
                    thumb = entry["thumbnails"][-1].get("url", "") or ""
            except Exception:
                thumb = ""
            if not thumb and vid_only:
                thumb = f"https://i.ytimg.com/vi/{vid_only}/hqdefault.jpg"
            # author/uploader
            author = entry.get("uploader") or entry.get("artist") or entry.get("channel") or ""
            results.append({
                "title": entry.get("title", "Unknown"),
                "videoId": vid_only or vid,
                "thumbnail": thumb,
                "author": author
            })
    return results

def choose_best_audio_format(info):
    formats = info.get("formats", []) or []
    for fmt in formats:
        acodec = fmt.get("acodec")
        if acodec and acodec.lower() == "opus" and fmt.get("url"):
            return fmt
    for fmt in formats:
        if fmt.get("acodec") and fmt.get("acodec").lower() in ("aac", "mp4a", "vorbis") and fmt.get("url"):
            return fmt
    for fmt in formats:
        if fmt.get("url") and (fmt.get("vcodec") == "none" or fmt.get("acodec")):
            return fmt
    return formats[-1] if formats else None

async def extract_info_async(url):
    return await asyncio.to_thread(_ydl_extract_info, url)

async def search_async(query, max_results=20):
    return await asyncio.to_thread(search_youtube_music_sync, query, max_results)

# ---------------- BackendPlayer (websocket broadcast) ----------------
class BackendPlayer:
    def __init__(self):
        self._task = None
        self._pause = asyncio.Event()
        self._pause.set()   # set => not paused (unused for buffering now)
        self._stop_event = asyncio.Event()
        self._clients = set()  # websocket clients
        self._current_upstream_url = None
        self._lock = asyncio.Lock()
        self._content_type = "audio/mpeg"
        self._ffmpeg_proc = None

        # rolling in-memory buffer (store recent chunks)
        # store chunks as bytes objects; maxlen controls how much to keep
        self._buffer_chunks = collections.deque(maxlen=1024)  # tune size (number of chunks)
        self._buffer_bytes = 0
        self._buffer_max_bytes = 10 * 1024 * 1024  # ~10MB default max
        self._broadcast_enabled = True  # when False, producer buffers but doesn't broadcast

    async def start_stream(self, video_url, start_time: float = None):
        async with self._lock:
            logging.info("start_stream requested: %s", video_url)
            # If same upstream already producing, keep it running and reuse buffer
            if self._task and self._current_upstream_url == video_url:
                logging.info("start_stream: upstream already running, reusing buffer")
                # ensure broadcasting enabled so new clients will receive live chunks after catch-up
                self._broadcast_enabled = True
                return
            # otherwise stop any existing producer (hard) and start new
            await self.stop_stream(hard=True)
            try:
                info = await extract_info_async(video_url)
                fmt = choose_best_audio_format(info)
                if not fmt or not fmt.get("url"):
                    raise RuntimeError("No suitable upstream format found")
                self._current_upstream_url = fmt["url"]

                # prefer server mpv for PCM/WAV if available (advertise audio/wav)
                if shutil.which("mpv"):
                    self._content_type = "audio/wav"
                else:
                    if fmt.get("acodec") == "opus":
                        self._content_type = "audio/ogg"
                    elif fmt.get("ext"):
                        ext = fmt.get("ext")
                        if ext in ("m4a", "mp4", "aac"):
                            self._content_type = "audio/mp4"
                        elif ext in ("mp3",):
                            self._content_type = "audio/mpeg"
                        else:
                            self._content_type = "application/octet-stream"
                logging.info("Chosen upstream URL (truncated): %s..., content_type=%s",
                             (self._current_upstream_url[:120] + "...") if self._current_upstream_url else None,
                             self._content_type)
            except Exception:
                logging.exception("Failed to extract upstream URL")
                raise
            self._pause.set()
            self._stop_event.clear()
            # reset buffer for new upstream
            self._buffer_chunks.clear()
            self._buffer_bytes = 0
            # if a start_time was provided and ffmpeg exists, use ffmpeg to seek
            if start_time is not None and shutil.which("ffmpeg"):
                self._content_type = "audio/wav"
                self._task = asyncio.create_task(self._producer_task_ffmpeg(self._current_upstream_url, start_time))
            else:
                self._task = asyncio.create_task(self._producer_task(self._current_upstream_url))

    async def _producer_task(self, upstream_url):
        logging.info("producer task starting for upstream: %s", upstream_url)
        timeout = ClientTimeout(total=None, sock_read=30)
        headers = {"User-Agent": USER_AGENT}

        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
                    async with sess.get(upstream_url) as resp:
                        if resp.status != 200:
                            logging.error("Upstream returned status %s", resp.status)
                            return
                        chunk_count = 0
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            # always append to local rolling buffer
                            if chunk:
                                # append chunk and maintain byte accounting
                                self._buffer_chunks.append(chunk)
                                self._buffer_bytes += len(chunk)
                                # trim oldest chunks if exceed max bytes
                                while self._buffer_bytes > self._buffer_max_bytes and self._buffer_chunks:
                                    old = self._buffer_chunks.popleft()
                                    try:
                                        self._buffer_bytes -= len(old)
                                    except Exception:
                                        self._buffer_bytes = max(0, self._buffer_bytes - (len(old) if old else 0))
                                chunk_count += 1
                                # broadcast to connected clients only when broadcasting enabled
                                if self._broadcast_enabled:
                                    await self._broadcast(chunk)
                                if chunk_count % 100 == 0:
                                    logging.info("buffered/broadcasted %d chunks, clients=%d buffer_bytes=%d", chunk_count, len(self._clients), self._buffer_bytes)
                            await asyncio.sleep(0)

                logging.info("upstream finished normally")
                break

            except (aiohttp.client_exceptions.ClientPayloadError,
                    aiohttp.client_exceptions.ClientConnectionError,
                    ConnectionResetError,
                    asyncio.IncompleteReadError) as e:
                logging.warning("Upstream connection error (attempt %d): %s", attempt, e)
                if self._stop_event.is_set():
                    break
                backoff = min(5, 0.5 * attempt)
                await asyncio.sleep(backoff)
                logging.info("Retrying upstream fetch (attempt %d)...", attempt + 1)
                continue

            except asyncio.CancelledError:
                logging.info("producer task cancelled")
                break

            except Exception:
                logging.exception("producer task error")
                break

        logging.info("producer task finished")
        self._task = None
        self._current_upstream_url = None

    async def _producer_task_ffmpeg(self, upstream_url, start_time: float):
        """Spawn ffmpeg to read upstream_url and start at start_time, stream stdout to clients."""
        logging.info("producer task (ffmpeg) starting for upstream: %s start=%s", upstream_url, start_time)
        # seek AFTER input (-i) for better accuracy on many HTTP inputs
        args = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-i", upstream_url,
            "-ss", str(start_time),
            "-vn",
            "-ac", "2",
            "-ar", "44100",
            "-f", "wav",
            "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self._ffmpeg_proc = proc
            while not self._stop_event.is_set():
                chunk = await proc.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                # buffer ffmpeg output as well
                self._buffer_chunks.append(chunk)
                self._buffer_bytes += len(chunk)
                while self._buffer_bytes > self._buffer_max_bytes and self._buffer_chunks:
                    old = self._buffer_chunks.popleft()
                    try:
                        self._buffer_bytes -= len(old)
                    except Exception:
                        self._buffer_bytes = max(0, self._buffer_bytes - (len(old) if old else 0))
                if self._broadcast_enabled:
                    await self._broadcast(chunk)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            logging.info("ffmpeg producer cancelled")
            try:
                if self._ffmpeg_proc:
                    self._ffmpeg_proc.kill()
            except Exception:
                pass
        except Exception:
            logging.exception("ffmpeg producer error")
        finally:
            self._ffmpeg_proc = None
            logging.info("producer task (ffmpeg) finished")
            self._task = None
            self._current_upstream_url = None

    async def _broadcast(self, chunk):
        if not self._clients:
            return
        to_remove = []
        for ws in set(self._clients):
            try:
                await ws.send_bytes(chunk)
            except Exception:
                logging.warning("client send failed, removing client")
                to_remove.append(ws)
        for r in to_remove:
            self._clients.discard(r)

    async def _broadcast_text(self, text: str):
        """Send a short text control message to all connected websocket clients."""
        if not self._clients:
            return
        to_remove = []
        for ws in set(self._clients):
            try:
                await ws.send_str(text)
            except Exception:
                logging.warning("client send_str failed, removing client")
                to_remove.append(ws)
        for r in to_remove:
            self._clients.discard(r)

    async def _send_buffer_to_client(self, ws):
        """Send current buffer contents to a newly connected client for quick catch-up."""
        try:
            # we iterate over a snapshot to avoid blocking producer
            snapshot = list(self._buffer_chunks)
            for chunk in snapshot:
                await ws.send_bytes(chunk)
        except Exception:
            logging.exception("failed sending buffer to client")
            try:
                await ws.close()
            except Exception:
                pass

    async def stop_stream(self, hard: bool = False):
        logging.info("stop_stream requested")
        # soft stop: stop broadcasting and disconnect clients but keep upstream fetching to preserve buffer
        if not hard:
            logging.info("soft stop: keep upstream running, clear clients and disable broadcast")
            self._broadcast_enabled = False
            try:
                await self._broadcast_text("stop")
            except Exception:
                pass
            for ws in list(self._clients):
                try:
                    await ws.close()
                except Exception:
                    pass
            self._clients.clear()
            return

        # hard stop: cancel producer and free resources
        self._stop_event.set()
        self._pause.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        try:
            if self._ffmpeg_proc and self._ffmpeg_proc.returncode is None:
                self._ffmpeg_proc.kill()
        except Exception:
            pass
        try:
            await self._broadcast_text("stop")
        except Exception:
            pass
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        self._current_upstream_url = None
        # reset buffer
        self._buffer_chunks.clear()
        self._buffer_bytes = 0
        self._broadcast_enabled = True

    async def pause(self):
        logging.info("pause requested")
        self._pause.clear()
        try:
            await self._broadcast_text("pause")
        except Exception:
            pass

    async def resume(self):
        logging.info("resume requested")
        self._pause.set()
        try:
            await self._broadcast_text("resume")
        except Exception:
            pass

    def add_client(self, ws):
        # send buffer first, then register client for live broadcasts
        asyncio.create_task(self._send_buffer_to_client(ws))
        self._clients.add(ws)
        logging.info("client connected (buffer sent), total clients=%d", len(self._clients))

    def remove_client(self, ws):
        self._clients.discard(ws)
        logging.info("client disconnected, total clients=%d", len(self._clients))

PLAYER = BackendPlayer()

# ---------------- HTTP / WS handlers ----------------
routes = web.RouteTableDef()

@routes.get("/search")
async def handle_search(request):
    q = request.query.get("query", "")
    if not q:
        return web.json_response([])
    try:
        results = await search_async(q, max_results=20)
        # rewrite thumbnail URLs to point at backend /thumbnail proxy
        base = f"{request.scheme}://{request.host}"
        out = []
        for r in (results or []):
            vid = r.get("videoId") or r.get("id") or r.get("url") or r.get("webpage_url") or ""
            vid_only = _extract_video_id(vid) or ""
            thumb_url = f"{base}/thumbnail?videoId={quote_plus(vid_only)}" if vid_only else r.get("thumbnail", "")
            out.append({
                "title": r.get("title", r.get("name", "")),
                "videoId": vid_only or vid,
                "thumbnail": thumb_url,
                "author": r.get("uploader") or r.get("artist") or r.get("channel", "")
            })
        return web.json_response(out)
    except Exception:
        logging.exception("search failed")
        return web.json_response({"error": "search failed", "trace": traceback.format_exc()}, status=500)

def _extract_video_id(s):
    if not s:
        return ""
    m = re.search(r"[?&]v=([^&]+)", str(s))
    if m:
        return m.group(1)
    # if it's already an id-like string, return it
    s = str(s).strip()
    if s and not s.startswith("http"):
        return s
    return ""

@routes.get("/recommendations")
async def handle_recommendations(request):
    video_id = request.query.get("videoId") or request.query.get("video_url") or ""
    title = request.query.get("title", "")
    recs = []
    # helper to build thumbnail url when only id available
    def thumb_for_vid(vid):
        if not vid:
            return ""
        base = f"{request.scheme}://{request.host}"
        return f"{base}/thumbnail?videoId={quote_plus(vid)}"

    # Build a base title (prefer provided title, else try to extract)
    base_title = title or ""
    if not base_title and video_id:
        try:
            info = await extract_info_async(video_id)
            base_title = info.get("title", "") if isinstance(info, dict) else ""
        except Exception:
            base_title = ""

    if not base_title:
        # nothing to base recommendations on
        return web.json_response([])

    # tokenise words from base title, fallback to whole string
    words = re.findall(r"\w+", base_title)
    if not words:
        words = base_title.split()

    used_ids = set()
    # generate up to N recommendations by searching random substrings
    TARGET = 8
    attempts = 0
    while len(recs) < TARGET and attempts < TARGET * 4:
        attempts += 1
        # pick a random snippet length and start position
        k = random.randint(1, min(3, max(1, len(words))))
        start = random.randint(0, max(0, len(words) - k))
        q = " ".join(words[start:start + k]).strip()
        if not q:
            continue
        try:
            # run yt-dlp search synchronously in thread to avoid blocking event loop
            results = await asyncio.to_thread(search_youtube_music_sync, q, 1)
            if not results:
                continue
            first = results[0]
            vid = first.get("videoId") or first.get("url") or first.get("id") or ""
            if not vid:
                continue
            # normalize id (strip watch?v= if present)
            vid_only = _extract_video_id(vid)
            if not vid_only or vid_only in used_ids:
                continue
            used_ids.add(vid_only)
            thumb = first.get("thumbnail") or thumb_for_vid(vid_only)
            author = first.get("uploader") or first.get("artist") or first.get("channel") or ""
            # ensure thumbnail points to backend proxy
            if thumb and not thumb.startswith(f"{request.scheme}://{request.host}"):
                thumb = thumb_for_vid(vid_only)
            recs.append({
                "title": first.get("title") or q,
                "videoId": vid_only,
                "thumbnail": thumb,
                "author": author
            })
        except Exception:
            logging.exception("recommendation search failed for query: %s", q)
            continue

    return web.json_response(recs)

@routes.post("/control")
async def handle_control(request):
    data = await request.json()
    action = data.get("action")
    if action == "play":
        video_url = data.get("video_url")
        start_time = data.get("start_time", None)
        # coerce numeric if provided
        try:
            if start_time is not None:
                start_time = float(start_time)
        except Exception:
            start_time = None
        if not video_url:
            return web.json_response({"error": "video_url required for play"}, status=400)
        try:
            await PLAYER.start_stream(video_url, start_time=start_time)
            return web.json_response({"status": "playing", "content_type": PLAYER._content_type})
        except Exception as e:
            logging.exception("control play failed")
            return web.json_response({"error": str(e)}, status=500)
    elif action == "pause":
        await PLAYER.pause()
        return web.json_response({"status": "paused"})
    elif action == "resume":
        await PLAYER.resume()
        return web.json_response({"status": "resumed"})
    elif action == "stop":
        # accept optional boolean "hard" flag; default is soft stop
        hard = bool(data.get("hard", False))
        await PLAYER.stop_stream(hard=hard)
        return web.json_response({"status": "stopped", "hard": hard})
    else:
        return web.json_response({"error": "unknown action"}, status=400)

@routes.get("/ws_audio")
async def ws_audio(request):
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    PLAYER.add_client(ws)
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                if msg.data == "pause":
                    await PLAYER.pause()
                elif msg.data == "resume":
                    await PLAYER.resume()
                elif msg.data == "stop":
                    await PLAYER.stop_stream()
            elif msg.type in (web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                break
    finally:
        PLAYER.remove_client(ws)
    return ws

@routes.get("/proxy_stream")
async def proxy_stream(request):
    video_id = request.query.get("videoId") or request.query.get("video_url") or ""
    if not video_id:
        return web.json_response({"error": "videoId or video_url parameter required"}, status=400)
    try:
        info = await extract_info_async(video_id)
    except Exception as e:
        return web.json_response({"error": "failed to extract info", "detail": str(e)}, status=500)
    fmt = choose_best_audio_format(info)
    if not fmt or not fmt.get("url"):
        return web.json_response({"error": "no suitable audio format found"}, status=404)
    upstream_url = fmt["url"]

    timeout = ClientTimeout(total=None, sock_read=30)
    headers = {"User-Agent": USER_AGENT}
    content_type = "audio/mpeg"
    if fmt.get("acodec") == "opus":
        content_type = "audio/ogg"
    elif fmt.get("ext") in ("m4a", "mp4", "aac"):
        content_type = "audio/mp4"
    elif fmt.get("ext") == "mp3":
        content_type = "audio/mpeg"

    sr = web.StreamResponse(status=200, headers={"Content-Type": content_type})
    await sr.prepare(request)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
            async with sess.get(upstream_url) as resp:
                if resp.status != 200:
                    logging.error("proxy upstream returned %s", resp.status)
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    if chunk:
                        await sr.write(chunk)
        await sr.write_eof()
    except Exception:
        logging.exception("proxy_stream error")
        try:
            await sr.write_eof()
        except Exception:
            pass
    return sr

@routes.get("/thumbnail")
async def thumbnail_proxy(request):
    """
    Proxy thumbnail fetch through backend. Accepts videoId or url.
    Returns raw image bytes with appropriate Content-Type.
    """
    vid = request.query.get("videoId") or request.query.get("video_id") or request.query.get("url") or ""
    if not vid:
        return web.json_response({"error": "videoId required"}, status=400)
    vid_only = _extract_video_id(vid)
    # try to extract thumbnail via yt-dlp metadata first
    thumb_url = None
    try:
        info = await extract_info_async(vid_only or vid)
        if isinstance(info, dict):
            thumbs = info.get("thumbnails") or []
            if thumbs:
                # prefer last (best) but will be proxied through backend
                thumb_url = thumbs[-1].get("url")
    except Exception:
        pass

    if not thumb_url and vid_only:
        thumb_url = f"https://i.ytimg.com/vi/{vid_only}/hqdefault.jpg"

    if not thumb_url:
        return web.json_response({"error": "no thumbnail url"}, status=404)

    timeout = ClientTimeout(total=10)
    headers = {"User-Agent": USER_AGENT}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as sess:
            async with sess.get(thumb_url) as resp:
                if resp.status != 200:
                    logging.warning("thumbnail upstream returned %s for %s", resp.status, thumb_url)
                    return web.json_response({"error": "failed fetching thumbnail"}, status=502)
                data = await resp.read()
                ctype = resp.headers.get("Content-Type") or mimetypes.guess_type(thumb_url)[0] or "image/jpeg"
                return web.Response(body=data, content_type=ctype, headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        logging.exception("thumbnail proxy error")
        return web.json_response({"error": "thumbnail fetch error"}, status=502)

@routes.get("/health")
async def health(request):
    return web.json_response({"ok": True})

app = web.Application()
app.add_routes(routes)

if __name__ == "__main__":
    logging.info("Starting backend on %s:%d", HOST, PORT)
    web.run_app(app, host=HOST, port=PORT)