import threading
import asyncio
import aiohttp
from aiohttp import web
import socket
import time
import requests
import mpv
from PyQt5.QtGui import QPixmap, QPainter, QTransform, QRegion
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLabel, QHBoxLayout, QSlider
from PyQt5.QtCore import QTimer, Qt
import re

# Local server config (random free port)
def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

class VinylLabel(QLabel):
    """Circular thumbnail that can spin. Paints rotated pixmap and keeps a circular mask."""
    def __init__(self, size=160, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._orig_pixmap = None
        self._pixmap = None
        self._angle = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)  # ~25 FPS rotation
        self._timer.timeout.connect(self._advance)
        self.setScaledContents(False)
        # ensure initial circular mask
        region = QRegion(self.rect(), QRegion.Ellipse)
        self.setMask(region)

    def set_vinyl_pixmap(self, pixmap: QPixmap):
        if pixmap is None:
            self._orig_pixmap = None
            self._pixmap = None
            self.update()
            return
        # keep original for clean rescale later
        self._orig_pixmap = pixmap
        self._pixmap = self._orig_pixmap.scaled(self.width(), self.height(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        self.update()

    def _advance(self):
        self._angle = (self._angle + 2.5) % 360.0
        self.update()

    def start_spin(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop_spin(self):
        if self._timer.isActive():
            self._timer.stop()
            # keep whatever angle currently set

    def paintEvent(self, ev):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        # draw circular background (vinyl ring)
        rect = self.rect()
        # draw ring (outer subtle gradient effect could be added)
        painter.setBrush(Qt.black)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect)

        if self._pixmap:
            # rotate painter about center and draw pixmap clipped to circle
            painter.save()
            cx = rect.center().x()
            cy = rect.center().y()
            painter.translate(cx, cy)
            painter.rotate(self._angle)
            painter.translate(-cx, -cy)
            # create circular clip
            path = painter.clipPath()
            painter.setClipRegion(self.mask())  # ensure circular mask
            # draw pixmap centered
            pix = self._pixmap
            px = (rect.width() - pix.width()) // 2
            py = (rect.height() - pix.height()) // 2
            painter.drawPixmap(px, py, pix)
            painter.restore()

        # draw a subtle inner circle to simulate label center
        painter.setBrush(Qt.gray)
        margin = int(self.width() * 0.18)
        inner = rect.adjusted(margin, margin, -margin, -margin)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(inner)

    def resizeEvent(self, ev):
        # update mask to always be circular
        r = self.rect()
        region = QRegion(r, QRegion.Ellipse)
        self.setMask(region)
        if self._orig_pixmap:
            # rescale from original pixmap to avoid quality loss after multiple resizes
            self._pixmap = self._orig_pixmap.scaled(self.width(), self.height(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        super().resizeEvent(ev)


class MusicPlayer(QWidget):
    def __init__(self, backend_url="http://127.0.0.1:5000", progress=None):
        super().__init__()
        self.setWindowTitle("Music Player")
        self.layout = QVBoxLayout(self)

        # Vinyl thumbnail above controls
        self.vinyl = VinylLabel(size=160)
        self.layout.addWidget(self.vinyl, alignment=Qt.AlignCenter)

        # Controls layout: Play / Pause / Stop
        ctrl_layout = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.play_clicked)
        ctrl_layout.addWidget(self.play_button)

        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self.pause_clicked)
        ctrl_layout.addWidget(self.pause_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_clicked)
        ctrl_layout.addWidget(self.stop_button)

        self.layout.addLayout(ctrl_layout)

        # seek bar (needs a fucking control or something)
        seek_layout = QHBoxLayout()
        self.seek_label = QLabel("00:00 / 00:00")
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        seek_layout.addWidget(self.seek_slider)
        seek_layout.addWidget(self.seek_label)
        self.layout.addLayout(seek_layout)

        # volume slider thingy
        vol_layout = QHBoxLayout()
        self.vol_label = QLabel("Vol")
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        vol_layout.addWidget(self.vol_label)
        vol_layout.addWidget(self.vol_slider)
        self.layout.addLayout(vol_layout)

        self.status_label = QLabel("Status: Stopped")
        self.layout.addWidget(self.status_label)

        self.backend_url = backend_url.rstrip("/")
        self.progress = progress  # QProgressBar from main window (optional)

        # local HTTP server port (queue will be created inside the background loop)
        self._local_port = find_free_port()
        self._ws_task = None
        # connection flag to know when ws is live
        self._ws_connected = False
        self._local_app = web.Application()
        self._local_app.router.add_get("/local_stream", self._local_stream_handler)
        self._runner = web.AppRunner(self._local_app)

        # background event loop + aiohttp site
        self._loop = asyncio.new_event_loop()
        self._loop_running = False
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()

        # wait for loop to be ready, then create the asyncio.Queue inside that loop
        start_wait_deadline = time.time() + 3.0
        while not self._loop_running and time.time() < start_wait_deadline:
            time.sleep(0.01)
        if not self._loop_running:
            raise RuntimeError("Local event loop failed to start")
        fut = asyncio.run_coroutine_threadsafe(self._create_queue(), self._loop)
        try:
            self._queue = fut.result(timeout=2.0)
        except Exception as e:
            raise RuntimeError(f"Failed to create local queue: {e}")

        # create mpv instance (client mpv)
        self._mpv = mpv.MPV(ytdl=False, input_default_bindings=True)
        # set initial volume
        try:
            self._mpv.volume = self.vol_slider.value()
        except Exception:
            pass

        # Poll mpv and update seek slider
        self._mpv_event_timer = QTimer(self)
        self._mpv_event_timer.setInterval(500)
        self._mpv_event_timer.timeout.connect(self._poll_mpv)
        self._mpv_event_timer.start()

        self._current_video_url = None
        self._current_video_id = None
        self._playing = False
        self._backend_content_type = "audio/mpeg"

        # paused state (True when paused, False otherwise)
        self._paused = False

        # callbacks
        self.on_track_end = None

        # track-end helper
        self._last_time_pos = 0.0
        self._track_end_triggered = False

        # reconnection/backoff state
        self._ws_reconnect_attempts = 0
        self._ws_reconnect_max = 6

    async def _create_queue(self):
        # must be executed inside the background event loop
        self._queue = asyncio.Queue()
        return self._queue

    # ----------------- local aiohttp server loop -----------------
    def _start_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "127.0.0.1", self._local_port)
        self._loop.run_until_complete(site.start())
        # signal ready
        self._loop_running = True
        # keep running forever
        self._loop.run_forever()

    async def _local_stream_handler(self, request):
        """
        Serve bytes from self._queue as an HTTP chunked response.
        mpv will request this endpoint and play continuously.
        """
        content_type = getattr(self, "_backend_content_type", "audio/mpeg")
        resp = web.StreamResponse(status=200, reason='OK', headers={
            'Content-Type': content_type
        })
        await resp.prepare(request)
        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    break
                await resp.write(chunk)
        except Exception:
            pass
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    # ----------------- websocket receiver + reconnection -----------------
    async def _ws_receiver(self, ws_url):
        """
        Connect to backend websocket and push binary frames into local queue.
        Reconnect automatically on transient errors.
        """
        self._ws_reconnect_attempts = 0
        while not self._stop_requested():
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(ws_url, heartbeat=30) as ws:
                        self._ws_reconnect_attempts = 0
                        # mark websocket connected for the main thread
                        self._ws_connected = True
                        QTimer.singleShot(0, lambda: self._set_status("WS connected"))

                        # feed incoming binary frames into local queue
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                await self._queue.put(msg.data)
                            elif msg.type == aiohttp.WSMsgType.TEXT:
                                # immediate remote-control instructions from server
                                try:
                                    txt = msg.data.strip().lower()
                                    if txt == "pause":
                                        # pause mpv on main thread
                                        QTimer.singleShot(0, lambda: setattr(self._mpv, "pause", True))
                                        QTimer.singleShot(0, lambda: self._set_status("Paused (remote)"))
                                    elif txt == "resume":
                                        QTimer.singleShot(0, lambda: setattr(self._mpv, "pause", False))
                                        QTimer.singleShot(0, lambda: self._set_status("Resumed (remote)"))
                                    elif txt == "stop":
                                        # stop local playback immediately
                                        QTimer.singleShot(0, lambda: self.stop_local())
                                        QTimer.singleShot(0, lambda: self._set_status("Stopped (remote)"))
                                except Exception:
                                    pass
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception as e:
                # transient error - try to reconnect with exponential backoff
                self._ws_reconnect_attempts += 1
                backoff = min(8, 0.5 * (2 ** (self._ws_reconnect_attempts - 1)))
                self._set_status(f"WS disconnected, retrying in {backoff:.1f}s ({self._ws_reconnect_attempts})")
                await asyncio.sleep(backoff)
                if self._ws_reconnect_attempts > self._ws_reconnect_max:
                    self._set_status("WS reconnect failed")
                    break
                continue
            # clean exit of connection loop - break if stop requested
            break
        # clean exit: mark disconnected
        self._ws_connected = False
        QTimer.singleShot(0, lambda: self._set_status("WS disconnected"))
        # close local stream by sentinel
        try:
            await self._queue.put(None)
        except Exception:
            pass

    def _start_ws_receiver(self, ws_url):
        # schedule ws receiver on background event loop
        if self._ws_task and not self._ws_task.done():
            try:
                self._ws_task.cancel()
            except Exception:
                pass
        self._ws_task = asyncio.run_coroutine_threadsafe(self._ws_receiver(ws_url), self._loop)

    def _attempt_start_play(self, local_stream, max_attempts=15):
        """Non-blocking helper: poll _ws_connected and start mpv when connected.
           Uses QTimer to avoid blocking GUI thread."""
        attempts = {"n": 0, "retried": False}

        def _check():
            attempts["n"] += 1
            if getattr(self, "_ws_connected", False):
                # connected — start mpv playback now
                try:
                    self._mpv.play(local_stream)
                    try:
                        self._mpv.pause = False
                    except Exception:
                        pass
                    self._playing = True
                    self._set_status("Playing (remote)")
                    QTimer.singleShot(250, lambda: self.seek_slider.setEnabled(True))
                except Exception:
                    self._set_status("Failed to start mpv")
                return
            if attempts["n"] >= max_attempts:
                if not attempts["retried"]:
                    # try restarting ws receiver once
                    attempts["retried"] = True
                    self._set_status("WS connect slow, retrying...")
                    ws_url = self.backend_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws_audio"
                    self._start_ws_receiver(ws_url)
                    # keep polling (reset counter)
                    attempts["n"] = 0
                    QTimer.singleShot(200, _check)
                    return
                # give up waiting — start mpv anyway (will play when queue fills)
                try:
                    self._mpv.play(local_stream)
                    try:
                        self._mpv.pause = False
                    except Exception:
                        pass
                    self._playing = True
                    self._set_status("Playing (remote, no-ws)")
                    QTimer.singleShot(250, lambda: self.seek_slider.setEnabled(True))
                except Exception:
                    self._set_status("Failed to start mpv (no-ws)")
                return
            # schedule next check
            QTimer.singleShot(200, _check)

        # start polling
        QTimer.singleShot(0, _check)

    def _stop_requested(self):
        # helper: if mpv isn't playing and stop has been requested externally
        return False

    # ----------------- UI Button handlers -----------------
    def play_clicked(self):
        # Sends control play to backend (backend will begin upstream fetch and broadcast)
        if not self._current_video_url:
            self.status_label.setText("No song URL set")
            return

        def _do_play():
            try:
                # show progress in UI thread
                if self.progress:
                    QTimer.singleShot(0, lambda: self.progress.show())
                # ensure local player stopped quickly to allow new stream
                QTimer.singleShot(0, lambda: self.stop_local())

                r = requests.post(f"{self.backend_url}/control", json={"action": "play", "video_url": self._current_video_url}, timeout=30)
                if r.status_code == 200:
                    try:
                        body = r.json()
                        ct = body.get("content_type")
                        if ct:
                            self._backend_content_type = ct
                    except Exception:
                        pass
                    # start local receiver + mpv on main thread
                    QTimer.singleShot(0, lambda: self.start_streaming())
                else:
                    QTimer.singleShot(0, lambda: self._set_status(f"Play failed: {r.status_code}"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._set_status(f"Play error: {e}"))
            finally:
                if self.progress:
                    QTimer.singleShot(0, lambda: self.progress.hide())

        t = threading.Thread(target=_do_play, daemon=True)
        t.start()

    def pause_clicked(self):
        """Non-blocking pause control."""
        def _do_pause():
            try:
                # save last position immediately on main thread
                QTimer.singleShot(0, lambda: setattr(self, "_last_time_pos", float(getattr(self._mpv, "time_pos", 0) or 0)))
                # Immediately update local UI/player so pause feels instant
                QTimer.singleShot(0, lambda: setattr(self._mpv, "pause", True))
                QTimer.singleShot(0, lambda: setattr(self, "_paused", True))
                QTimer.singleShot(0, lambda: self._set_status("Paused (local)"))
                # stop vinyl spin for pause
                QTimer.singleShot(0, lambda: self.vinyl.stop_spin())
                if self.progress:
                    QTimer.singleShot(0, lambda: self.progress.show())
                r = requests.post(f"{self.backend_url}/control", json={"action": "pause"}, timeout=8)
                if r.status_code == 200:
                    # update local mpv and paused flag on main thread
                    QTimer.singleShot(0, lambda: self._set_status("Paused (remote)"))
                else:
                    QTimer.singleShot(0, lambda: self._set_status("Pause failed"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._set_status(f"Pause error: {e}"))
            finally:
                if self.progress:
                    QTimer.singleShot(0, lambda: self.progress.hide())

        t = threading.Thread(target=_do_pause, daemon=True)
        t.start()

    def resume_clicked(self):
        """Non-blocking resume control (call to resume a previously paused track)."""
        def _do_resume():
            try:
                if self.progress:
                    QTimer.singleShot(0, lambda: self.progress.show())
                r = requests.post(f"{self.backend_url}/control", json={"action": "resume"}, timeout=8)
                if r.status_code == 200:
                    # unpause local mpv and clear paused flag on main thread
                    QTimer.singleShot(0, lambda: setattr(self._mpv, "pause", False))
                    QTimer.singleShot(0, lambda: setattr(self, "_paused", False))
                    QTimer.singleShot(0, lambda: self._set_status("Resumed (remote)"))
                    # ensure _playing is True
                    QTimer.singleShot(0, lambda: setattr(self, "_playing", True))
                    # resume vinyl spin
                    QTimer.singleShot(0, lambda: self.vinyl.start_spin())
                else:
                    QTimer.singleShot(0, lambda: self._set_status("Resume failed"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._set_status(f"Resume error: {e}"))
            finally:
                if self.progress:
                    QTimer.singleShot(0, lambda: self.progress.hide())

        t = threading.Thread(target=_do_resume, daemon=True)
        t.start()

    def stop_clicked(self):
        """Non-blocking stop control (remote + local). Hard stop backend to free resources,
        but client saves timestamp so resume uses start_time."""
        def _do_stop():
            try:
                # stash last position immediately
                QTimer.singleShot(0, lambda: setattr(self, "_last_time_pos", float(getattr(self._mpv, "time_pos", 0) or 0)))
                if self.progress:
                    QTimer.singleShot(0, lambda: self.progress.show())
                # send HARD stop to backend so the upstream fetch/ffmpeg is terminated and buffer cleared
                r = requests.post(f"{self.backend_url}/control", json={"action": "stop", "hard": True}, timeout=8)
                # always stop local immediately (disconnect local mpv / ws)
                QTimer.singleShot(0, lambda: self.stop_local())
                # mark paused so UI resumes with start_time
                QTimer.singleShot(0, lambda: setattr(self, "_paused", True))
                if r.status_code == 200:
                    QTimer.singleShot(0, lambda: self._set_status("Stopped (remote)"))
                else:
                    QTimer.singleShot(0, lambda: self._set_status("Stop failed"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._set_status(f"Stop error: {e}"))
            finally:
                if self.progress:
                    QTimer.singleShot(0, lambda: self.progress.hide())

        t = threading.Thread(target=_do_stop, daemon=True)
        t.start()

    def stop_local(self):
        """Immediately stop local mpv and websocket receiver for this client only."""
        try:
            # cancel ws receiver task
            if getattr(self, "_ws_task", None):
                try:
                    self._ws_task.cancel()
                except Exception:
                    pass
                self._ws_task = None
            # mark disconnected
            self._ws_connected = False
            # put sentinel into queue on background loop so local_stream returns
            try:
                asyncio.run_coroutine_threadsafe(self._queue.put(None), self._loop)
            except Exception:
                pass
            # stop mpv immediately
            try:
                # prefer mpv stop command
                try:
                    self._mpv.stop()
                except Exception:
                    try:
                        self._mpv.command("stop")
                    except Exception:
                        pass
            except Exception:
                pass
            # update flags and UI
            self._playing = False
            self._set_status("Stopped (local)")
            # stop vinyl spin
            QTimer.singleShot(0, lambda: self.vinyl.stop_spin())
            # keep _paused True so UI knows resume should use start_time
            self._paused = True
            # disable seek controls
            QTimer.singleShot(0, lambda: self.seek_slider.setEnabled(False))
        except Exception:
            pass

    def start_streaming(self):
        """Start local websocket receiver and mpv playback (call after control play succeeded)."""
        # clear paused flag for fresh start
        self._paused = False
        ws_url = self.backend_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws_audio"
        # ensure flag reset and start receiver
        self._ws_connected = False
        self._start_ws_receiver(ws_url)
        # non-blocking attempt to start mpv once ws connected (or fallback after timeout)
        local_stream = f"http://127.0.0.1:{self._local_port}/local_stream"
        self._attempt_start_play(local_stream)
        # start vinyl spin when streaming starts
        QTimer.singleShot(200, lambda: self.vinyl.start_spin())

    # ----------------- seek / volume helpers -----------------
    def _on_volume_changed(self, value):
        try:
            self._mpv.volume = value
        except Exception:
            pass

    def _on_seek_released(self):
        # perform seek using mpv if duration known
        try:
            duration = getattr(self._mpv, "duration", None)
            if duration and duration > 0:
                pos = self.seek_slider.value() / 1000.0
                target = pos * duration
                # use mpv command to seek absolute
                try:
                    self._mpv.command("seek", target, "absolute")
                except Exception:
                    # fallback: set time_pos property if available
                    try:
                        self._mpv.time_pos = target
                    except Exception:
                        pass
        except Exception:
            pass

    def _format_time(self, seconds):
        try:
            s = int(seconds)
            mm = s // 60
            ss = s % 60
            return f"{mm:02d}:{ss:02d}"
        except Exception:
            return "00:00"

    # ----------------- helpers -----------------
    def set_remote_video(self, video_url):
        # called by UI manager when a result is clicked
        self._current_video_url = video_url
        # normalize to canonical video id if possible so comparisons are reliable
        vid = None
        try:
            # url like ...watch?v=ID
            m = re.search(r"[?&]v=([^&]+)", str(video_url))
            if m:
                vid = m.group(1)
            else:
                # if passed already an id or short url, accept it
                s = str(video_url).strip()
                if s and not s.startswith("http"):
                    vid = s
        except Exception:
            vid = None
        self._current_video_id = vid

        # fetch thumbnail in background and set vinyl pixmap
        def _fetch_thumb():
            try:
                thumb_url = ""
                if self._current_video_id:
                    thumb_url = f"https://i.ytimg.com/vi/{self._current_video_id}/hqdefault.jpg"
                else:
                    # if remote provided a full url with v=... we handled above; fallback: try to extract via proxy endpoint
                    if self._current_video_url and "youtube" in str(self._current_video_url).lower():
                        m2 = re.search(r"[?&]v=([^&]+)", str(self._current_video_url))
                        if m2:
                            thumb_url = f"https://i.ytimg.com/vi/{m2.group(1)}/hqdefault.jpg"
                if not thumb_url:
                    return
                r = requests.get(thumb_url, timeout=6)
                if r.status_code == 200:
                    data = r.content
                    pix = QPixmap()
                    pix.loadFromData(data)
                    QTimer.singleShot(0, lambda: self.vinyl.set_vinyl_pixmap(pix))
            except Exception:
                pass

        threading.Thread(target=_fetch_thumb, daemon=True).start()

    def _set_status(self, text):
        # update UI label from main thread
        try:
            self.status_label.setText(f"Status: {text}")
        except Exception:
            pass

    # ----------------- poll and detect track end -----------------
    def _poll_mpv(self):
        try:
            time_pos = getattr(self._mpv, "time_pos", None)
            duration = getattr(self._mpv, "duration", None)
            # always keep last known position updated
            if time_pos is not None:
                self._last_time_pos = float(time_pos or 0)
            if time_pos is None:
                return

            # update seek slider
            if duration and duration > 0:
                frac = min(1.0, max(0.0, float(time_pos) / float(duration)))
                self.seek_slider.blockSignals(True)
                self.seek_slider.setValue(int(frac * 1000))
                self.seek_slider.blockSignals(False)
                self.seek_label.setText(f"{self._format_time(time_pos)} / {self._format_time(duration)}")
            else:
                self.seek_label.setText(f"{self._format_time(time_pos)} / --:--")

            # end-of-track detection: trigger once when near end
            if duration and duration > 0:
                if time_pos >= max(0, duration - 0.6):
                    if not self._track_end_triggered:
                        self._track_end_triggered = True
                        # schedule callback on main thread
                        if self.on_track_end:
                            QTimer.singleShot(0, lambda: self.on_track_end())
                else:
                    self._track_end_triggered = False

            # update status text if mpv state changed
            playing = getattr(self._mpv, "pause", False) is False
            if playing and self._playing:
                self._set_status("Playing (remote)")
            elif not playing and self._playing:
                self._set_status("Paused (local)")
        except Exception:
            pass