#!/usr/bin/env python3
"""
Monitoring Agent — Ubuntu 24.04 талд ажиллах жижиг HTTP сервер.

Windows дээрх controller-оос дараах командуудыг токеноор хамгаалан хүлээж авна:
    GET  /status      -> хост, хэрэглэгч, uptime, CPU/RAM
    GET  /screenshot  -> идэвхтэй дэлгэцийн зураг (JPEG/PNG)
    POST /exec        -> terminal команд ажиллуулж гаралтыг буцаах
    POST /term/open|input|resize|close, GET /term/read -> интерактив PTY terminal
    POST /message     -> хэрэглэгчид мэдэгдэл харуулах
    POST /lock        -> дэлгэц түгжих
    POST /reboot      -> дахин ачаалах
    POST /shutdown    -> унтраах

Зөвхөн Python 3 stdlib ашигладаг тул Ubuntu талд pip шаардлагагүй.
Дэлгэцийн зургийг жижигрүүлэхэд Pillow байвал ашиглана (заавал биш).
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# PTY-д хэрэгтэй Linux-only модулиуд
try:
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios
    import uuid
    _PTY_OK = True
except ImportError:
    _PTY_OK = False

# ---------------------------------------------------------------------------
# Тохиргоо
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("AGENT_CONFIG", "/etc/monitoring-agent/config.json")
DEFAULTS = {
    "port": 8765,
    "token": "CHANGE_ME",          # controller-ийн токентой ЯГ ижил байх ёстой
    "screenshot_max_width": 1280,   # 0 бол жижигрүүлэхгүй
    "screenshot_quality": 55,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        print(f"[warn] {CONFIG_PATH} олдсонгүй, default тохиргоо ашиглаж байна", file=sys.stderr)
    except Exception as e:
        print(f"[warn] тохиргоо уншихад алдаа: {e}", file=sys.stderr)
    return cfg


CONFIG = load_config()
START_TIME = time.time()


# ---------------------------------------------------------------------------
# Идэвхтэй график session-ыг илрүүлэх (root service-ээс хэрэглэгчийн дэлгэц рүү хандах)
# ---------------------------------------------------------------------------
def active_graphical_session():
    """loginctl-оор seat0 дээрх идэвхтэй хэрэглэгч болон орчныг олж авна."""
    info = {"user": None, "uid": None, "display": ":0", "wayland": None, "type": None}
    try:
        out = subprocess.check_output(
            ["loginctl", "list-sessions", "--no-legend"], text=True, timeout=5
        )
    except Exception:
        return info

    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        sid = parts[0]
        try:
            props = subprocess.check_output(
                ["loginctl", "show-session", sid,
                 "-p", "Active", "-p", "Name", "-p", "User",
                 "-p", "Type", "-p", "Display", "-p", "State"],
                text=True, timeout=5,
            )
        except Exception:
            continue
        d = {}
        for p in props.splitlines():
            if "=" in p:
                k, v = p.split("=", 1)
                d[k] = v
        if d.get("Active") == "yes" and d.get("Type") in ("x11", "wayland"):
            info["user"] = d.get("Name")
            info["uid"] = d.get("User")
            info["type"] = d.get("Type")
            if d.get("Display"):
                info["display"] = d.get("Display")
            break
    return info


def user_env(sess):
    """Тухайн хэрэглэгчийн нэрээр screenshot/notify tool ажиллуулах орчин бэлдэнэ."""
    env = dict(os.environ)
    uid = sess.get("uid")
    if uid:
        runtime = f"/run/user/{uid}"
        env["XDG_RUNTIME_DIR"] = runtime
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    if sess.get("type") == "wayland":
        env["WAYLAND_DISPLAY"] = "wayland-0"
        env["XDG_SESSION_TYPE"] = "wayland"
    env["DISPLAY"] = sess.get("display") or ":0"
    return env


def run_as_user(sess, argv, **kwargs):
    """Командыг идэвхтэй хэрэглэгчийн нэрээр ажиллуулна (root-оос)."""
    env = user_env(sess)
    user = sess.get("user")
    if user and os.geteuid() == 0:
        argv = ["sudo", "-u", user, "--preserve-env=DISPLAY,WAYLAND_DISPLAY,"
                "XDG_RUNTIME_DIR,DBUS_SESSION_BUS_ADDRESS,XDG_SESSION_TYPE"] + argv
    return subprocess.run(argv, env=env, **kwargs)


# ---------------------------------------------------------------------------
# Дэлгэцийн зураг авах (олон backend-ийг дараалан оролдоно)
# ---------------------------------------------------------------------------
def capture_screen():
    sess = active_graphical_session()
    tmp = f"/tmp/mon-shot-{os.getpid()}.png"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass

    backends = []
    if shutil.which("gnome-screenshot"):
        backends.append(["gnome-screenshot", "-f", tmp])
    if sess.get("type") == "wayland" and shutil.which("grim"):
        backends.append(["grim", tmp])
    if shutil.which("scrot"):
        backends.append(["scrot", "-o", tmp])
    if shutil.which("import"):  # ImageMagick (X11)
        backends.append(["import", "-window", "root", tmp])
    if shutil.which("spectacle"):  # KDE
        backends.append(["spectacle", "-b", "-n", "-o", tmp])

    last_err = "screenshot tool олдсонгүй"
    for argv in backends:
        try:
            r = run_as_user(sess, argv, capture_output=True, timeout=20)
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                data = open(tmp, "rb").read()
                os.remove(tmp)
                return _maybe_shrink(data), sess
            last_err = (r.stderr or b"").decode(errors="ignore")[:300] or f"{argv[0]} хоосон зураг"
        except Exception as e:
            last_err = f"{argv[0]}: {e}"
    raise RuntimeError(last_err)


def _maybe_shrink(png_bytes):
    """Pillow байвал JPEG болгож жижигрүүлнэ, эс бол PNG-ээр буцаана."""
    max_w = CONFIG.get("screenshot_max_width", 0)
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        if max_w and img.width > max_w:
            h = int(img.height * max_w / img.width)
            img = img.resize((max_w, h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=CONFIG.get("screenshot_quality", 55))
        return {"data": buf.getvalue(), "mime": "image/jpeg"}
    except Exception:
        return {"data": png_bytes, "mime": "image/png"}


# ---------------------------------------------------------------------------
# Статус
# ---------------------------------------------------------------------------
def read_status():
    sess = active_graphical_session()
    status = {
        "hostname": socket.gethostname(),
        "active_user": sess.get("user"),
        "uptime_seconds": int(time.time() - START_TIME),
        "os": "unknown",
        "cpu_percent": None,
        "mem_percent": None,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    status["os"] = line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    # Санах ой
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                meminfo[k] = int(v.strip().split()[0])
        total = meminfo.get("MemTotal", 0)
        avail = meminfo.get("MemAvailable", 0)
        if total:
            status["mem_percent"] = round((total - avail) * 100 / total, 1)
    except Exception:
        pass
    # CPU (богино хугацааны дундаж)
    try:
        load1 = os.getloadavg()[0]
        ncpu = os.cpu_count() or 1
        status["cpu_percent"] = round(min(load1 / ncpu * 100, 100), 1)
    except Exception:
        pass
    return status


# ---------------------------------------------------------------------------
# Интерактив terminal (PTY) session-ууд
# ---------------------------------------------------------------------------
TERM_SESSIONS = {}
TERM_LOCK = threading.Lock()
TERM_MAX = 20
TERM_IDLE = 1800  # секунд, идэвхгүй session-ыг хаана


class TermSession:
    CAP = 256 * 1024  # сүүлийн 256KB гаралтыг хадгална

    def __init__(self, as_user=False):
        self.sid = uuid.uuid4().hex
        self.buf = bytearray()
        self.produced = 0
        self.lock = threading.Lock()
        self.alive = True
        self.last = time.time()

        sess = active_graphical_session()
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        if as_user and sess.get("user"):
            env = user_env(sess)
            env["TERM"] = "xterm-256color"
            argv = ["su", "-", sess["user"]] if os.geteuid() == 0 else ["bash", "-i"]
        else:
            argv = ["bash", "-i"]

        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvpe(argv[0], argv, env)
            except Exception:
                os._exit(1)
        self.pid = pid
        self.fd = fd
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while self.alive:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
                if self.fd in r:
                    chunk = os.read(self.fd, 65536)
                    if not chunk:
                        break
                    with self.lock:
                        self.buf.extend(chunk)
                        self.produced += len(chunk)
                        if len(self.buf) > self.CAP:
                            del self.buf[:len(self.buf) - self.CAP]
                    self.last = time.time()
            except OSError:
                break
        self.alive = False

    def read(self, since):
        with self.lock:
            base = self.produced - len(self.buf)
            start = max(0, since - base)
            return bytes(self.buf[start:]), self.produced

    def write(self, data):
        self.last = time.time()
        try:
            os.write(self.fd, data)
        except OSError:
            self.alive = False

    def resize(self, rows, cols):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def close(self):
        self.alive = False
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def term_reap():
    """Идэвхгүй болон үхсэн session-уудыг цэвэрлэнэ."""
    now = time.time()
    with TERM_LOCK:
        for sid in list(TERM_SESSIONS):
            s = TERM_SESSIONS[sid]
            if not s.alive or now - s.last > TERM_IDLE:
                s.close()
                del TERM_SESSIONS[sid]


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "MonAgent/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[agent] %s - %s\n" % (self.address_string(), fmt % args))

    def _authorized(self):
        token = self.headers.get("X-Auth-Token", "")
        expected = CONFIG.get("token", "")
        # тогтмол хугацааны харьцуулалт
        if len(token) != len(expected):
            return False
        ok = 0
        for a, b in zip(token, expected):
            ok |= ord(a) ^ ord(b)
        return ok == 0

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self):
        self._json(401, {"error": "unauthorized"})

    def do_GET(self):
        if not self._authorized():
            return self._deny()
        if self.path == "/status":
            return self._json(200, read_status())
        if self.path == "/screenshot":
            try:
                shot, _ = capture_screen()
                self.send_response(200)
                self.send_header("Content-Type", shot["mime"])
                self.send_header("Content-Length", str(len(shot["data"])))
                self.end_headers()
                self.wfile.write(shot["data"])
            except Exception as e:
                self._json(500, {"error": str(e)})
            return
        if self.path == "/ping":
            return self._json(200, {"agent": True, "hostname": socket.gethostname()})
        if self.path.startswith("/term/read"):
            q = parse_qs(urlparse(self.path).query)
            sid = (q.get("sid") or [""])[0]
            since = int((q.get("since") or ["0"])[0])
            s = TERM_SESSIONS.get(sid)
            if not s:
                return self._json(404, {"error": "session олдсонгүй", "closed": True})
            data, nxt = s.read(since)
            return self._json(200, {
                "data": base64.b64encode(data).decode(),
                "next": nxt, "alive": s.alive,
            })
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        sess = active_graphical_session()

        if self.path == "/message":
            text = str(payload.get("text", ""))[:500]
            title = str(payload.get("title", "Багшийн мэдэгдэл"))[:120]
            try:
                if shutil.which("zenity"):
                    run_as_user(sess, ["zenity", "--info", "--title", title,
                                       "--text", text, "--width", "400"],
                                capture_output=True, timeout=3)
                elif shutil.which("notify-send"):
                    run_as_user(sess, ["notify-send", title, text],
                                capture_output=True, timeout=5)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"error": str(e)})

        if self.path == "/term/open":
            if not _PTY_OK:
                return self._json(500, {"error": "PTY дэмжигдэхгүй"})
            term_reap()
            with TERM_LOCK:
                if len(TERM_SESSIONS) >= TERM_MAX:
                    return self._json(429, {"error": "хэт олон session нээгдсэн"})
                s = TermSession(as_user=bool(payload.get("as_user", False)))
                TERM_SESSIONS[s.sid] = s
            return self._json(200, {"sid": s.sid})

        if self.path == "/term/input":
            s = TERM_SESSIONS.get(payload.get("sid"))
            if not s or not s.alive:
                return self._json(404, {"error": "session олдсонгүй", "closed": True})
            s.write(base64.b64decode(payload.get("data", "")))
            return self._json(200, {"ok": True})

        if self.path == "/term/resize":
            s = TERM_SESSIONS.get(payload.get("sid"))
            if s:
                s.resize(int(payload.get("rows", 24)), int(payload.get("cols", 80)))
            return self._json(200, {"ok": True})

        if self.path == "/term/close":
            with TERM_LOCK:
                s = TERM_SESSIONS.pop(payload.get("sid"), None)
            if s:
                s.close()
            return self._json(200, {"ok": True})

        if self.path == "/exec":
            command = str(payload.get("command", ""))
            if not command:
                return self._json(400, {"error": "command хоосон"})
            as_user = bool(payload.get("as_user", False))
            timeout = min(int(payload.get("timeout", 30) or 30), 300)
            # y/N зэрэг асуултад урьдчилан хариулах stdin
            stdin_text = payload.get("stdin")
            kwargs = {"capture_output": True, "timeout": timeout}
            if stdin_text:
                if not str(stdin_text).endswith("\n"):
                    stdin_text = str(stdin_text) + "\n"
                kwargs["input"] = stdin_text.encode()
            try:
                if as_user and sess.get("user"):
                    r = run_as_user(sess, ["bash", "-lc", command], **kwargs)
                else:
                    r = subprocess.run(["bash", "-lc", command], **kwargs)
                return self._json(200, {
                    "returncode": r.returncode,
                    "stdout": (r.stdout or b"").decode(errors="ignore")[-8000:],
                    "stderr": (r.stderr or b"").decode(errors="ignore")[-4000:],
                })
            except subprocess.TimeoutExpired:
                return self._json(200, {"returncode": -1, "stdout": "",
                                        "stderr": f"timeout ({timeout}s хэтэрлээ)"})
            except Exception as e:
                return self._json(500, {"error": str(e)})

        if self.path == "/lock":
            try:
                run_as_user(sess, ["loginctl", "lock-sessions"],
                            capture_output=True, timeout=5)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"error": str(e)})

        if self.path == "/reboot":
            self._json(200, {"ok": True, "action": "reboot"})
            _schedule_power("reboot", payload.get("delay", 3))
            return

        if self.path == "/shutdown":
            self._json(200, {"ok": True, "action": "shutdown"})
            _schedule_power("poweroff", payload.get("delay", 3))
            return

        self._json(404, {"error": "not found"})


def _schedule_power(action, delay):
    """Хариу буцаасны дараа delay секундын дотор унтраах/reboot хийнэ."""
    import threading

    def worker():
        time.sleep(max(0, min(int(delay or 0), 120)))
        subprocess.run(["systemctl", action], capture_output=True)

    threading.Thread(target=worker, daemon=True).start()


def main():
    port = int(CONFIG.get("port", 8765))
    if CONFIG.get("token") == "CHANGE_ME":
        print("[warn] TOKEN default хэвээр байна! config.json дотор солино уу.", file=sys.stderr)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[agent] {socket.gethostname()} дээр 0.0.0.0:{port} порт дээр сонсож байна")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
