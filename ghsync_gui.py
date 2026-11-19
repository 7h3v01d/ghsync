#!/usr/bin/env python3
"""
ghsync_gui - dark industrial GUI for ghsync.py.

Keep this file in the same folder as ghsync.py.   Needs:  pip install PyQt6
Run:   python ghsync_gui.py [project_folder]
"""
import argparse
import contextlib
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghsync  # noqa: E402

from PyQt6.QtCore import QEvent, QPointF, QSettings, Qt, QThread, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtGui import (QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap,  # noqa: E402
                         QPolygonF, QShortcut, QTextCharFormat, QTextCursor)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame,  # noqa: E402
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QSplitter,
                             QStackedWidget, QSystemTrayIcon, QTableWidget,
                             QTableWidgetItem, QTableWidgetSelectionRange,
                             QTabWidget, QVBoxLayout, QWidget, QAbstractItemView)

# ------------------------------------------------------------------ palette
BG, PANEL, PANEL2, LINE = "#121416", "#1a1d21", "#22262c", "#2d3239"
TEXT, MUTED, DIM = "#d7dbe0", "#7d8590", "#4d545d"
AMBER, BLUE, RED, GREEN = "#ffb000", "#5aa9e6", "#ff5c5c", "#4ecb8d"
MONO = '"Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", monospace'
UI = '"Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", sans-serif'

QSS = """
QWidget { background: %(BG)s; color: %(TEXT)s; font-family: %(UI)s; font-size: 13px; }
QLabel { background: transparent; }
QToolTip { background: %(PANEL2)s; color: %(TEXT)s; border: 1px solid %(LINE)s; }

QLineEdit { background: %(PANEL)s; border: 1px solid %(LINE)s; padding: 7px 10px;
            selection-background-color: %(AMBER)s; selection-color: #000; font-family: %(MONO)s; }
QLineEdit:focus { border-color: %(AMBER)s; }
QLineEdit:read-only { color: %(MUTED)s; }

QPushButton { background: %(PANEL2)s; border: 1px solid %(LINE)s; padding: 8px 16px;
              font-weight: 600; font-size: 11px; letter-spacing: 1px; }
QPushButton:hover { border-color: %(MUTED)s; background: #292e35; }
QPushButton:pressed { background: %(PANEL)s; }
QPushButton:disabled { color: %(DIM)s; background: %(PANEL)s; border-color: #23272d; }
QPushButton[kind="primary"] { background: %(AMBER)s; color: #111; border: 1px solid %(AMBER)s; }
QPushButton[kind="primary"]:hover { background: #ffc233; border-color: #ffc233; }
QPushButton[kind="primary"]:disabled { background: #3a3220; color: #6e6446; border-color: #3a3220; }
QPushButton[kind="warn"] { color: %(RED)s; border-color: #5a2f31; }
QPushButton[kind="warn"]:hover { border-color: %(RED)s; background: #2a1d1f; }
QPushButton[kind="warn"]:disabled { color: %(DIM)s; border-color: #23272d; }

QTableWidget { background: %(PANEL)s; border: 1px solid %(LINE)s; gridline-color: transparent;
               alternate-background-color: #1d2024; outline: 0; font-family: %(MONO)s; font-size: 12px; }
QTableWidget::item { padding: 3px 8px; border: none; }
QTableWidget::item:selected { background: #2c323a; color: #fff; }
QHeaderView { background: %(BG)s; }
QHeaderView::section { background: %(BG)s; color: %(MUTED)s; border: none; border-bottom: 2px solid %(LINE)s;
                       padding: 7px 8px; font-size: 10px; font-weight: 700; letter-spacing: 2px;
                       font-family: %(UI)s; }

QPlainTextEdit { background: #0e1012; border: 1px solid %(LINE)s; padding: 8px;
                 font-family: %(MONO)s; font-size: 12px; selection-background-color: %(AMBER)s;
                 selection-color: #000; }

QTabWidget::pane { border: 1px solid %(LINE)s; top: -1px; }
QTabBar::tab { background: %(BG)s; color: %(MUTED)s; padding: 7px 18px; border: 1px solid transparent;
               font-size: 10px; font-weight: 700; letter-spacing: 2px; }
QTabBar::tab:selected { color: %(AMBER)s; background: %(PANEL)s; border: 1px solid %(LINE)s; border-bottom-color: %(PANEL)s; }
QTabBar::tab:hover:!selected { color: %(TEXT)s; }

QScrollBar:vertical { background: %(BG)s; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #333941; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: %(DIM)s; }
QScrollBar:horizontal { background: %(BG)s; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #333941; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QSplitter::handle { background: %(BG)s; }
QSplitter::handle:vertical { height: 6px; }

QProgressBar { background: %(PANEL)s; border: none; max-height: 3px; min-height: 3px; }
QProgressBar::chunk { background: %(AMBER)s; }

QCheckBox { spacing: 8px; color: %(MUTED)s; background: transparent; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid %(MUTED)s; background: %(PANEL)s; }
QCheckBox::indicator:checked { background: %(AMBER)s; border-color: %(AMBER)s; }
QMessageBox { background: %(PANEL)s; }

QMenu { background: %(PANEL)s; border: 1px solid %(LINE)s; padding: 4px; }
QMenu::item { padding: 7px 28px 7px 14px; background: transparent; }
QMenu::item:selected { background: %(PANEL2)s; color: %(AMBER)s; }
QMenu::item:disabled { color: %(DIM)s; }
QMenu::separator { height: 1px; background: %(LINE)s; margin: 4px 8px; }
""" % dict(BG=BG, PANEL=PANEL, PANEL2=PANEL2, LINE=LINE, TEXT=TEXT, MUTED=MUTED, DIM=DIM,
           AMBER=AMBER, BLUE=BLUE, RED=RED, GREEN=GREEN, MONO=MONO, UI=UI)


# ------------------------------------------------------------ small widgets
class HazardBar(QWidget):
    """Thin diagonal hazard stripe used as the top edge of the window."""
    def __init__(self):
        super().__init__()
        self.setFixedHeight(5)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0c0e"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(AMBER))
        h, w, x = self.height(), self.width(), -self.height()
        while x < w:
            p.drawPolygon(QPolygonF([QPointF(x, h), QPointF(x + 9, h),
                                     QPointF(x + 9 + h, 0), QPointF(x + h, 0)]))
            x += 18


class Stat(QFrame):
    def __init__(self, caption, color):
        super().__init__()
        self.setObjectName("stat")
        self.setStyleSheet(f"#stat{{background:{PANEL};border:1px solid {LINE};"
                           f"border-left:3px solid {color};}}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 9, 14, 9)
        lay.setSpacing(0)
        cap = QLabel(caption)
        cap.setStyleSheet(f"color:{MUTED};font-size:10px;font-weight:700;letter-spacing:2px;")
        self.num = QLabel("-")
        self.num.setStyleSheet(f"color:{color};font-size:26px;font-weight:600;font-family:{MONO};")
        lay.addWidget(cap)
        lay.addWidget(self.num)
        self.color = color

    def set(self, n):
        self.num.setText(str(n))
        self.num.setStyleSheet(f"color:{self.color if n else DIM};font-size:26px;"
                               f"font-weight:600;font-family:{MONO};")


def button(text, kind=None, tip=None):
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if kind:
        b.setProperty("kind", kind)
    if tip:
        b.setToolTip(tip)
    return b


def caption(text, color=MUTED):
    l = QLabel(text)
    l.setStyleSheet(f"color:{color};font-size:10px;font-weight:700;letter-spacing:2px;")
    return l


# ------------------------------------------------------------ worker thread
class Emitter(io.TextIOBase):
    def __init__(self, sig, kind):
        self.sig, self.kind = sig, kind

    def write(self, s):
        if s:
            self.sig.emit(s, self.kind)
        return len(s or "")

    def flush(self):
        pass


class Worker(QThread):
    out = pyqtSignal(str, str)
    result = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        so, se = Emitter(self.out, "out"), Emitter(self.out, "err")
        try:
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                res = self.fn()
            self.result.emit(res)
        except SystemExit:
            self.failed.emit("")          # ghsync.die() already wrote the message to stderr
        except Exception as e:            # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


def compute_plan(root):
    root, cfg = ghsync.load_cfg(argparse.Namespace(dir=root))
    base, local, remote, skipped = ghsync.gather(root, cfg)
    push, pull, conflict, same = ghsync.classify(base, local, remote["files"])
    return dict(cfg=cfg, base=base, local=local, remote=remote, push=push, pull=pull,
                conflict=conflict, same=len(same), skipped=skipped)


class WatchWorker(QThread):
    """Background poll for the tray watcher. No stdout redirection, never overlaps a real job."""
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except SystemExit:
            self.failed.emit("not signed in, or folder not linked")
        except Exception as e:            # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


STATE_COLOR = {"ok": GREEN, "pending": AMBER, "conflict": RED,
               "error": "#8a929c", "paused": "#5c636c", "unknown": "#5c636c"}
_ICONS = {}


def make_icon(state):
    """Square status icon: up arrow (push) + down arrow (pull), coloured by state."""
    if state not in _ICONS:
        pm = QPixmap(128, 128)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(STATE_COLOR.get(state, MUTED)))
        p.drawRoundedRect(6, 6, 116, 116, 16, 16)
        p.setBrush(QColor("#111315"))
        p.drawPolygon(QPolygonF([QPointF(42, 22), QPointF(18, 56), QPointF(66, 56)]))
        p.drawRect(33, 56, 18, 48)
        p.drawPolygon(QPolygonF([QPointF(86, 106), QPointF(62, 72), QPointF(110, 72)]))
        p.drawRect(77, 24, 18, 48)
        p.end()
        _ICONS[state] = QIcon(pm)
    return _ICONS[state]


# -------------------------------------------------------------- main window
class Main(QMainWindow):
    LOCAL_POLL_MS = 5000      # how often the folder is checked for edits
    REMOTE_EVERY = 12         # check GitHub every N local polls (12 x 5 s = once a minute)

    def __init__(self, folder=None):
        super().__init__()
        self.setWindowTitle("GHSYNC")
        self.setWindowIcon(make_icon("pending"))
        self.resize(1040, 720)
        self.setMinimumSize(820, 560)
        self.settings = QSettings("ghsync", "gui")
        self.root = os.path.abspath(folder or self.settings.value("folder", os.getcwd()))
        self.plan = None
        self.cfg = None
        self.busy = False
        self.last_refresh = 0.0
        self._workers = set()
        # watcher state
        self.watch_on = self.settings.value("watch", True, type=bool)
        self.watch_busy = False
        self.watch_error = None
        self.last_local = None
        self.local_dirty = False
        self.last_sig = None
        self.tick_n = 0
        self.tray_state = "unknown"
        self._quitting = False
        self._told_tray = False
        self._build()
        self._build_tray()
        QShortcut(QKeySequence("F5"), self, activated=self.refresh)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.watch_tick)
        self.timer.start(self.LOCAL_POLL_MS)
        self.load_folder(self.root)

    # ---- layout
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(HazardBar())

        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 12)
        body.setSpacing(12)
        outer.addLayout(body, 1)

        # header
        head = QHBoxLayout()
        mark = QLabel("GH<span style='color:%s'>SYNC</span>" % AMBER)
        mark.setTextFormat(Qt.TextFormat.RichText)
        mark.setStyleSheet("font-size:20px;font-weight:800;letter-spacing:5px;")
        self.repo_lbl = QLabel("")
        self.repo_lbl.setStyleSheet(f"color:{MUTED};font-family:{MONO};font-size:12px;")
        self.pill = QLabel("")
        head.addWidget(mark)
        head.addStretch(1)
        head.addWidget(self.repo_lbl)
        head.addSpacing(12)
        head.addWidget(self.pill)
        body.addLayout(head)

        # folder bar
        frow = QHBoxLayout()
        frow.setSpacing(8)
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        browse = button("BROWSE")
        browse.clicked.connect(self.browse)
        frow.addWidget(caption("FOLDER"))
        frow.addWidget(self.folder_edit, 1)
        frow.addWidget(browse)
        body.addLayout(frow)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.stack.addWidget(self._connect_page())
        self.stack.addWidget(self._main_page())

        # footer
        foot = QVBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(0)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        foot.addWidget(self.progress)
        sb = QWidget()
        sbl = QHBoxLayout(sb)
        sbl.setContentsMargins(20, 6, 20, 8)
        self.status = QLabel("READY")
        self.status.setStyleSheet(f"color:{MUTED};font-size:10px;font-weight:700;letter-spacing:2px;")
        sbl.addWidget(self.status)
        sbl.addStretch(1)
        self.watch_chk = QCheckBox("WATCH FOLDER")
        self.watch_chk.setChecked(self.watch_on)
        self.watch_chk.setToolTip("Poll the folder and GitHub in the background and notify you "
                                  "when changes are waiting. Closing the window keeps watching in the tray.")
        self.watch_chk.setStyleSheet("font-size:10px;font-weight:700;letter-spacing:2px;")
        self.watch_chk.toggled.connect(self.set_watch)
        sbl.addWidget(self.watch_chk)
        sbl.addSpacing(18)
        hint = QLabel("F5 REFRESH")
        hint.setStyleSheet(f"color:{DIM};font-size:10px;font-weight:700;letter-spacing:2px;")
        sbl.addWidget(hint)
        foot.addWidget(sb)
        outer.addLayout(foot)

    def _main_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.st_push, self.st_pull = Stat("PUSH  /  LOCAL > GITHUB", AMBER), Stat("PULL  /  GITHUB > LOCAL", BLUE)
        self.st_conf, self.st_same = Stat("CONFLICT", RED), Stat("IN SYNC", GREEN)
        for s in (self.st_push, self.st_pull, self.st_conf, self.st_same):
            stats.addWidget(s, 1)
        lay.addLayout(stats)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)

        # change table (+ empty state)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["DIRECTION", "CHANGE", "PATH"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 90)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self.diff_selected())
        self.empty = QLabel("")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"background:{PANEL};border:1px solid {LINE};color:{MUTED};"
                                 f"font-size:11px;font-weight:700;letter-spacing:3px;")
        self.tstack = QStackedWidget()
        self.tstack.addWidget(self.table)
        self.tstack.addWidget(self.empty)
        split.addWidget(self.tstack)

        # log + diff tabs
        self.tabs = QTabWidget()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setPlainText("Select a file and press DIFF (or double-click it).")
        self.tabs.addTab(self.log, "LOG")
        self.tabs.addTab(self.diff, "DIFF")
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)

        # action bar
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.msg = QLineEdit()
        self.msg.setPlaceholderText("Commit message (optional)")
        self.b_keep_local = button("KEEP LOCAL", "warn", "Selected conflicts: overwrite GitHub with your local version")
        self.b_keep_remote = button("KEEP GITHUB", "warn", "Selected conflicts: overwrite your local file with the GitHub version")
        self.b_diff = button("DIFF")
        self.b_refresh = button("REFRESH")
        self.b_pull = button("PULL")
        self.b_push = button("PUSH")
        self.b_sync = button("SYNC", "primary")
        self.b_keep_local.clicked.connect(lambda: self.resolve("local"))
        self.b_keep_remote.clicked.connect(lambda: self.resolve("remote"))
        self.b_diff.clicked.connect(self.diff_selected)
        self.b_refresh.clicked.connect(self.refresh)
        self.b_pull.clicked.connect(lambda: self.do_sync("pull"))
        self.b_push.clicked.connect(lambda: self.do_sync("push"))
        self.b_sync.clicked.connect(lambda: self.do_sync("sync"))
        bar.addWidget(self.msg, 1)
        for b in (self.b_keep_local, self.b_keep_remote, self.b_diff):
            bar.addWidget(b)
        bar.addSpacing(10)
        for b in (self.b_refresh, self.b_pull, self.b_push, self.b_sync):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.action_widgets = [self.msg, self.b_keep_local, self.b_keep_remote, self.b_diff,
                               self.b_refresh, self.b_pull, self.b_push, self.b_sync]
        return page

    def _connect_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(16)

        self.c_title = QLabel("")
        self.c_title.setStyleSheet(f"font-size:15px;font-weight:700;letter-spacing:3px;color:{AMBER};")
        self.c_sub = QLabel("")
        self.c_sub.setWordWrap(True)
        self.c_sub.setStyleSheet(f"color:{MUTED};")
        lay.addWidget(self.c_title)
        lay.addWidget(self.c_sub)

        # token card
        self.token_card = self._card()
        tl = self.token_card.layout()
        tl.addWidget(caption("01   GITHUB TOKEN"))
        hint = QLabel("Create a <b>classic</b> token with the <b>repo</b> scope at "
                      "github.com/settings/tokens. It is stored in your user folder.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED};")
        tl.addWidget(hint)
        trow = QHBoxLayout()
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("ghp_...")
        self.token_edit.returnPressed.connect(self.do_login)
        b = button("SAVE TOKEN", "primary")
        b.clicked.connect(self.do_login)
        trow.addWidget(self.token_edit, 1)
        trow.addWidget(b)
        tl.addLayout(trow)
        lay.addWidget(self.token_card)

        # create / link cards
        self.setup_row = QWidget()
        row = QHBoxLayout(self.setup_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)

        c1 = self._card()
        l1 = c1.layout()
        l1.addWidget(caption("02A   CREATE NEW REPO FROM THIS FOLDER"))
        d1 = QLabel("Creates the repository on GitHub and uploads everything in the folder. No file-count limit.")
        d1.setWordWrap(True)
        d1.setStyleSheet(f"color:{MUTED};")
        l1.addWidget(d1)
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("repo-name")
        self.public_chk = QCheckBox("Public repository (default is private)")
        b1 = button("CREATE && UPLOAD", "primary")
        b1.clicked.connect(self.do_create)
        l1.addWidget(self.new_name)
        l1.addWidget(self.public_chk)
        l1.addStretch(1)
        l1.addWidget(b1)

        c2 = self._card()
        l2 = c2.layout()
        l2.addWidget(caption("02B   LINK TO AN EXISTING REPO"))
        d2 = QLabel("Compares this folder with the repo. Files that are identical show as in sync; "
                    "only real differences appear in the list.")
        d2.setWordWrap(True)
        d2.setStyleSheet(f"color:{MUTED};")
        l2.addWidget(d2)
        self.link_name = QLineEdit()
        self.link_name.setPlaceholderText("owner/repo")
        b2 = button("LINK && COMPARE")
        b2.clicked.connect(self.do_link)
        l2.addWidget(self.link_name)
        l2.addStretch(1)
        l2.addWidget(b2)
        row.addWidget(c1, 1)
        row.addWidget(c2, 1)
        lay.addWidget(self.setup_row)

        self.connect_log = QLabel("")   # reserved spacing
        lay.addStretch(1)
        return page

    def _card(self):
        f = QFrame()
        f.setObjectName("card")
        f.setStyleSheet(f"#card{{background:{PANEL};border:1px solid {LINE};}}")
        l = QVBoxLayout(f)
        l.setContentsMargins(16, 14, 16, 14)
        l.setSpacing(10)
        return f

    # ---- helpers
    def has_token(self):
        return bool(os.environ.get("GITHUB_TOKEN")) or os.path.exists(ghsync.TOKEN_FILE)

    def cfg_path(self):
        return os.path.join(self.root, ghsync.STATE_DIR, "config.json")

    def set_pill(self, text, color):
        self.pill.setText(text)
        self.pill.setStyleSheet(f"color:{color};border:1px solid {color};padding:3px 10px;"
                                f"font-size:10px;font-weight:700;letter-spacing:2px;")
        self.pill.setVisible(bool(text))

    def set_status(self, text, color=MUTED):
        self.status.setText(text.upper())
        self.status.setStyleSheet(f"color:{color};font-size:10px;font-weight:700;letter-spacing:2px;")

    def set_busy(self, busy, text=""):
        self.busy = busy
        self.progress.setRange(0, 0 if busy else 1)
        self.progress.setValue(0)
        for w in self.action_widgets:
            w.setEnabled(not busy)
        if busy:
            self.set_status(text, AMBER)
        else:
            self.update_buttons()

    def log_write(self, text, kind="out"):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(RED if kind == "err" else
                                 AMBER if kind == "mark" else TEXT))
        cur = self.log.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(text, fmt)
        self.log.setTextCursor(cur)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def selected_rows(self):
        rows = sorted({i.row() for i in self.table.selectedItems()})
        out = []
        for r in rows:
            it = self.table.item(r, 0)
            out.append((it.data(Qt.ItemDataRole.UserRole), self.table.item(r, 2).text()))
        return out

    def update_buttons(self):
        if self.busy:
            return
        sel = self.selected_rows()
        has_conf = any(d == "conflict" for d, _ in sel)
        self.b_keep_local.setEnabled(has_conf)
        self.b_keep_remote.setEnabled(has_conf)
        self.b_diff.setEnabled(bool(sel))
        if self.plan:
            self.b_pull.setEnabled(bool(self.plan["pull"]))
            self.b_push.setEnabled(bool(self.plan["push"]))
            self.b_sync.setEnabled(bool(self.plan["push"] or self.plan["pull"]))

    # ---- jobs
    def run_job(self, fn, label, on_ok=None, then_refresh=False):
        if self.busy:
            return
        if self.watch_busy:      # a background poll is mid-flight: retry as soon as it ends
            QTimer.singleShot(150, lambda: self.run_job(fn, label, on_ok, then_refresh))
            return
        self.set_busy(True, label)
        self.log_write(f"\n> {label.upper()}\n", "mark")
        w = Worker(fn)
        self._workers.add(w)
        w.out.connect(self.log_write)
        w.result.connect(lambda res: self._job_ok(res, on_ok, then_refresh))
        w.failed.connect(self._job_failed)
        w.finished.connect(lambda: self._workers.discard(w))
        w.start()

    def _job_ok(self, res, on_ok, then_refresh):
        self.set_busy(False)
        self.set_status("Done", GREEN)
        if on_ok:
            on_ok(res)
        if then_refresh:
            self.refresh()

    def _job_failed(self, msg):
        self.set_busy(False)
        if msg:
            self.log_write(msg + "\n", "err")
        self.set_status("Failed - see log", RED)
        if self.stack.currentIndex() == 1:
            self.tabs.setCurrentIndex(0)

    # ---- folder / state
    def browse(self):
        if self.busy:
            return
        d = QFileDialog.getExistingDirectory(self, "Choose project folder", self.root)
        if d:
            self.load_folder(os.path.abspath(d))

    def load_folder(self, root):
        self.root = root
        self.settings.setValue("folder", root)
        self.folder_edit.setText(root)
        self.plan = None
        self.last_local, self.local_dirty, self.last_sig, self.tick_n = None, False, None, 0
        self.table.setRowCount(0)
        cfg = ghsync.read_json(self.cfg_path(), None)
        self.cfg = cfg
        self.update_tray()
        if cfg and self.has_token():
            self.repo_lbl.setText(f"{cfg['owner']}/{cfg['repo']}  ·  {cfg['branch']}")
            self.stack.setCurrentIndex(1)
            self.refresh()
            return
        self.set_pill("", MUTED)
        self.repo_lbl.setText("")
        self.token_card.setVisible(not self.has_token())
        self.setup_row.setVisible(not cfg)
        if cfg:
            self.c_title.setText("TOKEN REQUIRED")
            self.c_sub.setText(f"This folder is linked to {cfg['owner']}/{cfg['repo']}. "
                               "Add your GitHub token to continue.")
        else:
            self.c_title.setText("FOLDER NOT LINKED")
            self.c_sub.setText("Connect this folder to a GitHub repository to start syncing.")
        self.stack.setCurrentIndex(0)
        self.set_status("Not linked", MUTED)

    def refresh(self):
        if self.busy or self.stack.currentIndex() != 1:
            return
        self.last_refresh = time.time()
        root = self.root
        self.run_job(lambda: compute_plan(root), "Scanning", on_ok=self.apply_plan)

    def apply_plan(self, plan, notify=False):
        keep = {p for _, p in self.selected_rows()}      # keep the user's selection across refreshes
        self.plan = plan
        self.last_local = dict(plan["local"])
        cfg = plan["cfg"]
        self.repo_lbl.setText(f"{cfg['owner']}/{cfg['repo']}  ·  {cfg['branch']}  ·  "
                              f"head {plan['remote']['commit'][:7]}")
        push, pull, conf = plan["push"], plan["pull"], plan["conflict"]
        self.st_push.set(len(push))
        self.st_pull.set(len(pull))
        self.st_conf.set(len(conf))
        self.st_same.set(plan["same"])

        def tag(new, old):
            return "ADD" if old is None else ("DEL" if new is None else "MOD")

        rows = [("conflict", "CONFLICT", "BOTH", p) for p in sorted(conf)]
        rows += [("push", "PUSH", tag(v, plan["remote"]["files"].get(p)), p) for p, v in sorted(push.items())]
        rows += [("pull", "PULL", tag(v, plan["local"].get(p)), p) for p, v in sorted(pull.items())]
        colors = {"conflict": RED, "push": AMBER, "pull": BLUE}
        glyph = {"conflict": "◆", "push": "▲", "pull": "▼"}
        chg_color = {"ADD": GREEN, "DEL": RED, "MOD": MUTED, "BOTH": RED}

        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for r, (kind, label, chg, path) in enumerate(rows):
            a = QTableWidgetItem(f"{glyph[kind]}  {label}")
            a.setData(Qt.ItemDataRole.UserRole, kind)
            a.setForeground(QColor(colors[kind]))
            f = QFont(a.font())
            f.setBold(True)
            a.setFont(f)
            b = QTableWidgetItem(chg)
            b.setForeground(QColor(chg_color[chg]))
            c = QTableWidgetItem(path)
            for it in (a, b, c):
                self.table.setItem(r, (a, b, c).index(it), it)
        for r in range(self.table.rowCount()):
            if self.table.item(r, 2).text() in keep:
                self.table.setRangeSelected(QTableWidgetSelectionRange(r, 0, r, 2), True)
        self.table.setUpdatesEnabled(True)

        if conf:
            self.set_pill("CONFLICTS", RED)
        elif push or pull:
            self.set_pill("CHANGES PENDING", AMBER)
        else:
            self.set_pill("IN SYNC", GREEN)
        if rows:
            self.tstack.setCurrentIndex(0)
        else:
            self.empty.setText("ALL CLEAR  -  FOLDER AND REPO MATCH")
            self.tstack.setCurrentIndex(1)
        if plan["skipped"]:
            self.set_status(f"{len(plan['skipped'])} file(s) over 100 MB skipped (GitHub limit)", AMBER)
        self.update_buttons()
        self.update_tray()
        self.after_plan(notify)

    # ---- actions
    def confirm_deletes(self, mode):
        if not self.plan:
            return True
        dl = sum(v is None for v in self.plan["pull"].values()) if mode in ("pull", "sync") else 0
        dr = sum(v is None for v in self.plan["push"].values()) if mode in ("push", "sync") else 0
        if not (dl or dr):
            return True
        text = (f"This will delete {dl} file(s) from this folder and {dr} file(s) from GitHub.\n\nContinue?")
        return QMessageBox.question(self, "Confirm deletions", text) == QMessageBox.StandardButton.Yes

    def do_sync(self, mode):
        if self.busy or not self.confirm_deletes(mode):
            return
        root, message = self.root, self.msg.text().strip() or None
        ns = argparse.Namespace(dir=root, message=message, dry_run=False, yes=True,
                                prefer=None, resolve=None, only=None)
        self.run_job(lambda: ghsync.run_sync(ns, mode), mode.title() + "ing" if mode != "sync" else "Syncing",
                     on_ok=lambda _: self.msg.clear(), then_refresh=True)

    def resolve(self, side):
        sel = [p for d, p in self.selected_rows() if d == "conflict"]
        if self.busy or not sel:
            return
        loser = "GitHub" if side == "local" else "your local copy"
        if QMessageBox.question(self, "Resolve conflicts",
                                f"Keep the {'local' if side == 'local' else 'GitHub'} version of {len(sel)} file(s)?\n\n"
                                f"The {loser} will be overwritten.") != QMessageBox.StandardButton.Yes:
            return
        root, message = self.root, self.msg.text().strip() or None
        ns = argparse.Namespace(dir=root, message=message, dry_run=False, yes=True, prefer=None,
                                resolve={p: side for p in sel}, only=set(sel))
        self.run_job(lambda: ghsync.run_sync(ns, "sync"), "Resolving", then_refresh=True)

    def diff_selected(self):
        sel = self.selected_rows()
        if self.busy or not sel:
            return
        root, path = self.root, sel[0][1]

        def job():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ghsync.cmd_diff(argparse.Namespace(dir=root, paths=[path]))
            return buf.getvalue()

        self.run_job(job, "Diffing " + path, on_ok=self.show_diff)

    def show_diff(self, text):
        self.diff.clear()
        cur = self.diff.textCursor()
        for line in text.splitlines() or ["No differences."]:
            fmt = QTextCharFormat()
            if line.startswith("==="):
                fmt.setForeground(QColor(AMBER))
                fmt.setFontWeight(700)
            elif line.startswith("@@"):
                fmt.setForeground(QColor(BLUE))
            elif line.startswith("+") and not line.startswith("+++"):
                fmt.setForeground(QColor(GREEN))
                fmt.setBackground(QColor("#14261d"))
            elif line.startswith("-") and not line.startswith("---"):
                fmt.setForeground(QColor(RED))
                fmt.setBackground(QColor("#2a1819"))
            elif line.startswith(("+++", "---")):
                fmt.setForeground(QColor(MUTED))
            else:
                fmt.setForeground(QColor(TEXT))
            cur.insertText(line + "\n", fmt)
        self.diff.moveCursor(QTextCursor.MoveOperation.Start)
        self.tabs.setCurrentIndex(1)

    def do_login(self):
        tok = self.token_edit.text().strip()
        if self.busy or not tok:
            return

        def job():
            had = os.environ.get("GITHUB_TOKEN")
            os.environ["GITHUB_TOKEN"] = tok
            try:
                me = ghsync.api("GET", "/user")
            finally:
                if had is None:
                    os.environ.pop("GITHUB_TOKEN", None)
                else:
                    os.environ["GITHUB_TOKEN"] = had
            with open(ghsync.TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(tok)
            try:
                os.chmod(ghsync.TOKEN_FILE, 0o600)
            except OSError:
                pass
            print(f"Logged in as {me['login']}.")

        def ok(_):
            self.token_edit.clear()
            self.load_folder(self.root)

        self.run_job(job, "Checking token", on_ok=ok)

    def do_create(self):
        name = self.new_name.text().strip()
        if self.busy or not name:
            return
        if not self.has_token():
            self.set_status("Save your token first", RED)
            return
        ns = argparse.Namespace(dir=self.root, name=name, public=self.public_chk.isChecked(),
                                org=None, description=None, message=None)
        self.run_job(lambda: ghsync.cmd_init(ns), "Creating repo and uploading",
                     on_ok=lambda _: self.load_folder(self.root))

    def do_link(self):
        repo = self.link_name.text().strip()
        if self.busy or "/" not in repo:
            self.set_status("Use owner/repo", RED)
            return
        if not self.has_token():
            self.set_status("Save your token first", RED)
            return
        ns = argparse.Namespace(dir=self.root, repo=repo, branch=None)
        self.run_job(lambda: ghsync.cmd_link(ns), "Linking",
                     on_ok=lambda _: self.load_folder(self.root))

    # ---- tray + background watcher
    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_icon("unknown"), self)
        self.tray_menu = QMenu()
        self.act_open = self.tray_menu.addAction("Open GHSYNC")
        self.act_sync = self.tray_menu.addAction("Sync now")
        self.act_watch = self.tray_menu.addAction("Pause watching")
        self.tray_menu.addSeparator()
        self.act_quit = self.tray_menu.addAction("Quit")
        self.act_open.triggered.connect(self.show_window)
        self.act_sync.triggered.connect(self.tray_sync)
        self.act_watch.triggered.connect(lambda: self.watch_chk.setChecked(not self.watch_on))
        self.act_quit.triggered.connect(self.quit_app)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self.show_window)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and self.isActiveWindow():
                self.hide()
            else:
                self.show_window()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_sync(self):
        if self.busy or not self.plan:
            return
        if any(v is None for v in list(self.plan["push"].values()) + list(self.plan["pull"].values())):
            self.show_window()          # deletions need a confirmation dialog the user can see
        self.do_sync("sync")

    def quit_app(self):
        if self.busy:
            self.show_window()
            QMessageBox.information(self, "Busy", "An operation is still running. Wait for it to finish.")
            return
        self._quitting = True
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def set_watch(self, on):
        self.watch_on = bool(on)
        self.settings.setValue("watch", self.watch_on)
        self.tick_n = 0
        self.update_tray()
        if self.watch_on:
            QTimer.singleShot(0, self.watch_tick)

    def update_tray(self):
        p = self.plan
        n = (len(p["push"]) + len(p["pull"]) + len(p["conflict"])) if p else 0
        if self.watch_error:
            state = "error"
        elif not self.watch_on:
            state = "paused"
        elif not p:
            state = "unknown"
        elif p["conflict"]:
            state = "conflict"
        elif p["push"] or p["pull"]:
            state = "pending"
        else:
            state = "ok"
        self.tray_state = state
        self.setWindowTitle("GHSYNC" + (f"  ·  {n} waiting" if n else ""))
        if not self.tray:
            return
        self.tray.setIcon(make_icon(state))
        head = "GHSYNC" + (f" · {self.cfg['owner']}/{self.cfg['repo']}" if self.cfg else "")
        if state == "error":
            detail = "Can't reach GitHub - retrying"
        elif state == "paused":
            detail = "Watching paused"
        elif not p:
            detail = "Not linked yet"
        elif not n:
            detail = "In sync"
        else:
            detail = f"▲ {len(p['push'])} push   ▼ {len(p['pull'])} pull   ◆ {len(p['conflict'])} conflict"
        self.tray.setToolTip(head + "\n" + detail)
        self.act_sync.setEnabled(bool(p and (p["push"] or p["pull"])))
        self.act_watch.setText("Pause watching" if self.watch_on else "Resume watching")

    def notify(self, title, body):
        self.log_write(f"\n[notify] {title}  -  {body.replace(chr(10), '  ')}\n", "mark")
        if self.tray:
            self.tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 10000)

    def after_plan(self, notify):
        """Tell the user when something new is waiting (once per distinct set of changes)."""
        p = self.plan
        sig = hash(tuple(sorted([("u", k, v) for k, v in p["push"].items()] +
                                [("d", k, v) for k, v in p["pull"].items()] +
                                [("c", k, str(v)) for k, v in p["conflict"].items()],
                                key=lambda t: (t[0], t[1]))))
        total = len(p["push"]) + len(p["pull"]) + len(p["conflict"])
        if not total:
            self.last_sig = None
            return
        if sig == self.last_sig:
            return
        self.last_sig = sig
        if not notify or (self.isVisible() and self.isActiveWindow()):
            return              # user-initiated refresh, or they are looking at the window already
        parts = []
        if p["push"]:
            parts.append(f"{len(p['push'])} to push")
        if p["pull"]:
            parts.append(f"{len(p['pull'])} to pull")
        if p["conflict"]:
            parts.append(f"{len(p['conflict'])} conflict(s)")
        names = sorted(list(p["push"]) + list(p["pull"]) + list(p["conflict"]))
        shown = ", ".join(names[:3]) + (" …" if len(names) > 3 else "")
        self.notify("GHSYNC · changes waiting", " · ".join(parts) + "\n" + shown)

    def _start_watch_job(self, fn, on_done):
        self.watch_busy = True
        w = WatchWorker(fn)
        self._workers.add(w)
        w.done.connect(on_done)
        w.failed.connect(self._watch_failed)
        w.finished.connect(lambda: self._workers.discard(w))
        w.start()

    def watch_tick(self):
        if (not self.watch_on or self.busy or self.watch_busy or not self.cfg
                or self.stack.currentIndex() != 1):
            return
        check_remote = self.tick_n % self.REMOTE_EVERY == 0
        self.tick_n += 1
        root, cfg = self.root, self.cfg

        def job():
            local, _ = ghsync.scan_local(root)
            head = None
            if check_remote:
                ref = ghsync.api("GET", f"/repos/{cfg['owner']}/{cfg['repo']}/git/ref/heads/{cfg['branch']}")
                head = ref["object"]["sha"]
            return local, head

        self._start_watch_job(job, self._tick_done)

    def _tick_done(self, res):
        self.watch_busy = False
        local, head = res
        recovered = False
        if head is not None and self.watch_error:
            self.watch_error, recovered = None, True
            self.update_tray()
        trigger = recovered or self.plan is None
        if self.last_local is not None:
            if local != self.last_local:
                self.last_local, self.local_dirty = local, True     # still changing: wait for quiet
            elif self.local_dirty:
                self.local_dirty, trigger = False, True             # quiet for one poll: re-compare
        if head is not None and self.plan and head != self.plan["remote"]["commit"]:
            trigger = True
        if trigger:
            self.watch_refresh()

    def watch_refresh(self):
        if self.busy or self.watch_busy or not self.cfg:
            return
        root = self.root
        self._start_watch_job(lambda: compute_plan(root), self._watch_plan)

    def _watch_plan(self, plan):
        self.watch_busy = False
        self.last_refresh = time.time()
        self.apply_plan(plan, notify=True)

    def _watch_failed(self, msg):
        self.watch_busy = False
        if msg != self.watch_error:
            self.watch_error = msg
            self.log_write(f"\n[watch] {msg}\n", "err")
            self.update_tray()

    # ---- window events
    def changeEvent(self, e):
        if (e.type() == QEvent.Type.ActivationChange and self.isActiveWindow() and not self.busy
                and self.stack.currentIndex() == 1 and time.time() - self.last_refresh > 15):
            self.refresh()
        super().changeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        if sys.platform == "win32":          # dark title bar on Windows 10/11
            try:
                import ctypes
                hwnd = int(self.winId())
                val = ctypes.c_int(1)
                for attr in (20, 19):
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
            except Exception:
                pass

    def closeEvent(self, e):
        if self.tray and self.watch_on and not self._quitting:
            e.ignore()                      # keep watching: just hide to the tray
            self.hide()
            if not self._told_tray:
                self._told_tray = True
                self.tray.showMessage("GHSYNC is still watching",
                                      "Right-click the tray icon to open, pause or quit.",
                                      QSystemTrayIcon.MessageIcon.Information, 5000)
        elif self.busy:
            QMessageBox.information(self, "Busy", "An operation is still running. Wait for it to finish.")
            e.ignore()
        else:
            e.accept()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    app.setWindowIcon(make_icon("pending"))
    win = Main(args[0] if args else None)
    app.setQuitOnLastWindowClosed(win.tray is None)      # stay alive in the tray when it exists
    if "--tray" in sys.argv and win.tray:
        pass                                              # start hidden; the icon is the way in
    else:
        win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
