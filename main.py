#!/usr/bin/env python3
import sys
import os
import requests
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget, QLineEdit, QPushButton, QLabel, QScrollArea, QTextEdit, QProgressBar, QHBoxLayout, QSizePolicy, QMenu, QAction
from PyQt5.QtCore import QThreadPool, QRunnable, pyqtSlot, QObject, pyqtSignal, QPropertyAnimation, QRect
from player import MusicPlayer
from ui import UIManager

class WorkerSignals(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(object)
    result = pyqtSignal(object)

class SearchWorker(QRunnable):
    def __init__(self, backend_url, query):
        super().__init__()
        self.backend_url = backend_url
        self.query = query
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        try:
            resp = requests.get(f"{self.backend_url}/search", params={"query": self.query}, timeout=10)
            if resp.status_code == 200:
                self.signals.result.emit(resp.json())
            else:
                self.signals.error.emit(f"HTTP {resp.status_code}")
        except Exception as e:
            self.signals.error.emit(str(e))
        finally:
            self.signals.finished.emit()

# new: PlayWorker posts /control?action=play and returns JSON response
class PlayWorker(QRunnable):
    def __init__(self, backend_url, video_url, start_time=None):
        super().__init__()
        self.backend_url = backend_url
        self.video_url = video_url
        self.start_time = start_time
        self.signals = WorkerSignals()

    @pyqtSlot()
    def run(self):
        try:
            payload = {"action": "play", "video_url": self.video_url}
            if self.start_time is not None:
                payload["start_time"] = float(self.start_time)
            resp = requests.post(f"{self.backend_url}/control", json=payload, timeout=30)
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except Exception:
                    body = {"status": "playing"}
                self.signals.result.emit(body)
            else:
                self.signals.error.emit(f"HTTP {resp.status_code}")
        except Exception as e:
            self.signals.error.emit(str(e))
        finally:
            self.signals.finished.emit()

class YouTubeMusicStreamer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube Music Streamer")
        self.setGeometry(100, 100, 1000, 700)
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        # Top bar: burger (left) + search field + search button (right of field)
        topbar = QHBoxLayout()
        # burger menu
        self.burger_btn = QPushButton("☰")
        self.burger_btn.setFixedWidth(40)
        self.burger_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # create menu for burger
        burger_menu = QMenu()
        # Logs toggle
        self.logs_action = burger_menu.addAction("Logs")
        self.logs_action.setCheckable(True)
        self.logs_action.setChecked(False)

        # Themes submenu
        themes_menu = burger_menu.addMenu("Themes")
        self.theme_actions = []
        themes = [
            ("Blue Mix", "blue"),
            ("Warm Mix", "warm"),
            ("Modern Mix", "modern")
        ]
        for name, key in themes:
            a = QAction(name, self)
            a.setCheckable(True)
            themes_menu.addAction(a)
            self.theme_actions.append((key, a))
            a.triggered.connect(lambda checked, k=key: self.apply_theme(k))
        self.burger_btn.setMenu(burger_menu)
        topbar.addWidget(self.burger_btn, 0)

        # search area (search field + button) to the right of burger
        search_area = QHBoxLayout()
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Search for music...")
        self.search_field.setMinimumHeight(36)  # make it a bit bigger
        self.search_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        search_area.addWidget(self.search_field, 1)

        self.search_button = QPushButton("Search")
        self.search_button.setFixedWidth(120)
        self.search_button.setMinimumHeight(36)
        self.search_button.clicked.connect(self.perform_search)
        search_area.addWidget(self.search_button, 0)

        topbar.addLayout(search_area, 1)
        self.layout.addLayout(topbar)

        # connect logs toggle
        self.logs_action.triggered.connect(self._toggle_logs)

        # Main content: left = player + search results, right = recommendations (thinner)
        content = QHBoxLayout()

        # left column (player + results)
        left_col = QVBoxLayout()
        # Progress bar for long operations
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # busy indicator
        self.progress.hide()
        left_col.addWidget(self.progress)

        # create player and place it directly below topbar
        self.player = MusicPlayer(backend_url=self._backend_url_from_env(), progress=self.progress)
        left_col.addWidget(self.player, 0)

        # results area (search results) below player
        self.results_area = QScrollArea()
        self.results_area.setMinimumHeight(200)
        left_col.addWidget(self.results_area, 1)

        content.addLayout(left_col, 3)

        # right column: recommendations (thinner)
        self.recommendations_widget = QWidget()
        rec_layout = QVBoxLayout(self.recommendations_widget)
        rec_label = QLabel("Recommended Songs:")
        rec_layout.addWidget(rec_label)
        # Use a QListWidget for recommendations
        from PyQt5.QtWidgets import QListWidget
        self.recommendations_list = QListWidget()
        self.recommendations_list.setMinimumWidth(240)
        rec_layout.addWidget(self.recommendations_list, 1)
        content.addWidget(self.recommendations_widget, 1)

        self.layout.addLayout(content, 1)

        # Console / logs (hidden by default)
        self.console_output = QTextEdit()
        self.console_output.setReadOnly(True)
        self.console_output.hide()
        self.layout.addWidget(self.console_output)

        # Read backend host/port from env or use defaults
        backend_host = os.environ.get("BACKEND_HOST", "127.0.0.1")
        backend_port = os.environ.get("BACKEND_PORT", "5000")
        self.backend_url = f"http://{backend_host}:{backend_port}"

        self.threadpool = QThreadPool()

        # pass progress bar into the player so controls can show it for long ops
        # UIManager will populate results_area and handle interactions
        self.ui_manager = UIManager(self, self.results_area, self.console_output, self.player,
                                    backend_url=self.backend_url, threadpool=self.threadpool,
                                    progress=self.progress, recommendations_list=self.recommendations_list)

        # wire search button also to the search_field enter key
        self.search_field.returnPressed.connect(self.perform_search)

    def _backend_url_from_env(self):
        backend_host = os.environ.get("BACKEND_HOST", "127.0.0.1")
        backend_port = os.environ.get("BACKEND_PORT", "5000")
        return f"http://{backend_host}:{backend_port}"

    def _toggle_logs(self, checked):
        # animate console show/hide via height animation
        if checked:
            self.console_output.show()
            anim = QPropertyAnimation(self.console_output, b"geometry")
            start = QRect(self.console_output.x(), self.console_output.y(), self.console_output.width(), 0)
            end = QRect(self.console_output.x(), self.console_output.y(), self.console_output.width(), 180)
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.setDuration(220)
            anim.start()
            # keep reference so it isn't GC'd
            self._logs_anim = anim
        else:
            anim = QPropertyAnimation(self.console_output, b"geometry")
            start = QRect(self.console_output.x(), self.console_output.y(), self.console_output.width(), self.console_output.height())
            end = QRect(self.console_output.x(), self.console_output.y(), self.console_output.width(), 0)
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.setDuration(220)
            def _hide():
                self.console_output.hide()
            anim.finished.connect(_hide)
            anim.start()
            self._logs_anim = anim

    def apply_theme(self, key):
        # uncheck all theme actions and check selected
        for k, action in self.theme_actions:
            action.setChecked(k == key)
        # simple stylesheet sets - gradients and rounded corners
        if key == "blue":
            qss = """
                QWidget { background: qlineargradient(x1:0 y1:0, x2:1 y2:1, stop:0 #1D3461, stop:0.6 #1F487E, stop:1 #376996); color: #f0f6fb; border-radius:8px; }
                QLineEdit, QTextEdit, QListWidget { background: rgba(255,255,255,0.06); border-radius:6px; padding:6px; }
                QPushButton { background: rgba(255,255,255,0.08); border-radius:6px; padding:6px 10px; }
                QPushButton:hover { background: rgba(255,255,255,0.12); }
            """
        elif key == "warm":
            qss = """
                QWidget { background: qlineargradient(x1:0 y1:0, x2:1 y2:1, stop:0 #EA8C55, stop:0.6 #C75146, stop:1 #AD2E24); color: #fff9f6; border-radius:8px; }
                QLineEdit, QTextEdit, QListWidget { background: rgba(0,0,0,0.06); border-radius:6px; padding:6px; }
                QPushButton { background: rgba(255,255,255,0.08); border-radius:6px; padding:6px 10px; }
                QPushButton:hover { background: rgba(255,255,255,0.12); }
            """
        else:  # modern (mostly dark + accent)
            qss = """
                QWidget { background: qlineargradient(x1:0 y1:0, x2:1 y2:1, stop:0 #2C363F, stop:0.6 #E75A7C, stop:1 #F2F5EA); color: #eaeef0; border-radius:8px; }
                QLineEdit, QTextEdit, QListWidget { background: rgba(255,255,255,0.04); border-radius:6px; padding:6px; color: #f2f5ea; }
                QPushButton { background: rgba(255,255,255,0.06); border-radius:6px; padding:6px 10px; }
                QPushButton:hover { background: rgba(255,255,255,0.12); }
            """
        QApplication.instance().setStyleSheet(qss)

    def perform_search(self):
        query = self.search_field.text().strip()
        if not query:
            return
        self.console_output.append(f"Searching for: {query}")
        self.progress.show()
        worker = SearchWorker(self.backend_url, query)
        worker.signals.result.connect(self.on_search_results)
        worker.signals.error.connect(self.on_search_error)
        worker.signals.finished.connect(self.on_search_finished)
        self.threadpool.start(worker)

    def on_search_results(self, results):
        self.ui_manager.display_results(results)

    def on_search_error(self, err):
        self.console_output.append(f"Search error: {err}")

    def on_search_finished(self):
        self.progress.hide()

    def fetch_results(self, query):
        # legacy synchronous fallback (not used)
        try:
            response = requests.get(f"{self.backend_url}/search", params={"query": query}, timeout=8)
            if response.status_code == 200:
                return response.json()
            else:
                self.console_output.append(f"Error fetching results: {response.status_code}")
        except Exception as e:
            self.console_output.append(f"Error fetching results: {e}")
        return []

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YouTubeMusicStreamer()
    window.show()
    sys.exit(app.exec_())