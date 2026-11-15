#!/usr/bin/env python3
"""The installer's window in the live desktop, WebKitGTK version: the local
page (graveyard-installer) in a GTK 4 window, without a browser. Used while
the image has no Qt WebEngine (window.qml)."""
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gtk, WebKit  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("http") else "http://127.0.0.1:8475/"


def same_page(view, decision, kind):
    # links to other sites open nowhere: this window is the installer only
    if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
        decision.ignore()
        return True
    if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
        uri = decision.get_navigation_action().get_request().get_uri()
        if not uri.startswith(URL.rstrip("/")):
            decision.ignore()
            return True
    return False


def activate(app):
    win = Gtk.ApplicationWindow(application=app, title="Install GraveyardOS")
    win.set_default_size(1100, 760)
    win.set_size_request(720, 520)
    view = WebKit.WebView()
    black = Gdk.RGBA()
    black.parse("#000000")
    view.set_background_color(black)
    settings = view.get_settings()
    settings.set_enable_developer_extras(False)
    settings.set_enable_back_forward_navigation_gestures(False)
    view.connect("decide-policy", same_page)
    view.connect("context-menu", lambda *a: True)
    view.load_uri(URL)
    win.set_child(view)
    win.present()


app = Gtk.Application(application_id="org.graveyardos.Installer")
app.connect("activate", activate)
sys.exit(app.run([sys.argv[0]]))
