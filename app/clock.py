from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import Qt, QTimer, QRect
from PyQt5.QtGui import QFont, QColor, QPainter
import sys
from datetime import datetime
import locale
import random
import time

class OverlayClock(QWidget):
    def __init__(self):
        super().__init__()
        try:
            locale.setlocale(locale.LC_TIME, 'ro_RO.UTF-8')
        except:
            try:
                locale.setlocale(locale.LC_TIME, 'ro_RO')
            except:
                print("Warning: Romanian locale not available")

        # Настройки окна для прозрачности
        self.setWindowFlags(
            Qt.FramelessWindowHint | 
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        
        self.clock_width = 500
        self.clock_height = 200
        self.setGeometry(0, 0, self.clock_width, self.clock_height)

        self.time = ""
        self.date = ""

        # Защита от выгорания
        self.base_x = 0
        self.base_y = 0
        self.last_move_time = time.time()
        self.move_interval = 300
        self.moved = False
        self.offset_x = 0
        self.offset_y = 0

        self.timer = QTimer()
        self.timer.timeout.connect(self.updateTime)
        self.timer.start(1000)
        self.updateTime()
        self.show()

    def updateTime(self):
        now = datetime.now()
        self.time = now.strftime("%H:%M")
        try:
            current_date = now.strftime("%-d %B %Y").lower()
            months_ro = {
                'january': 'ianuarie', 'february': 'februarie',
                'march': 'martie', 'april': 'aprilie', 'may': 'mai',
                'june': 'iunie', 'july': 'iulie', 'august': 'august',
                'september': 'septembrie', 'october': 'octombrie',
                'november': 'noiembrie', 'december': 'decembrie'
            }
            for en, ro in months_ro.items():
                current_date = current_date.replace(en, ro)
        except:
            current_date = now.strftime("%d-%m-%Y")
        self.date = current_date

        if time.time() - self.last_move_time > self.move_interval:
            self.move_randomly()
            self.last_move_time = time.time()

        self.update()

    def move_randomly(self):
        if not self.moved:
            self.offset_x = random.choice([-1, 1]) * random.randint(50, 150)
            self.offset_y = random.choice([-1, 1]) * random.randint(50, 150)
            self.move(self.base_x + self.offset_x, self.base_y + self.offset_y)
            self.moved = True
        else:
            self.move(self.base_x, self.base_y)
            self.moved = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        # Красивый фон с закругленными углами
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 120))  # Полупрозрачный черный
        painter.drawRoundedRect(self.rect(), 15, 15)  # Закругленные углы

        # Или можно добавить обводку:
        # painter.setPen(QColor(245, 222, 179, 100))  # Цвет wheat полупрозрачный
        # painter.setBrush(QColor(0, 0, 0, 120))
        # painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 12, 12)

        time_font = QFont('Arial', 90, QFont.Bold)
        date_font = QFont('Arial', 22)

        # Время с тенью
        painter.setFont(time_font)
        painter.setPen(QColor(0, 0, 0, 150))
        painter.drawText(QRect(3, 3, self.width(), self.height()), Qt.AlignHCenter, self.time)
        painter.setPen(QColor("#F5DEB3"))
        painter.drawText(self.rect(), Qt.AlignHCenter, self.time)

        # Дата с тенью
        painter.setFont(date_font)
        painter.setPen(QColor(0, 0, 0, 150))
        painter.drawText(QRect(2, self.height() // 2 + 37, self.width(), 100), Qt.AlignHCenter | Qt.AlignTop, self.date)
        painter.setPen(QColor("#F5DEB3"))
        painter.drawText(QRect(0, self.height() // 2 + 35, self.width(), 100), Qt.AlignHCenter | Qt.AlignTop, self.date)

    def mousePressEvent(self, event):
        self.oldPos = event.globalPos()

    def mouseMoveEvent(self, event):
        delta = event.globalPos() - self.oldPos
        self.move(self.x() + delta.x(), self.y() + delta.y())
        self.oldPos = event.globalPos()
        self.base_x = self.x()
        self.base_y = self.y()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    clock = OverlayClock()
    sys.exit(app.exec_())
