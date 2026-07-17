#!/usr/bin/env python3
"""QuickPlay — minimal, instant audio player.

Double-click an audio file → it just plays. No library, no scan.
Showtime-style: frameless rounded window, cover art fills it, controls
integrated at the bottom. Drag anywhere to move, Esc/× to close.

Usage: quickplay.py <file> [file ...]
"""
import sys
import os
from PySide6.QtCore import Qt, QUrl, QTimer, QPoint, QRectF
from PySide6.QtGui import (QKeySequence, QShortcut, QPixmap, QPainter, QColor,
                           QLinearGradient, QPainterPath, QFont, QIcon)
from PySide6.QtWidgets import (QApplication, QWidget, QHBoxLayout, QVBoxLayout,
                               QSlider, QLabel, QToolButton, QStyle, QGraphicsDropShadowEffect)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaMetaData
from PySide6.QtNetwork import QLocalServer, QLocalSocket

RADIUS = 16
SERVER_NAME = f"quickplay-{os.getuid()}"


def fmt(ms):
    s = max(0, ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


class Cover(QLabel):
    """Cover-art / placeholder area — rounded, gradient fallback with a note."""
    def __init__(self):
        super().__init__()
        self.setMinimumSize(360, 300)
        self.pix = None
        self.on_resize = None
        self.setAlignment(Qt.AlignCenter)

    def resizeEvent(self, e):
        # overlayt (title, close, min) positioidaan vasta kun coverilla on oikea koko
        if self.on_resize:
            self.on_resize()
        super().resizeEvent(e)

    def set_art(self, pixmap):
        self.pix = pixmap
        self.update()

    # vedä ikkunaa kansikuvasta — startSystemMove toimii myös Waylandilla
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.window().windowHandle().startSystemMove()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        path = QPainterPath()
        path.addRoundedRect(QRectF(r).adjusted(0, 0, -0.5, -0.5), RADIUS, RADIUS)
        p.setClipPath(path)
        if self.pix and not self.pix.isNull():
            scaled = self.pix.scaled(r.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = (scaled.width() - r.width()) // 2
            y = (scaled.height() - r.height()) // 2
            p.drawPixmap(r, scaled, scaled.rect().adjusted(x, y, -x, -y))
            # darken bottom for control legibility
            g = QLinearGradient(0, r.height() * 0.55, 0, r.height())
            g.setColorAt(0, QColor(0, 0, 0, 0)); g.setColorAt(1, QColor(0, 0, 0, 190))
            p.fillRect(r, g)
        else:
            g = QLinearGradient(0, 0, r.width(), r.height())
            g.setColorAt(0, QColor("#2b2f36")); g.setColorAt(1, QColor("#16181c"))
            p.fillRect(r, g)
            p.setPen(QColor(255, 255, 255, 40))
            f = QFont(); f.setPixelSize(int(min(r.width(), r.height()) * 0.34)); p.setFont(f)
            p.drawText(r, Qt.AlignCenter, "♪")


class QuickPlay(QWidget):
    def __init__(self, files):
        super().__init__()
        self.files = [f for f in files if os.path.exists(f)]
        self.index = 0
        self._scrub = False

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.9)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(380, 440)
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickplay.svg")
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))

        # rounded container
        self.card = QWidget(self)
        self.card.setObjectName("card")
        shadow = QGraphicsDropShadowEffect(blurRadius=40, xOffset=0, yOffset=8)
        shadow.setColor(QColor(0, 0, 0, 160))
        self.card.setGraphicsEffect(shadow)

        self.cover = Cover()
        self.title = QLabel("—", self.cover)
        self.title.setStyleSheet("color:#fff;font-size:15px;font-weight:600;background:transparent;")
        self.title.setWordWrap(True)

        st = self.style()
        def btn(icon):
            b = QToolButton(); b.setAutoRaise(True)
            b.setIcon(st.standardIcon(icon))
            b.setIconSize(b.iconSize() * 1.1)
            b.setStyleSheet("QToolButton{color:#fff;border:0;padding:6px;}"
                            "QToolButton:hover{background:rgba(255,255,255,0.12);border-radius:8px;}")
            return b
        self.prev_btn = btn(QStyle.SP_MediaSkipBackward); self.prev_btn.clicked.connect(lambda: self.jump(-1))
        self.play_btn = btn(QStyle.SP_MediaPause); self.play_btn.clicked.connect(self.toggle)
        self.next_btn = btn(QStyle.SP_MediaSkipForward); self.next_btn.clicked.connect(lambda: self.jump(1))
        for b in (self.prev_btn, self.next_btn):
            b.setVisible(len(self.files) > 1)

        self.loop = False
        self.loop_btn = btn(QStyle.SP_BrowserReload)
        self.loop_btn.setCheckable(True)
        self.loop_btn.setToolTip("Toista uudelleen (loop)")
        self.loop_btn.setStyleSheet("QToolButton{color:#8a8f96;border:0;padding:6px;}"
                                    "QToolButton:hover{background:rgba(255,255,255,0.12);border-radius:8px;}"
                                    "QToolButton:checked{color:#4a9eff;}")
        self.loop_btn.toggled.connect(lambda v: setattr(self, "loop", v))

        def wbtn(txt, hover):
            b = QToolButton(self.cover); b.setText(txt)
            b.setStyleSheet("QToolButton{color:#fff;background:rgba(0,0,0,0.40);border-radius:13px;"
                            "font-size:13px;font-weight:bold;width:26px;height:26px;border:0;}"
                            "QToolButton:hover{background:%s;}" % hover)
            return b
        self.min_btn = wbtn("–", "rgba(255,255,255,0.30)")   # –
        self.min_btn.clicked.connect(self.showMinimized)
        self.close_btn = wbtn("✕", "rgba(232,72,72,0.85)")   # ✕
        self.close_btn.clicked.connect(self.close)
        self.cover.on_resize = self.place_overlays

        self.seek = QSlider(Qt.Horizontal); self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.seek.sliderPressed.connect(lambda: setattr(self, "_scrub", True))
        self.seek.sliderReleased.connect(lambda: setattr(self, "_scrub", False))

        self.tpos = QLabel("0:00"); self.tdur = QLabel("0:00")
        for t in (self.tpos, self.tdur):
            t.setStyleSheet("color:#cfd3d8;font-size:11px;background:transparent;")

        self.vol = QSlider(Qt.Horizontal); self.vol.setRange(0, 100); self.vol.setValue(90); self.vol.setFixedWidth(90)
        self.vol.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))

        srow = QHBoxLayout(); srow.setContentsMargins(0, 0, 0, 0)
        srow.addWidget(self.tpos); srow.addWidget(self.seek, 1); srow.addWidget(self.tdur)
        crow = QHBoxLayout(); crow.setContentsMargins(0, 0, 0, 0)
        crow.addWidget(self.prev_btn); crow.addWidget(self.play_btn); crow.addWidget(self.next_btn)
        crow.addWidget(self.loop_btn)
        crow.addStretch(1)
        vlab = QLabel("\U0001F509"); vlab.setStyleSheet("color:#cfd3d8;background:transparent;")
        crow.addWidget(vlab); crow.addWidget(self.vol)

        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(0, 0, 0, 0); cl.setSpacing(0)
        cl.addWidget(self.cover, 1)
        controls = QWidget(); controls.setObjectName("controls")
        clv = QVBoxLayout(controls); clv.setContentsMargins(14, 8, 14, 12); clv.setSpacing(4)
        clv.addLayout(srow); clv.addLayout(crow)
        cl.addWidget(controls)

        self.card.setStyleSheet(
            "#card{background:#16181c;border-radius:%dpx;}"
            "#controls{background:#16181c;border-bottom-left-radius:%dpx;border-bottom-right-radius:%dpx;}"
            "QSlider::groove:horizontal{height:4px;background:rgba(255,255,255,0.18);border-radius:2px;}"
            "QSlider::sub-page:horizontal{background:#4a9eff;border-radius:2px;}"
            "QSlider::handle:horizontal{background:#fff;width:12px;margin:-5px 0;border-radius:6px;}" % (RADIUS, RADIUS, RADIUS)
        )

        self.player.positionChanged.connect(self.on_pos)
        self.player.durationChanged.connect(self.on_dur)
        self.player.playbackStateChanged.connect(self.on_state)
        self.player.mediaStatusChanged.connect(self.on_status)
        self.player.metaDataChanged.connect(self.on_meta)

        QShortcut(QKeySequence(Qt.Key_Space), self, self.toggle)
        QShortcut(QKeySequence(Qt.Key_Right), self, lambda: self.player.setPosition(self.player.position() + 5000))
        QShortcut(QKeySequence(Qt.Key_Left), self, lambda: self.player.setPosition(self.player.position() - 5000))
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.close)

        if self.files:
            self.load(0)
        else:
            self.title.setText("Ei tiedostoa")

    def resizeEvent(self, e):
        self.card.setGeometry(self.rect())

    def place_overlays(self):
        w, h = self.cover.width(), self.cover.height()
        self.title.setGeometry(14, h - 46, w - 28, 40)
        self.close_btn.setGeometry(w - 36, 10, 26, 26)
        self.min_btn.setGeometry(w - 68, 10, 26, 26)

    # frameless drag — startSystemMove toimii myös Waylandilla
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.windowHandle().startSystemMove()

    def load(self, i):
        self.index = i % len(self.files)
        f = self.files[self.index]
        n = f"  ({self.index + 1}/{len(self.files)})" if len(self.files) > 1 else ""
        self.title.setText(os.path.basename(f) + n)
        self.cover.set_art(None)
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(f)))
        self.player.play()

    def on_meta(self):
        md = self.player.metaData()
        img = md.value(QMediaMetaData.CoverArtImage)
        if img is None:
            img = md.value(QMediaMetaData.ThumbnailImage)
        if img is not None:
            self.cover.set_art(QPixmap.fromImage(img))
        t = md.value(QMediaMetaData.Title)
        a = md.value(QMediaMetaData.AlbumArtist) or md.value(QMediaMetaData.ContributingArtist)
        if isinstance(a, (list, tuple)):
            a = ", ".join(str(x) for x in a)
        if t:
            self.title.setText(f"{t}\n{a}" if a else str(t))

    def toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def jump(self, d):
        if self.files:
            self.load(self.index + d)

    def on_pos(self, p):
        if not self._scrub:
            self.seek.setValue(p)
        self.tpos.setText(fmt(p))

    def on_dur(self, d):
        self.seek.setRange(0, d); self.tdur.setText(fmt(d))

    def on_state(self, s):
        icon = QStyle.SP_MediaPause if s == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay
        self.play_btn.setIcon(self.style().standardIcon(icon))

    def on_status(self, s):
        if s == QMediaPlayer.EndOfMedia:
            if self.loop:                                  # toista sama uudelleen
                self.player.setPosition(0); self.player.play()
            elif self.index + 1 < len(self.files):         # jono eteenpäin
                self.load(self.index + 1)
            # jonon lopussa jää auki (ei sulkeudu)

    def open_files(self, files):
        """Uusi biisi/biisit olemassa olevaan instanssiin — korvaa jonon, soita, nosta."""
        files = [f for f in files if os.path.exists(f)]
        if not files:
            return
        self.files = files
        self.load(0)
        self.showNormal()
        self.raise_()
        self.activateWindow()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("QuickPlay")
    files = [os.path.abspath(f) for f in sys.argv[1:]]

    # jos instanssi jo pyörii → lähetä sille biisit ja poistu
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(("\n".join(files)).encode()); sock.flush()
        sock.waitForBytesWritten(1000); sock.disconnectFromServer()
        return

    QLocalServer.removeServer(SERVER_NAME)  # siivoa mahd. jämä
    w = QuickPlay(files)

    server = QLocalServer()
    server.listen(SERVER_NAME)

    def on_conn():
        c = server.nextPendingConnection()
        if c and c.waitForReadyRead(1000):
            data = bytes(c.readAll()).decode()
            w.open_files([f for f in data.split("\n") if f])
    server.newConnection.connect(on_conn)

    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
