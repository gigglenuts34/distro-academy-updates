#!/usr/bin/env python3
"""Distro Academy runtime: the native window, saving, the AI bridge and auto-updates.
Started by the stable launcher stub (/usr/bin/distro-academy), and itself updatable through the signed feed."""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import gi

# GPU rendering is the default (it's ~10x smoother). "Safe graphics" turns off WebKit's DMA-BUF
# renderer for the rare GPU/driver combos that show a blank window; it's slower but always works.
SAFE_GFX = "--safe-graphics" in sys.argv or os.environ.get("DISTRO_ACADEMY_SAFE_GRAPHICS") == "1"
if "--safe-graphics" in sys.argv:
    sys.argv.remove("--safe-graphics")
if SAFE_GFX:
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gio, GLib, Gtk, WebKit2  # noqa: E402

APP_ID = "io.github.DistroAcademy"
APP_DIR = os.environ.get("DISTRO_ACADEMY_DIR", "/usr/share/distro-academy")
DATA_DIR = os.path.join(GLib.get_user_data_dir(), "distro-academy")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
WINDOW_FILE = os.path.join(DATA_DIR, "window.json")
DEBUG = bool(os.environ.get("DISTRO_ACADEMY_DEBUG"))
# The page may only reach the optional AI providers: Claude's API or a local Ollama.
HTTP_HOSTS = {"api.anthropic.com", "localhost", "127.0.0.1", "::1"}
UPDATE_DIR = os.path.join(DATA_DIR, "updates")
CURRENT_FILE = os.path.join(UPDATE_DIR, "current.json")
CHECK_FILE = os.path.join(UPDATE_DIR, "last-check.json")
# Written at build time: this package's version, the update feed and the release public key.
UPDATE_CONF = {}
MAX_MANIFEST, MAX_APP = 64 * 1024, 40 * 1024 * 1024
CHECK_EVERY = 3600
SAVE_DIRS = {
    "pictures": GLib.UserDirectory.DIRECTORY_PICTURES,
    "documents": GLib.UserDirectory.DIRECTORY_DOCUMENTS,
}


def read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def feed_allowed(url):
    u = urllib.parse.urlparse(url)
    if u.scheme == "https":
        return True
    # Local feeds are only for testing a release before publishing it.
    return os.environ.get("DISTRO_ACADEMY_INSECURE_FEED") == "1" and (u.scheme == "file" or u.hostname in ("localhost", "127.0.0.1"))


def fetch(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "DistroAcademy/%s" % UPDATE_CONF.get("version", "?"), "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download is larger than expected")
    return data


def downloaded_update():
    """The newest downloaded app, if it's newer than the installed package and still intact."""
    cur = read_json(CURRENT_FILE, None)
    if not cur:
        return None
    ver = str(cur.get("version", ""))
    if vtuple(ver) <= vtuple(UPDATE_CONF.get("version", "0")):
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)  # the package caught up: drop old downloads
        return None
    path = os.path.join(UPDATE_DIR, ver, "index.html")
    try:
        with open(path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != cur.get("sha256"):
                return None
    except OSError:
        return None
    return {"version": ver, "path": path, "notes": cur.get("notes", []), "date": cur.get("date", ""),
            "core_changed": bool(cur.get("core_sha256")) and cur.get("core_sha256") != RUNNING_CORE_SHA}


# ---------- desktop notifications (freedesktop D-Bus, works on KDE, GNOME, XFCE, ...) ----------
NOTIFY = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications")


def notify(title, body, actions=()):
    """Shows a desktop notification and returns its id (0 if there's no notification service)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        acts = [x for pair in actions for x in pair]
        hints = {"desktop-entry": GLib.Variant("s", "distro-academy"), "urgency": GLib.Variant("y", 1)}
        args = GLib.Variant("(susssasa{sv}i)", ("Distro Academy", 0, "distro-academy", str(title)[:120],
                                                 GLib.markup_escape_text(str(body)[:400]), acts, hints, -1))
        res = bus.call_sync(*NOTIFY, "Notify", args, GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        return res.unpack()[0]
    except GLib.Error:
        return 0


def on_notification_click(handler):
    """handler(notification_id, action_key) for clicks on any of our notifications."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return
    bus.signal_subscribe(NOTIFY[0], NOTIFY[2], "ActionInvoked", NOTIFY[1], None, Gio.DBusSignalFlags.NONE,
                         lambda _c, _s, _p, _i, _n, params: handler(*params.unpack()))


# ---------- the daily streak reminder: a systemd user timer that runs "distro-academy --remind" ----------
SYSTEMD_USER = os.path.join(GLib.get_user_config_dir(), "systemd", "user")
TIMER_NAME = "distro-academy-reminder"


def set_reminder(on, hour):
    """Turns the reminder on (at hour:00 every day) or off. Returns {ok, message}."""
    if not shutil.which("systemctl"):
        return {"ok": False, "message": "This system doesn't use systemd, so reminders can't run while the app is closed."}
    svc, tmr = (os.path.join(SYSTEMD_USER, TIMER_NAME + ext) for ext in (".service", ".timer"))
    run = lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True, timeout=20)
    try:
        if not on:
            run("disable", "--now", TIMER_NAME + ".timer")
            for f in (svc, tmr):
                if os.path.exists(f):
                    os.remove(f)
            run("daemon-reload")
            return {"ok": True, "message": "off"}
        hour = max(0, min(23, int(hour)))
        exe = os.environ.get("DISTRO_ACADEMY_STUB") or shutil.which("distro-academy") or "/usr/bin/distro-academy"
        os.makedirs(SYSTEMD_USER, exist_ok=True)
        with open(svc, "w", encoding="utf-8") as f:
            f.write("[Unit]\nDescription=Distro Academy daily streak reminder\n\n[Service]\nType=oneshot\nExecStart=%s --remind\n" % exe)
        with open(tmr, "w", encoding="utf-8") as f:
            f.write("[Unit]\nDescription=Distro Academy daily streak reminder\n\n[Timer]\nOnCalendar=*-*-* %02d:00:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n" % hour)
        run("daemon-reload")
        r = run("enable", "--now", TIMER_NAME + ".timer")
        if r.returncode:
            return {"ok": False, "message": (r.stderr or "systemctl failed").strip()[:200]}
        return {"ok": True, "message": "on"}
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {"ok": False, "message": str(e)[:200]}


def run_reminder():
    """Runs from the timer: if you haven't studied today, a nudge. Clicking it opens the app."""
    state = read_json(STATE_FILE, None) or {}
    if not (state.get("settings", {}).get("notify", {}) or {}).get("reminder"):
        return 0
    days = (state.get("prog") or {}).get("days") or {}
    day = lambda n: time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))
    if days.get(day(0)):
        return 0
    streak = 0
    while days.get(day(streak + 1)):
        streak += 1
    name = (state.get("profile") or {}).get("name") or "hey"
    if streak:
        title, body = "Your %d-day streak ends at midnight \U0001f525" % streak, "%s, one quick lesson or a review keeps it alive. Larry is watching." % name
    else:
        title, body = "Five minutes of Linux? \U0001f427", "%s, a quick review or one lesson today starts a new streak." % name
    nid = notify(title, body, [("default", "Open Distro Academy")])
    if not nid:
        return 1
    loop = GLib.MainLoop()

    def clicked(i, _key):
        if i == nid:
            subprocess.Popen([os.environ.get("DISTRO_ACADEMY_STUB") or "distro-academy"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            loop.quit()
    on_notification_click(clicked)
    GLib.timeout_add_seconds(600, loop.quit)  # wait up to 10 minutes for a click, then exit
    loop.run()
    return 0


def _running_core_sha():
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, NameError):
        return ""


RUNNING_CORE_SHA = _running_core_sha()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Distro Academy")
        self.set_icon_name("distro-academy")
        geo = read_json(WINDOW_FILE, {})
        self.set_default_size(geo.get("w", 1200), geo.get("h", 780))
        if geo.get("max"):
            self.maximize()
        self.set_size_request(760, 520)
        self.connect("delete-event", self.on_close)

        manager = WebKit2.WebsiteDataManager(
            base_data_directory=os.path.join(DATA_DIR, "web"),
            base_cache_directory=os.path.join(GLib.get_user_cache_dir(), "distro-academy"),
        )
        context = WebKit2.WebContext.new_with_website_data_manager(manager)

        ucm = WebKit2.UserContentManager()
        self.ucm = ucm
        self.update_state = {"state": "idle"}
        self.notif_routes = {}
        on_notification_click(self.on_notification_click)
        ucm.connect("script-message-received::da", self.on_message)
        ucm.register_script_message_handler("da")

        self.view = WebKit2.WebView(web_context=context, user_content_manager=ucm)
        settings = self.view.get_settings()
        settings.set_enable_developer_extras(DEBUG)
        settings.set_allow_file_access_from_file_urls(True)
        settings.set_enable_smooth_scrolling(True)
        self.view.connect("context-menu", lambda *a: not DEBUG)
        self.view.connect("decide-policy", self.on_policy)
        self.add(self.view)

        self.load_app(os.environ.pop("DISTRO_ACADEMY_ROUTE", ""))
        if self.feed() and self.auto_updates():
            GLib.timeout_add_seconds(8, self.auto_check)
            GLib.timeout_add_seconds(CHECK_EVERY, self.auto_check)

    # ---------- which app to run ----------
    def load_app(self, route=""):
        """(Re)load the page with a fresh copy of the saved state, using the newest verified app."""
        upd = downloaded_update()
        index = upd["path"] if upd else os.path.join(APP_DIR, "index.html")
        self.running = upd["version"] if upd else UPDATE_CONF.get("version", "")
        info = {"running": self.running, "bundled": UPDATE_CONF.get("version", ""), "fromUpdate": bool(upd), "feed": bool(self.feed()), "signed": bool(UPDATE_CONF.get("pubkey"))}
        boot = "window.__DA_NATIVE__=true;window.__DA_STATE__=%s;window.__DA_UPDATE__=%s;window.__DA_GFX__=%s;" % (json.dumps(read_json(STATE_FILE, None)), json.dumps(info), json.dumps("safe" if os.environ.get("WEBKIT_DISABLE_DMABUF_RENDERER") == "1" else "gpu"))
        self.ucm.remove_all_scripts()
        self.ucm.add_script(WebKit2.UserScript.new(boot, WebKit2.UserContentInjectedFrames.TOP_FRAME, WebKit2.UserScriptInjectionTime.START, None, None))
        if DEBUG:
            print("distro-academy: loading", index, "version", self.running, file=sys.stderr)
        uri = GLib.filename_to_uri(index, None)
        if route and re.fullmatch(r"#/[\w./%-]*", route):
            uri += route  # reopen on the page the user was on
        self.view.load_uri(uri)

    # ---------- updates ----------
    def settings(self):
        return (read_json(STATE_FILE, {}) or {}).get("settings", {}).get("updates", {}) or {}

    def feed(self):
        url = self.settings().get("feed") or UPDATE_CONF.get("feed", "")
        return url if url and feed_allowed(url) else ""

    def auto_updates(self):
        return self.settings().get("auto", True) is not False

    def auto_check(self):
        last = read_json(CHECK_FILE, {}).get("at", 0)
        if self.auto_updates() and time.time() - last > CHECK_EVERY - 60 and self.update_state.get("state") != "checking":
            threading.Thread(target=self.check_updates, args=(None,), daemon=True).start()
        return True

    def check_updates(self, rid):
        feed, pub = self.feed(), UPDATE_CONF.get("pubkey", "")
        if not feed or not pub:
            return self.post_update({"state": "unconfigured"}, rid)
        self.update_state = {"state": "checking"}
        GLib.idle_add(self.notify_page, {"state": "checking"})
        base = feed if feed.endswith("/") else feed + "/"
        try:
            import ed25519
            raw = fetch(base + "latest.json", MAX_MANIFEST)
            sig = bytes.fromhex(fetch(base + "latest.json.sig", 1024).decode().strip())
            if not ed25519.verify(bytes.fromhex(pub), raw, sig):
                raise ValueError("the update's signature doesn't match, so it was ignored")
            man = json.loads(raw)
            ver, name = str(man.get("version", "")), str(man.get("file", ""))
            if man.get("app") != "distro-academy" or not re.fullmatch(r"DistroAcademy-[\d.]+\.html", name):
                raise ValueError("the update feed looks wrong")
            write_json(CHECK_FILE, {"at": time.time()})
            have = downloaded_update()
            best = max(vtuple(self.running), vtuple(have["version"]) if have else (0,))
            if vtuple(ver) <= best:
                ready = have and vtuple(have["version"]) > vtuple(self.running)
                return self.post_update({"state": "ready", "version": have["version"], "notes": have["notes"]} if ready else {"state": "latest", "version": self.running}, rid)
            data = fetch(base + name, MAX_APP)
            if len(data) != man.get("size") or hashlib.sha256(data).hexdigest() != man.get("sha256"):
                raise ValueError("the download was damaged, it will be retried later")
            # Newer releases also ship this runtime (core.py), so launcher fixes arrive without reinstalling.
            core = None
            if man.get("core"):
                cname = str(man["core"])
                if not re.fullmatch(r"core-[\d.]+\.py", cname):
                    raise ValueError("the update feed looks wrong")
                core = fetch(base + cname, MAX_MANIFEST * 8)
                if len(core) != man.get("core_size") or hashlib.sha256(core).hexdigest() != man.get("core_sha256"):
                    raise ValueError("the download was damaged, it will be retried later")
            folder = os.path.join(UPDATE_DIR, ver)
            os.makedirs(folder, exist_ok=True)
            for fname, blob in (("index.html", data), ("core.py", core)):
                if blob is None:
                    continue
                tmp = os.path.join(folder, fname + ".tmp")
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.replace(tmp, os.path.join(folder, fname))
            write_json(CURRENT_FILE, {"version": ver, "sha256": man["sha256"], "core_sha256": man.get("core_sha256") if core else None, "notes": man.get("notes", []), "date": man.get("date", "")})
            for old in os.listdir(UPDATE_DIR):
                if old != ver and os.path.isdir(os.path.join(UPDATE_DIR, old)):
                    shutil.rmtree(os.path.join(UPDATE_DIR, old), ignore_errors=True)
            self.post_update({"state": "ready", "version": ver, "notes": man.get("notes", [])}, rid)
        except Exception as e:  # network down, bad signature, disk full…: never crash the app over updates
            self.post_update({"state": "error", "message": str(getattr(e, "reason", e))[:200]}, rid)

    def post_update(self, payload, rid):
        self.update_state = payload
        GLib.idle_add(self.notify_page, payload, rid)

    def notify_page(self, payload, rid=None):
        if rid is not None:
            self.reply("DA._nativeReply", rid, payload)
        js = "window.DA&&DA.onUpdate&&DA.onUpdate(%s)" % json.dumps(payload)
        self.view.evaluate_javascript(js, -1, None, None, None, None, None)
        return False

    def restart_process(self, route):
        w, h = self.get_size()
        write_json(WINDOW_FILE, {"w": w, "h": h, "max": self.is_maximized()})
        env = dict(os.environ, DISTRO_ACADEMY_ROUTE=str(route or ""))
        # Go back through the launcher stub, which picks the new runtime on the way up.
        stub = os.environ.get("DISTRO_ACADEMY_STUB") or "/usr/bin/distro-academy"
        os.execve(sys.executable, [sys.executable, stub] + sys.argv[1:], env)

    def on_update_message(self, msg):
        action, rid = msg.get("action"), msg.get("id")
        if action == "check":
            if self.update_state.get("state") == "checking":
                return self.reply("DA._nativeReply", rid, {"state": "checking"})
            threading.Thread(target=self.check_updates, args=(rid,), daemon=True).start()
        elif action == "status":
            have = downloaded_update()
            self.reply("DA._nativeReply", rid, {**self.update_state, "running": self.running, "bundled": UPDATE_CONF.get("version", ""), "downloaded": have["version"] if have else "", "feed": self.feed(), "signed": bool(UPDATE_CONF.get("pubkey")), "lastCheck": read_json(CHECK_FILE, {}).get("at", 0)})
        elif action == "apply":
            have = downloaded_update()
            if have and have.get("core_changed"):
                # A new runtime only takes effect in a fresh process: restart ourselves in place.
                GLib.timeout_add(150, self.restart_process, msg.get("route", ""))
            else:
                GLib.timeout_add(150, lambda: (self.load_app(str(msg.get("route", ""))), False)[1])
        elif action == "revert":
            shutil.rmtree(UPDATE_DIR, ignore_errors=True)
            self.update_state = {"state": "idle"}
            GLib.timeout_add(150, lambda: (self.load_app(), False)[1])

    def on_message(self, _ucm, result):
        try:
            msg = json.loads(result.get_js_value().to_string())
        except (ValueError, AttributeError):
            return
        if msg.get("type") == "save":
            write_json(STATE_FILE, msg.get("state"))
        elif msg.get("type") == "open":
            self.open_external(msg.get("url", ""))
        elif msg.get("type") == "export":
            self.save_file({"name": msg.get("name"), "text": msg.get("data", "")})
        elif msg.get("type") == "saveFile":
            path = self.save_file(msg)
            self.reply("DA._nativeReply", msg.get("id"), {"path": path})
        elif msg.get("type") == "update":
            self.on_update_message(msg)
        elif msg.get("type") == "http":
            threading.Thread(target=self.http, args=(msg,), daemon=True).start()
        elif msg.get("type") == "notify":
            nid = notify(msg.get("title", ""), msg.get("body", ""), [("default", "Open")])
            route = str(msg.get("route", ""))
            if nid and re.fullmatch(r"#/[\w./%-]*", route):
                self.notif_routes[nid] = route
            self.reply("DA._nativeReply", msg.get("id"), {"ok": bool(nid)})
        elif msg.get("type") == "reminder":
            self.reply("DA._nativeReply", msg.get("id"), set_reminder(bool(msg.get("on")), msg.get("hour", 19)))

    def on_notification_click(self, nid, _key):
        if nid not in self.notif_routes:
            return
        route = self.notif_routes.pop(nid)
        self.present()
        self.view.evaluate_javascript("DA.ui && DA.ui.go(%s)" % json.dumps(route), -1, None, None, None, None, None)

    def save_file(self, msg):
        name = os.path.basename(msg.get("name") or "distro-academy-file.txt")
        kind = SAVE_DIRS.get(msg.get("dir", ""))
        folder = (GLib.get_user_special_dir(kind) if kind is not None else None) or os.path.expanduser("~")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        if "b64" in msg:
            with open(path, "wb") as f:
                f.write(base64.b64decode(msg["b64"]))
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(msg.get("text", ""))
        return path.replace(os.path.expanduser("~"), "~", 1)

    def http(self, msg):
        url = msg.get("url", "")
        if urllib.parse.urlparse(url).hostname not in HTTP_HOSTS:
            self.reply("DA._nativeReply", msg.get("id"), {"status": 0, "text": "blocked host"})
            return
        req = urllib.request.Request(url, data=(msg.get("body") or "").encode(), method="POST",
                                     headers=msg.get("headers") or {})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                out = {"status": r.status, "text": r.read().decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            out = {"status": e.code, "text": e.read().decode("utf-8", "replace")}
        except (urllib.error.URLError, OSError) as e:
            out = {"status": 0, "text": str(getattr(e, "reason", e))}
        GLib.idle_add(self.reply, "DA._nativeReply", msg.get("id"), out)

    def reply(self, fn, rid, data):
        js = "%s(%s,%s)" % (fn, json.dumps(rid), json.dumps(data))
        self.view.evaluate_javascript(js, -1, None, None, None, None, None)
        return False

    def open_external(self, uri):
        if uri.startswith(("http://", "https://")):
            Gio.AppInfo.launch_default_for_uri(uri, None)

    def on_policy(self, _view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri.startswith(("http://", "https://")):
                self.open_external(uri)
                decision.ignore()
                return True
        return False

    def on_close(self, *_):
        w, h = self.get_size()
        write_json(WINDOW_FILE, {"w": w, "h": h, "max": self.is_maximized()})
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.show_all()
        win.present()


if __name__ == "__main__":
    sys.path.insert(0, APP_DIR)  # ed25519.py ships next to the app
    UPDATE_CONF.update(read_json(os.path.join(APP_DIR, "update.json"), {}))
    GLib.set_prgname("distro-academy")
    GLib.set_application_name("Distro Academy")
    if "--remind" in sys.argv:
        sys.exit(run_reminder())
    sys.exit(App().run(sys.argv))
