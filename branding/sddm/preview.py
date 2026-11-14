#!/usr/bin/env python3
"""Render the GraveyardOS login screen to PNG files without SDDM.

    python3 branding/sddm/preview.py OUT_DIR [--size 1920x1080]

Needs PySide6. SDDM's objects (sddm, userModel, sessionModel, config,
keyboard) are imitated with a user and two sessions; the theme is shown in
dark mode, light mode, after a wrong password, and with the power menu open.
The Dunes are taken from branding/wallpapers/Graveyard-Dunes.
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, Property, Qt,  # noqa: E402
                            QTimer, QUrl, Signal, Slot)
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlPropertyMap  # noqa: E402
from PySide6.QtQuick import QQuickView  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
DUNES = os.path.join(REPO, "branding", "wallpapers", "Graveyard-Dunes", "contents")


class ListModel(QAbstractListModel):
    def __init__(self, rows, roles, last=0):
        super().__init__()
        self._rows, self._roles, self._last = rows, roles, last

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def data(self, index, role):
        name = self._roles.get(role)
        return self._rows[index.row()].get(name.decode()) if name else None

    def roleNames(self):
        return self._roles

    def get_last(self):
        return self._last

    lastIndex = Property(int, get_last, constant=True)


class Sddm(QObject):
    loginFailed = Signal()
    loginSucceeded = Signal()

    def _yes(self):
        return True

    canSuspend = Property(bool, _yes, constant=True)
    canReboot = Property(bool, _yes, constant=True)
    canPowerOff = Property(bool, _yes, constant=True)

    @Slot(str, str, int)
    def login(self, user, password, session):
        print(f"login {user} (session {session})")
        QTimer.singleShot(50, self.loginFailed.emit)

    @Slot()
    def suspend(self):
        pass

    @Slot()
    def reboot(self):
        pass

    @Slot()
    def powerOff(self):
        pass


class Keyboard(QObject):
    def _no(self):
        return False

    capsLock = Property(bool, _no, constant=True)


def main():
    out = sys.argv[1]
    w, h = (int(x) for x in (sys.argv[sys.argv.index("--size") + 1] if "--size" in sys.argv else "1920x1080").split("x"))
    os.makedirs(out, exist_ok=True)
    theme = tempfile.mkdtemp()
    shutil.copytree(os.path.join(HERE, "graveyard"), theme, dirs_exist_ok=True)
    os.makedirs(os.path.join(theme, "backgrounds"))
    shutil.copy(os.path.join(DUNES, "images_dark", "1920x1080.jpg"), os.path.join(theme, "backgrounds", "night.jpg"))
    shutil.copy(os.path.join(DUNES, "images", "1920x1080.jpg"), os.path.join(theme, "backgrounds", "day.jpg"))

    app = QGuiApplication(sys.argv)
    roles = {Qt.UserRole + 1: b"name", Qt.UserRole + 2: b"realName", Qt.UserRole + 3: b"icon"}
    users = ListModel([{"name": "tfem", "realName": "tfem", "icon": ""},
                       {"name": "guest", "realName": "Guest", "icon": ""}], roles)
    sessions = ListModel([{"name": "Plasma (Wayland)"}, {"name": "Hyprland"}], {Qt.UserRole + 1: b"name"})
    config = QQmlPropertyMap()
    config.insert("mode", "dark")
    config.insert("backgroundNight", "backgrounds/night.jpg")
    config.insert("backgroundDay", "backgrounds/day.jpg")
    sddm, keyboard = Sddm(), Keyboard()

    view = QQuickView()
    ctx = view.rootContext()
    for name, obj in (("sddm", sddm), ("userModel", users), ("sessionModel", sessions),
                      ("config", config), ("keyboard", keyboard)):
        ctx.setContextProperty(name, obj)
    view.setResizeMode(QQuickView.SizeRootObjectToView)
    view.resize(w, h)
    view.setSource(QUrl.fromLocalFile(os.path.join(theme, "Main.qml")))
    if view.status() != QQuickView.Ready:
        for e in view.errors():
            print(e.toString())
        sys.exit(1)
    view.show()
    root = view.rootObject()

    steps = []

    def shot(name):
        view.grabWindow().save(os.path.join(out, name))
        print("wrote", os.path.join(out, name))

    def at(ms, f):
        steps.append((ms, f))

    at(1500, lambda: shot("graveyard-sddm-dark.png"))
    at(1600, lambda: root.setProperty("dark", False))
    at(2600, lambda: shot("graveyard-sddm-light.png"))
    at(2700, lambda: (root.setProperty("dark", True), root.login()))
    at(3600, lambda: shot("graveyard-sddm-wrong-password.png"))
    at(3700, lambda: root.findChild(QObject, "password") and None)
    at(4500, app.quit)
    for ms, f in steps:
        QTimer.singleShot(ms, f)
    app.exec()
    shutil.rmtree(theme, ignore_errors=True)


if __name__ == "__main__":
    main()
