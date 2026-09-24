#!/usr/bin/env python3
"""
Monitoring Controller — Windows дээр ажиллах веб dashboard.

- Сүлжээг scan хийж agent-уудыг олно (IPv4)
- Бүх компьютерийн дэлгэцийг grid-ээр харуулна
- Сонгосон буюу бүх машиныг зэрэг унтраах / reboot / түгжих / мессеж илгээх

Ажиллуулах:
    pip install -r requirements.txt
    python controller.py
Дараа нь browser дээр:  http://127.0.0.1:5000
"""

import concurrent.futures
import ipaddress
import json
import os
import socket
import threading
import time

import requests
from flask import Flask, Response, jsonify, render_template, request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CONTROLLER_CONFIG", os.path.join(BASE, "config.json"))

DEFAULTS = {
    "agent_port": 8765,
    "token": "CHANGE_ME",
    "subnets": ["192.168.1.0/24"],   # scan хийх сүлжээ(нүүд)
    "scan_timeout": 0.4,              # секунд, порт нээлттэй эсэхийг шалгах
    "request_timeout": 6,            # секунд, agent руу хийх хүсэлт
    "listen_host": "127.0.0.1",
    "listen_port": 5000,
    "shutdown_delay": 3,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        print(f"[warn] {CONFIG_PATH} олдсонгүй — config.example.json-оос хуулна уу")
    return cfg


CONFIG = load_config()
app = Flask(__name__)

# Илэрсэн хостуудын кэш:  ip -> {status..., last_seen}
HOSTS = {}
HOSTS_LOCK = threading.Lock()


def _headers():
    return {"X-Auth-Token": CONFIG["token"]}


def _port_open(ip, port, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((str(ip), port)) == 0
    finally:
        s.close()


def _probe(ip):
    """Порт нээлттэй бол /status татаж, agent мөн эсэхийг баталгаажуулна."""
    port = CONFIG["agent_port"]
    if not _port_open(ip, port, CONFIG["scan_timeout"]):
        return None
    try:
        r = requests.get(f"http://{ip}:{port}/status",
                         headers=_headers(),
                         timeout=CONFIG["request_timeout"])
        if r.status_code == 200:
            data = r.json()
            data["ip"] = str(ip)
            data["online"] = True
            data["last_seen"] = time.time()
            return data
        if r.status_code == 401:
            return {"ip": str(ip), "online": True, "error": "token таарахгүй",
                    "last_seen": time.time()}
    except Exception:
        return {"ip": str(ip), "online": True, "error": "agent биш/хариу алга",
                "last_seen": time.time()}
    return None


def scan_network():
    targets = []
    for net in CONFIG["subnets"]:
        try:
            targets.extend(ipaddress.ip_network(net, strict=False).hosts())
        except ValueError as e:
            print(f"[warn] буруу subnet {net}: {e}")
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        for res in ex.map(_probe, targets):
            if res:
                found.append(res)
    with HOSTS_LOCK:
        for h in found:
            HOSTS[h["ip"]] = h
    return found


def refresh_status(ip):
    res = _probe(ipaddress.ip_address(ip))
    if res:
        with HOSTS_LOCK:
            HOSTS[ip] = res
    return res


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("dashboard.html", config={
        "subnets": CONFIG["subnets"],
        "agent_port": CONFIG["agent_port"],
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    found = scan_network()
    return jsonify({"count": len(found), "hosts": sorted(found, key=lambda h: h["ip"])})


@app.route("/api/hosts")
def api_hosts():
    with HOSTS_LOCK:
        hosts = list(HOSTS.values())
    return jsonify({"hosts": sorted(hosts, key=lambda h: h["ip"])})


@app.route("/api/status/<ip>")
def api_status(ip):
    res = refresh_status(ip)
    return jsonify(res or {"ip": ip, "online": False})


@app.route("/api/screenshot/<ip>")
def api_screenshot(ip):
    port = CONFIG["agent_port"]
    try:
        r = requests.get(f"http://{ip}:{port}/screenshot",
                         headers=_headers(),
                         timeout=CONFIG["request_timeout"] + 10)
        if r.status_code == 200:
            return Response(r.content,
                            content_type=r.headers.get("Content-Type", "image/jpeg"))
        return Response(f"agent алдаа {r.status_code}", status=502)
    except Exception as e:
        return Response(f"холбогдож чадсангүй: {e}", status=502)


@app.route("/api/command", methods=["POST"])
def api_command():
    """
    Body: {"action": "shutdown|reboot|lock|message", "targets": ["ip", ...],
           "text": "...", "title": "..."}
    targets хоосон бол илэрсэн БҮХ хост руу илгээнэ.
    """
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action")
    if action not in ("shutdown", "reboot", "lock", "message"):
        return jsonify({"error": "буруу action"}), 400

    targets = body.get("targets") or []
    if not targets:
        with HOSTS_LOCK:
            targets = [h["ip"] for h in HOSTS.values() if h.get("online") and not h.get("error")]

    port = CONFIG["agent_port"]
    payload = {}
    if action in ("shutdown", "reboot"):
        payload["delay"] = CONFIG["shutdown_delay"]
    if action == "message":
        payload["text"] = str(body.get("text", ""))[:500]
        payload["title"] = str(body.get("title", "Багшийн мэдэгдэл"))[:120]

    def send(ip):
        try:
            r = requests.post(f"http://{ip}:{port}/{action}",
                              headers=_headers(), json=payload,
                              timeout=CONFIG["request_timeout"])
            return ip, r.status_code == 200, r.status_code
        except Exception as e:
            return ip, False, str(e)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for ip, ok, detail in ex.map(send, targets):
            results[ip] = {"ok": ok, "detail": detail}
    return jsonify({"action": action, "results": results,
                    "sent": len(targets),
                    "success": sum(1 for v in results.values() if v["ok"])})


@app.route("/api/exec", methods=["POST"])
def api_exec():
    """
    Бүх/сонгосон машин дээр terminal команд ажиллуулж, гаралтыг цуглуулна.
    Body: {"command": "...", "targets": ["ip"...], "as_user": bool, "timeout": int}
    """
    body = request.get_json(force=True, silent=True) or {}
    command = str(body.get("command", "")).strip()
    if not command:
        return jsonify({"error": "command хоосон"}), 400
    as_user = bool(body.get("as_user", False))
    timeout = max(1, min(int(body.get("timeout", 30) or 30), 300))

    targets = body.get("targets") or []
    if not targets:
        with HOSTS_LOCK:
            targets = [h["ip"] for h in HOSTS.values() if h.get("online") and not h.get("error")]

    port = CONFIG["agent_port"]

    def run(ip):
        try:
            r = requests.post(f"http://{ip}:{port}/exec",
                              headers=_headers(),
                              json={"command": command, "as_user": as_user, "timeout": timeout},
                              timeout=timeout + 10)
            if r.status_code == 200:
                return ip, r.json()
            return ip, {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return ip, {"error": str(e)}

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for ip, res in ex.map(run, targets):
            results[ip] = res
    ok = sum(1 for v in results.values() if v.get("returncode") == 0)
    return jsonify({"results": results, "sent": len(targets), "success": ok})


@app.route("/api/term/read")
def api_term_read():
    """Terminal гаралтыг татах (GET, polling)."""
    ip = request.args.get("ip", "")
    sid = request.args.get("sid", "")
    since = request.args.get("since", "0")
    port = CONFIG["agent_port"]
    try:
        r = requests.get(f"http://{ip}:{port}/term/read",
                         headers=_headers(), params={"sid": sid, "since": since},
                         timeout=CONFIG["request_timeout"])
        return Response(r.content, status=r.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e), "closed": True}), 502


@app.route("/api/term/<action>", methods=["POST"])
def api_term_action(action):
    """open / input / resize / close командыг agent руу дамжуулна."""
    if action not in ("open", "input", "resize", "close"):
        return jsonify({"error": "буруу action"}), 400
    body = request.get_json(force=True, silent=True) or {}
    ip = body.pop("ip", "")
    port = CONFIG["agent_port"]
    try:
        r = requests.post(f"http://{ip}:{port}/term/{action}",
                          headers=_headers(), json=body,
                          timeout=CONFIG["request_timeout"])
        return Response(r.content, status=r.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e), "closed": True}), 502


if __name__ == "__main__":
    if CONFIG["token"] == "CHANGE_ME":
        print("[warn] config.json дотор token-оо солино уу (agent-тэй ижил байх ёстой)!")
    print(f"Dashboard:  http://{CONFIG['listen_host']}:{CONFIG['listen_port']}")
    app.run(host=CONFIG["listen_host"], port=CONFIG["listen_port"], threaded=True)
