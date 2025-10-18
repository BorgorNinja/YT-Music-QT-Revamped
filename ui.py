from PyQt5.QtWidgets import QVBoxLayout, QWidget, QLabel, QPushButton, QListWidget, QMessageBox, QListWidgetItem
from PyQt5.QtCore import Qt, QTimer, QSize
from PyQt5.QtGui import QPixmap, QIcon
import requests
import threading
import re

# --- added: cached requests session and thumbnail helper (copied/adapted from borgortube.py) ---
import requests_cache
requests_cache.install_cache('thumb_cache', expire_after=86400)
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "max-age=86400"
})

def get_low_res_thumbnail(url):
    """Prefer a lower-res thumbnail variant when a maxres URL is provided to avoid 403s."""
    if not url:
        return ""
    try:
        if "maxresdefault" in url:
            return url.replace("maxresdefault", "mqdefault")
        return url
    except Exception:
        return url

def derive_thumb_from_id(video_id):
    if not video_id:
        return ""
    # normalize id (strip watch?v=)
    m = re.search(r"[?&]v=([^&]+)", str(video_id))
    vid_only = m.group(1) if m else (str(video_id).strip() if video_id and not str(video_id).startswith("http") else "")
    if not vid_only:
        return ""
    return f"https://i.ytimg.com/vi/{vid_only}/hqdefault.jpg"


class MusicUI(QWidget):
    def __init__(self, backend_url):
        super().__init__()
        self.backend_url = backend_url
        self.layout = QVBoxLayout(self)

        self.search_results = QListWidget()
        self.search_results.itemClicked.connect(self.on_item_clicked)
        # make room for thumbnails
        self.search_results.setIconSize(QSize(96, 72))
        self.layout.addWidget(self.search_results)

        self.recommendations_label = QLabel("Recommended Songs:")
        self.layout.addWidget(self.recommendations_label)

        self.recommendations_list = QListWidget()
        # make room for thumbnails
        self.recommendations_list.setIconSize(QSize(64, 64))
        self.layout.addWidget(self.recommendations_list)

        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.play_selected)
        self.layout.addWidget(self.play_button)

        self.setLayout(self.layout)

    def update_search_results(self, results):
        # normalize possible yt-dlp payloads (list or dict with 'entries')
        items = []
        if isinstance(results, dict):
            items = results.get("entries") or results.get("results") or []
        elif isinstance(results, list):
            items = results
        else:
            items = []

        self.search_results.clear()
        for result in items:
            title = result.get('title', result.get('name', 'Unknown'))
            video_id = result.get("videoId") or result.get("id") or result.get("url") or result.get("webpage_url") or ""
            thumb = result.get("thumbnail", "") or ""
            # if thumbnail missing, derive from id
            if not thumb and video_id:
                thumb = derive_thumb_from_id(video_id)
            thumb = get_low_res_thumbnail(thumb) if thumb else ""

            item = QListWidgetItem(title)
            # make room visually for thumbnail + text
            item.setSizeHint(item.sizeHint())
            item.setData(Qt.UserRole, result)
            self.search_results.addItem(item)

            # async fetch thumbnail and set icon when ready (use cached session)
            if thumb:
                def _fetch_and_set(it, url):
                    try:
                        r = session.get(url, timeout=6)
                        if r.status_code == 200 and r.content:
                            pix = QPixmap()
                            pix.loadFromData(r.content)
                            icon = QIcon(pix.scaled(96, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                            QTimer.singleShot(0, lambda: it.setIcon(icon))
                            return
                    except Exception:
                        pass
                    # fallback: if we failed, try derived id thumb (if different)
                    try:
                        alt = derive_thumb_from_id(video_id)
                        if alt and alt != url:
                            r2 = session.get(alt, timeout=6)
                            if r2.status_code == 200 and r2.content:
                                pix2 = QPixmap()
                                pix2.loadFromData(r2.content)
                                icon2 = QIcon(pix2.scaled(96, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                                QTimer.singleShot(0, lambda: it.setIcon(icon2))
                    except Exception:
                        pass

                threading.Thread(target=_fetch_and_set, args=(item, thumb), daemon=True).start()
            else:
                # optional: set a small placeholder icon to keep alignment
                pix = QPixmap(96, 72)
                pix.fill(Qt.transparent)
                item.setIcon(QIcon(pix))

    def on_item_clicked(self, item):
        selected_title = item.text()
        self.fetch_recommendations(selected_title)

    def fetch_recommendations(self, song_title):
        response = requests.get(f"{self.backend_url}/recommendations", params={"title": song_title})
        if response.status_code == 200:
            recommendations = response.json()
            self.update_recommendations(recommendations)
        else:
            QMessageBox.warning(self, "Error", "Failed to fetch recommendations.")

    def update_recommendations(self, recommendations):
        self.recommendations_list.clear()
        self.last_recommendations = recommendations or []
        self._auto_index = 0
        for r in self.last_recommendations:
            title = r.get("title", "Unknown")
            author = r.get("author", "") or r.get("uploader", "") or ""
            text = f"{title}\n{author}" if author else title
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, r)
            self.recommendations_list.addItem(item)

            # fetch thumbnail asynchronously and set as icon if available (use cached session + low-res fallback)
            thumb = r.get("thumbnail", "") or ""
            video_id = r.get("videoId") or r.get("id") or ""
            if not thumb and video_id:
                thumb = derive_thumb_from_id(video_id)
            thumb = get_low_res_thumbnail(thumb) if thumb else ""
            if thumb:
                def _fetch_and_set(it, url):
                    try:
                        rresp = session.get(url, timeout=6)
                        if rresp.status_code == 200 and rresp.content:
                            pix = QPixmap()
                            pix.loadFromData(rresp.content)
                            icon = QIcon(pix.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                            QTimer.singleShot(0, lambda: it.setIcon(icon))
                    except Exception:
                        pass
                threading.Thread(target=_fetch_and_set, args=(item, thumb), daemon=True).start()
            else:
                pix = QPixmap(72, 72)
                pix.fill(Qt.transparent)
                item.setIcon(QIcon(pix))

    def play_selected(self):
        selected_item = self.search_results.currentItem()
        if selected_item:
            song_title = selected_item.text()
            QMessageBox.information(self, "Playing", f"Now playing: {song_title}")
        else:
            QMessageBox.warning(self, "Error", "No song selected.")


class UIManager:
    """
    Manages the results UI inside a provided QScrollArea, handles clicks,
    calls backend for stream URL and recommendations, and forwards play
    commands to the MusicPlayer.
    """
    def __init__(self, main_window, results_area, console_output, player, backend_url="http://127.0.0.1:5000", threadpool=None, progress=None, recommendations_list=None):
        self.main_window = main_window
        self.results_area = results_area
        self.console = console_output
        self.player = player
        self.backend_url = backend_url.rstrip("/")
        self.threadpool = threadpool
        self.progress = progress
        # store recommendations for autoplay
        self.last_recommendations = []
        self._auto_index = 0

        # connect player track-end callback to autoplay
        try:
            self.player.on_track_end = self._on_track_end
        except Exception:
            pass

        # container that will be inserted into the QScrollArea
        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.results_list = QListWidget()
        self.results_list.setIconSize(QSize(96, 96))   # make room for thumbnails
        self.results_list.itemClicked.connect(self.on_item_clicked)
        self.layout.addWidget(QLabel("Search Results:"))
        self.layout.addWidget(self.results_list)

        # If a recommendations_list was provided (from main layout), use it; else create one
        self.recommendations_label = QLabel("Recommended Songs:")
        self.layout.addWidget(self.recommendations_label)
        if recommendations_list is not None:
            self.recommendations_list = recommendations_list
        else:
            self.recommendations_list = QListWidget()
            self.layout.addWidget(self.recommendations_list)

        # Play button (also can be triggered by double click)
        self.play_button = QPushButton("Play Selected")
        self.play_button.clicked.connect(self.play_selected)
        self.layout.addWidget(self.play_button)

        # place container into provided scroll area
        self.results_area.setWidgetResizable(True)
        self.results_area.setWidget(self.container)

    def display_results(self, results):
        """Populate the search results list with [thumbnail] Title items.
           Results can be either a list or a dict with 'entries' (yt-dlp dump)."""
        items = []
        if isinstance(results, dict):
            items = results.get("entries") or results.get("results") or []
        elif isinstance(results, list):
            items = results
        else:
            items = []

        self.results_list.clear()
        for r in items:
            # normalize fields
            title = r.get("title") or r.get("name") or "Unknown"
            video_id = r.get("videoId") or r.get("id") or r.get("url") or r.get("webpage_url") or ""
            thumb = r.get("thumbnail", "") or ""
            # if thumbnail missing, try to derive from video id
            if not thumb and video_id:
                thumb = derive_thumb_from_id(video_id)
            thumb = get_low_res_thumbnail(thumb) if thumb else ""

            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, {"title": title, "videoId": video_id, "thumbnail": thumb, "raw": r})
            self.results_list.addItem(item)

            # async fetch thumbnail and set icon when ready
            if thumb:
                def _fetch_and_set(it, url):
                    try:
                        resp = requests.get(url, timeout=6)
                        if resp.status_code == 200:
                            pix = QPixmap()
                            pix.loadFromData(resp.content)
                            icon = QIcon(pix.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                            QTimer.singleShot(0, lambda: it.setIcon(icon))
                    except Exception:
                        pass
                threading.Thread(target=_fetch_and_set, args=(item, thumb), daemon=True).start()

        self.console.append(f"Displayed {self.results_list.count()} results.")

    def on_item_clicked(self, item):
        data = item.data(Qt.UserRole) or {}
        self.console.append(f"Selected: {data.get('title')}")
        # fetch stream url and play (non-blocking)
        self.fetch_stream_and_play(data)

    def fetch_stream_and_play(self, item_data):
        video_id = item_data.get("videoId") or item_data.get("id") or item_data.get("url")
        if not video_id:
            QMessageBox.warning(self.main_window, "Error", "No video id available for this item.")
            return

        # If this is the same video and player is paused, request play with start_time (resume from saved timestamp)
        try:
            if getattr(self.player, "_paused", False) and getattr(self.player, "_current_video_id", None):
                # extract candidate id
                import re
                m = re.search(r"[?&]v=([^&]+)", str(video_id))
                candidate_id = m.group(1) if m else (str(video_id).strip() if not str(video_id).startswith("http") else None)
                if candidate_id and candidate_id == self.player._current_video_id:
                    start_time = getattr(self.player, "_last_time_pos", 0) or 0
                    self.console.append(f"Resuming paused track at {start_time:.1f}s...")
                    # run PlayWorker but instruct backend to start at that time
                    from main import PlayWorker
                    worker = PlayWorker(self.backend_url, video_id, start_time=start_time)
                    def on_result_resume(body):
                        try:
                            ct = body.get("content_type")
                            if ct:
                                self.player._backend_content_type = ct
                        except Exception:
                            pass
                        # start local receiver + mpv playback
                        try:
                            self.player.start_streaming()
                            self.console.append(f"Resumed local playback for: {item_data.get('title')}")
                            # mark resumed
                            self.player._paused = False
                        except Exception as e:
                            QMessageBox.warning(self.main_window, "Error", f"Failed to start local playback: {e}")
                    def on_err(e):
                        self.console.append(f"Resume failed: {e}")
                    def on_fin():
                        if self.progress:
                            self.progress.hide()
                        try:
                            self.play_button.setEnabled(True)
                        except Exception:
                            pass
                    worker.signals.result.connect(on_result_resume)
                    worker.signals.error.connect(on_err)
                    worker.signals.finished.connect(on_fin)
                    if self.threadpool:
                        self.threadpool.start(worker)
                    else:
                        import threading
                        threading.Thread(target=worker.run, daemon=True).start()
                    return
        except Exception:
            pass

        # disable play button to avoid double-submits while backend is extracting
        try:
            self.play_button.setEnabled(False)
        except Exception:
            pass

        # Use PlayWorker (non-blocking) to call backend /control play.
        # Show progress while backend extracts formats and starts streaming.
        try:
            self.console.append(f"Requesting backend to play: {item_data.get('title')}")
            self.player.set_remote_video(video_id)

            # show progress indicator
            if self.progress:
                self.progress.show()

            from main import PlayWorker  # avoid circular import at module load
            worker = PlayWorker(self.backend_url, video_id)
            # when worker returns result, start local websocket receiver + mpv playback
            def on_result(body):
                # body may contain content_type
                try:
                    ct = body.get("content_type")
                    if ct:
                        self.player._backend_content_type = ct
                except Exception:
                    pass
                # start local receiver + mpv playback
                try:
                    self.player.start_streaming()
                    self.console.append(f"Started local playback for: {item_data.get('title')}")
                except Exception as e:
                    QMessageBox.warning(self.main_window, "Error", f"Failed to start local playback: {e}")
                # fetch recommendations after backend is ready
                self.fetch_recommendations(item_data)

            def on_error(err):
                QMessageBox.warning(self.main_window, "Error", f"Play request failed: {err}")
                self.console.append(f"Play request failed: {err}")

            def on_finished():
                if self.progress:
                    self.progress.hide()
                # re-enable play button when backend operation finished
                try:
                    self.play_button.setEnabled(True)
                except Exception:
                    pass

            worker.signals.result.connect(on_result)
            worker.signals.error.connect(on_error)
            worker.signals.finished.connect(on_finished)

            if self.threadpool:
                self.threadpool.start(worker)
            else:
                # fallback run in background thread if no threadpool provided
                import threading
                t = threading.Thread(target=worker.run, daemon=True)
                t.start()

            # ensure player widget is visible in the main layout
            if self.player.parent() is None:
                self.main_window.layout.addWidget(self.player)
                self.player.show()
        except Exception as e:
            QMessageBox.warning(self.main_window, "Error", f"Play request exception: {e}")
            if self.progress:
                self.progress.hide()
            try:
                self.play_button.setEnabled(True)
            except Exception:
                pass

    def fetch_recommendations(self, item_data):
        # Prefer videoId but fall back to title
        params = {}
        if item_data.get("videoId"):
            params["videoId"] = item_data.get("videoId", "")
        else:
            params["title"] = item_data.get("title", "")

        # run recommendations call in background thread to avoid blocking UI
        def _worker():
            try:
                response = requests.get(f"{self.backend_url}/recommendations", params=params, timeout=15)
                if response.status_code == 200:
                    recommendations = response.json()
                    # schedule update on the GUI thread
                    QTimer.singleShot(0, lambda: self.update_recommendations(recommendations))
                    QTimer.singleShot(0, lambda: self.console.append(f"Loaded {len(recommendations)} recommendations."))
                else:
                    QTimer.singleShot(0, lambda: self.console.append(f"Failed to fetch recommendations: {response.status_code}"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self.console.append(f"Recommendation fetch error: {e}"))

        if self.threadpool:
            # use the provided QThreadPool by running a QRunnable wrapper if desired,
            # but simplest: use a plain thread so we can post results with QTimer.singleShot
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
        else:
            t = threading.Thread(target=_worker, daemon=True)
            t.start()

    def update_recommendations(self, recommendations):
        # recommendations_list may be from main window (shared widget)
        try:
            self.recommendations_list.clear()
            self.last_recommendations = recommendations or []
            self._auto_index = 0
            for r in self.last_recommendations:
                title = r.get("title", "Unknown")
                author = r.get("author", "") or r.get("uploader", "") or ""
                text = f"{title}\n{author}" if author else title
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, r)
                self.recommendations_list.addItem(item)

                # fetch thumbnail asynchronously and set as icon if available
                thumb = r.get("thumbnail", "") or ""
                video_id = r.get("videoId") or r.get("id") or ""
                if not thumb and video_id:
                    thumb = derive_thumb_from_id(video_id)
                thumb = get_low_res_thumbnail(thumb) if thumb else ""
                if thumb:
                    def _fetch_and_set(it, url):
                        try:
                            rresp = session.get(url, timeout=6)
                            if rresp.status_code == 200 and rresp.content:
                                pix = QPixmap()
                                pix.loadFromData(rresp.content)
                                # scale down icon size
                                icon = QIcon(pix.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                                QTimer.singleShot(0, lambda: it.setIcon(icon))
                        except Exception:
                            pass
                    threading.Thread(target=_fetch_and_set, args=(item, thumb), daemon=True).start()
                else:
                    pix = QPixmap(72, 72)
                    pix.fill(Qt.transparent)
                    item.setIcon(QIcon(pix))
        except Exception as e:
            self.console.append(f"Failed to update recommendations: {e}")

    def _on_track_end(self):
        # autoplay next recommendation if available
        try:
            if not self.last_recommendations:
                self.console.append("No recommendations to autoplay.")
                return
            # pick next item by index
            if self._auto_index < len(self.last_recommendations):
                next_item = self.last_recommendations[self._auto_index]
                self._auto_index += 1
            else:
                # loop back or stop
                self._auto_index = 0
                next_item = self.last_recommendations[0]

            self.console.append(f"Autoplaying: {next_item.get('title')}")
            # ensure UI shows progress and use PlayWorker as usual
            if self.progress:
                self.progress.show()

            from main import PlayWorker
            worker = PlayWorker(self.backend_url, next_item.get("videoId"))
            def on_result(body):
                try:
                    ct = body.get("content_type")
                    if ct:
                        self.player._backend_content_type = ct
                except Exception:
                    pass
                # start playback locally
                try:
                    self.player.start_streaming()
                    self.console.append(f"Autoplay started: {next_item.get('title')}")
                except Exception as e:
                    self.console.append(f"Autoplay failed: {e}")
                # fetch next set of recommendations for subsequent autoplay
                self.fetch_recommendations(next_item)

            def on_error(err):
                self.console.append(f"Autoplay play request failed: {err}")

            def on_finished():
                if self.progress:
                    self.progress.hide()

            worker.signals.result.connect(on_result)
            worker.signals.error.connect(on_error)
            worker.signals.finished.connect(on_finished)
            if self.threadpool:
                self.threadpool.start(worker)
            else:
                import threading
                threading.Thread(target=worker.run, daemon=True).start()
        except Exception as e:
            self.console.append(f"Autoplay error: {e}")

    def play_selected(self):
        item = self.results_list.currentItem()
        if not item:
            QMessageBox.warning(self.main_window, "Error", "No song selected.")
            return
        # reuse click handler
        self.on_item_clicked(item)