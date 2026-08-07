"""Полупрозрачные часы поверх киоска.

Настройки живут в /var/lib/kiosk/clock.json и правятся со страницы пульта
(/surface/clock-settings); файл перечитывается на лету по mtime:

    {"position": "top-right", "scale": 100, "opacity": 55}

position: top-left | top-right | bottom-left | bottom-right | center
scale:    30-300 — размер в процентах от базового (500x200)
opacity:  0-100  — прозрачность подложки (0 — совсем без фона)

Фон честно полупрозрачный только при работающем композиторе (picom из
.xinitrc); без него X рисует чёрный ящик.
"""

from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import Qt, QTimer, QRect
from PyQt5.QtGui import QFont, QColor, QPainter
import json
import os
import sys
from datetime import datetime
import random
import time

SETTINGS_FILE = "/var/lib/kiosk/clock.json"
DEFAULTS = {"position": "top-right", "scale": 100, "opacity": 55}
BASE_W, BASE_H = 500, 170
MARGIN = 24

MONTHS_RO = {
    'january': 'ianuarie', 'february': 'februarie', 'march': 'martie',
    'april': 'aprilie', 'may': 'mai', 'june': 'iunie', 'july': 'iulie',
    'august': 'august', 'september': 'septembrie', 'october': 'octombrie',
    'november': 'noiembrie', 'december': 'decembrie',
}


class OverlayClock(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.settings = dict(DEFAULTS)
        self.settings_mtime = 0.0
        self.time_text = ""
        self.date_text = ""

        # Защита от выгорания: лёгкое покачивание вокруг выбранного угла
        self.jiggle_x = 0
        self.jiggle_y = 0
        self.last_move = time.time()

        self.reload_settings(force=True)

        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.tick()
        self.show()

    # ------------------------------------------------------------- настройки --
    def reload_settings(self, force=False):
        try:
            mtime = os.path.getmtime(SETTINGS_FILE)
        except OSError:
            mtime = 0.0
        if not force and mtime == self.settings_mtime:
            return
        self.settings_mtime = mtime
        merged = dict(DEFAULTS)
        try:
            with open(SETTINGS_FILE) as f:
                stored = json.load(f)
            merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        self.settings = merged
        self.apply_geometry()

    def apply_geometry(self):
        scale = max(30, min(300, int(self.settings.get("scale", 100)))) / 100.0
        w, h = int(BASE_W * scale), int(BASE_H * scale)
        screen = QApplication.primaryScreen().geometry()
        pos = self.settings.get("position", "top-right")
        x = {"top-left": MARGIN, "bottom-left": MARGIN,
             "center": (screen.width() - w) // 2}.get(pos, screen.width() - w - MARGIN)
        y = {"top-left": MARGIN, "top-right": MARGIN,
             "center": (screen.height() - h) // 2}.get(pos, screen.height() - h - MARGIN)
        self.setGeometry(x + self.jiggle_x, y + self.jiggle_y, w, h)

    # ---------------------------------------------------------------- время --
    def tick(self):
        now = datetime.now()
        self.time_text = now.strftime("%H:%M")
        date = now.strftime("%-d %B %Y").lower()
        for en, ro in MONTHS_RO.items():
            date = date.replace(en, ro)
        self.date_text = date

        self.reload_settings()

        if time.time() - self.last_move > 300:
            self.jiggle_x = random.randint(-10, 10)
            self.jiggle_y = random.randint(-10, 10)
            self.last_move = time.time()
            self.apply_geometry()

        self.update()

    # ------------------------------------------------------------- отрисовка --
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        scale = max(30, min(300, int(self.settings.get("scale", 100)))) / 100.0
        opacity = max(0, min(100, int(self.settings.get("opacity", 55))))

        if opacity > 0:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, round(opacity * 2.55)))
            painter.drawRoundedRect(self.rect(), int(18 * scale), int(18 * scale))

        time_font = QFont('Arial', max(10, int(84 * scale)), QFont.Bold)
        date_font = QFont('Arial', max(6, int(20 * scale)))

        area = self.rect().adjusted(0, int(6 * scale), 0, 0)
        painter.setFont(time_font)
        painter.setPen(QColor(0, 0, 0, 150))
        painter.drawText(area.translated(2, 2), Qt.AlignHCenter | Qt.AlignTop, self.time_text)
        painter.setPen(QColor("#F5DEB3"))
        painter.drawText(area, Qt.AlignHCenter | Qt.AlignTop, self.time_text)

        # Дата сразу под временем, а не прижатая к нижнему краю панели —
        # иначе между ними зияла дыра.
        date_top = int((6 + 118) * scale)
        painter.setFont(date_font)
        painter.setPen(QColor(255, 255, 255, 200))
        painter.drawText(self.rect().adjusted(0, date_top, 0, 0),
                         Qt.AlignHCenter | Qt.AlignTop, self.date_text)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    clock = OverlayClock()
    sys.exit(app.exec_())
