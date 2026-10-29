"""Small isolated Meta webhook/queue adapter, driven by the n8n runner workflow.

Only campaign settings are shared with the Telegram admin. This process has no
access to the Telegram token, customer/payment database or n8n database.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

import instagram_automation as campaign


class GraphError(Exception):
    def __init__(self, uncertain=False, code=0):
        self.uncertain, self.code = uncertain, code
        super().__init__("Meta request did not confirm delivery")


class Gateway:
    def __init__(self, root: Path, config_path: Path):
        self.root, self.config_path = root, config_path
        self.credentials_path = root / "secrets" / "credentials.json"
        self.state_dir = root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.state_dir / "events.sqlite3"
        self.lock = threading.Lock()
        self.connected = False
        self.checked_at = 0
        self.credentials_stamp = None
        self.last_ingress = 0
        with closing(self.connect()) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, result TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    person TEXT PRIMARY KEY, opened_at REAL NOT NULL DEFAULT 0,
                    link_sent_at REAL NOT NULL DEFAULT 0, opted_out INTEGER NOT NULL DEFAULT 0,
                    human_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sent_messages (id TEXT PRIMARY KEY, created_at REAL NOT NULL);
                UPDATE events SET status='unknown',result='process_interrupted'
                    WHERE status='sending';
            """)
            con.commit()

    def connect(self):
        con = sqlite3.connect(self.database, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def credentials(self):
        try:
            return json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def graph(self, method, endpoint, payload=None):
        credentials = self.credentials()
        version = credentials.get("api_version", "")
        if not re.fullmatch(r"v\d+\.\d+", version):
            raise GraphError()
        mode = credentials.get("api_mode", "instagram_login")
        hosts = {"instagram_login": "graph.instagram.com", "unified_login": "graph.facebook.com"}
        if mode not in hosts:
            raise GraphError()
        url = "https://" + hosts[mode] + "/" + version + "/" + endpoint
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + credentials.get("access_token", ""),
            "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=8) as response:
                result = json.loads(response.read(65536))
            if not isinstance(result, dict) or "error" in result:
                raise GraphError(uncertain=method == "POST")
            return result
        except HTTPError as error:
            # A 5xx can follow a successful side effect. Never blindly retry it.
            raise GraphError(uncertain=method == "POST" and error.code >= 500, code=error.code) from None
        except (URLError, TimeoutError, socket.timeout, OSError, ValueError):
            raise GraphError(uncertain=method == "POST") from None

    def ready(self):
        credentials = self.credentials()
        stamp = hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).digest()
        if stamp == self.credentials_stamp and time.time() - self.checked_at < 600:
            return self.connected
        self.credentials_stamp, self.checked_at, self.connected = stamp, time.time(), False
        required = ("app_id", "app_secret", "access_token", "ig_user_id", "api_version")
        if any(not credentials.get(key) for key in required):
            return False
        if not str(credentials["ig_user_id"]).isdigit() or not str(credentials["app_id"]).isdigit():
            return False
        mode = credentials.get("api_mode", "instagram_login")
        if mode not in {"instagram_login", "unified_login"}:
            return False
        try:
            if mode == "unified_login":
                expected_business = str(credentials.get("client_business_id", ""))
                if not expected_business.isdigit():
                    return False
                # With a business integration token /me is the system user, not Instagram.
                business = self.graph("GET", "me?fields=client_business_id")
                if str(business.get("client_business_id", "")) != expected_business:
                    return False
                accounts = self.graph("GET", "me/assigned_instagram_accounts?fields=id,username&limit=100")
                account = next((item for item in accounts.get("data", [])
                    if str(item.get("id", "")) == str(credentials["ig_user_id"])), {})
            else:
                account = self.graph("GET", "me?fields=user_id,username")
            if (str(account.get("user_id", account.get("id", ""))) != str(credentials["ig_user_id"])
                    or account.get("username", "").casefold() != "biomozg"):
                return False
            subscribed = self.graph("GET", str(credentials["ig_user_id"]) + "/subscribed_apps")
            self.connected = any(str(app.get("id")) == str(credentials["app_id"])
                and {"comments", "messages", "messaging_postbacks"}.issubset(set(app.get("subscribed_fields", [])))
                for app in subscribed.get("data", []))
        except (GraphError, TypeError, AttributeError):
            self.connected = False
        return self.connected

    def verify_signature(self, body: bytes, signature: str) -> bool:
        secret = self.credentials().get("app_secret", "")
        if not secret or not re.fullmatch(r"sha256=[a-f0-9]{64}", signature):
            return False
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def enqueue(self, event_id, kind, payload):
        now = time.time()
        with closing(self.connect()) as con:
            con.execute("INSERT OR IGNORE INTO events(id,kind,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
                        (event_id, kind, json.dumps(payload, ensure_ascii=False), now, now))
            con.commit()

    def ingest(self, value):
        if value.get("object") != "instagram":
            return
        own_id = str(self.credentials().get("ig_user_id", ""))
        for entry in value.get("entry", []):
            if str(entry.get("id")) != own_id:
                continue
            for change in entry.get("changes", []):
                item = change.get("value", {})
                if change.get("field") == "comments" and re.fullmatch(r"\d+", str(item.get("id", ""))):
                    person = str((item.get("from") or {}).get("id", ""))
                    self.enqueue("comment:" + str(item["id"]), "comment", {
                        "comment": str(item["id"]), "person": person,
                        "text": str(item.get("text", ""))[:2200], "timestamp": self.timestamp(entry.get("time", 0)),
                    })
            for item in entry.get("messaging", []):
                message = item.get("message") or {}
                postback = item.get("postback") or {}
                person = str((item.get("sender") or {}).get("id", ""))
                echo = bool(message.get("is_echo")) or person == own_id
                if echo:
                    person = str((item.get("recipient") or {}).get("id", ""))
                mid = str(message.get("mid") or postback.get("mid") or "")
                if not mid:
                    continue
                self.enqueue("message:" + mid, "echo" if echo else "message", {
                    "person": person, "mid": mid, "text": str(message.get("text", ""))[:2200],
                    "payload": (message.get("quick_reply") or {}).get("payload", "") or postback.get("payload", ""),
                    "timestamp": self.timestamp(item.get("timestamp", 0)),
                })
        self.last_ingress = time.time()

    @staticmethod
    def timestamp(value):
        value = float(value)
        return value / 1000 if value > 100000000000 else value

    def finish(self, event_id, status, result=""):
        with closing(self.connect()) as con:
            con.execute("UPDATE events SET status=?,result=?,updated_at=? WHERE id=?", (status, result, time.time(), event_id))
            con.commit()

    def conversation(self, person):
        with closing(self.connect()) as con:
            row = con.execute("SELECT * FROM conversations WHERE person=?", (person,)).fetchone()
        return dict(row) if row else {"opened_at": 0, "link_sent_at": 0, "opted_out": 0, "human_until": 0}

    def update_person(self, person, **fields):
        allowed = {"opened_at", "link_sent_at", "opted_out", "human_until"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Invalid conversation field")
        with closing(self.connect()) as con:
            con.execute("INSERT OR IGNORE INTO conversations(person) VALUES(?)", (person,))
            con.execute("UPDATE conversations SET " + ",".join(key + "=?" for key in fields) + " WHERE person=?", (*fields.values(), person))
            con.commit()

    def remember_sent(self, response):
        mid = response.get("message_id")
        if not mid:
            raise GraphError(uncertain=True)
        with closing(self.connect()) as con:
            con.execute("INSERT OR IGNORE INTO sent_messages VALUES(?,?)", (str(mid), time.time()))
            con.commit()

    def process(self, event):
        payload, kind, event_id = json.loads(event["payload"]), event["kind"], event["id"]
        person = str(payload.get("person", ""))
        credentials = self.credentials()
        own_id = str(credentials.get("ig_user_id", ""))
        if not person.isdigit() or person == own_id:
            self.finish(event_id, "skipped", "invalid_person")
            return
        conversation = self.conversation(person)
        now = time.time()
        if kind == "echo":
            with closing(self.connect()) as con:
                known = con.execute("SELECT 1 FROM sent_messages WHERE id=?", (payload["mid"],)).fetchone()
            if not known:
                self.update_person(person, human_until=now + 86400)
            self.finish(event_id, "skipped", "echo")
            return
        text = payload.get("text", "")
        if kind == "message" and campaign.words(text) in {"стоп", "stop", "отписаться", "не пишите"}:
            self.update_person(person, opted_out=1)
            self.finish(event_id, "skipped", "opt_out")
            return
        settings = campaign.load_settings(self.config_path)
        if not settings["enabled"] or conversation["opted_out"] or conversation["human_until"] > now:
            self.finish(event_id, "skipped", "paused_or_human_or_opt_out")
            return
        if kind == "comment":
            age = now - payload["timestamp"]
            if not 0 <= age < 7 * 86400 or not campaign.matches(text, settings["keywords"]):
                self.finish(event_id, "skipped", "keyword_or_private_reply_window")
                return
            if conversation["opened_at"] > now - 86400:
                self.finish(event_id, "skipped", "cooldown")
                return
            # Re-read the actual comment: deleted/edited comments and old events cannot start a campaign.
            current = self.graph("GET", payload["comment"] + "?fields=id,text,timestamp")
            from datetime import datetime
            created = datetime.fromisoformat(current["timestamp"].replace("Z", "+00:00")).timestamp()
            if not 0 <= time.time() - created < 7 * 86400 or not campaign.matches(current.get("text", ""), settings["keywords"]):
                self.finish(event_id, "skipped", "comment_changed_or_old")
                return
            message = {"text": campaign.choose(settings["opening"], person), "quick_replies": [{
                "content_type": "text", "title": settings["opt_in_label"], "payload": campaign.OPT_IN}]}
            recipient = {"comment_id": payload["comment"]}
        elif kind == "message":
            if not 0 <= now - payload["timestamp"] < 86400:
                self.finish(event_id, "skipped", "messaging_window")
                return
            consent = (conversation["opened_at"] > now - 86400
                and payload["timestamp"] >= conversation["opened_at"] - 1
                and (payload.get("payload") == campaign.OPT_IN or campaign.words(text) in {"да", "да хочу видео", "пришли", "пришлите", "отправь", "отправьте"}))
            if not consent and not campaign.matches(text, settings["keywords"]):
                self.finish(event_id, "skipped", "no_keyword_or_consent")
                return
            if conversation["link_sent_at"] > now - 86400:
                self.finish(event_id, "skipped", "link_already_sent")
                return
            recipient = {"id": person}
            message = {"attachment": {"type": "template", "payload": {
                "template_type": "button", "text": campaign.choose(settings["link_text"], person),
                "buttons": [{"type": "web_url", "title": settings["link_label"], "url": settings["target_url"]}],
            }}}
        elif kind == "public":
            if not settings["public_reply_enabled"]:
                self.finish(event_id, "skipped", "public_reply_paused")
                return
            recipient, message = None, None
        else:
            self.finish(event_id, "skipped", "unsupported")
            return
        # Pause is checked again immediately before a side effect; no batch keeps an old enabled flag.
        latest = campaign.load_settings(self.config_path)
        if not latest["enabled"] or (kind == "public" and not latest["public_reply_enabled"]):
            self.finish(event_id, "skipped", "paused")
            return
        if kind == "comment":
            if not campaign.matches(current.get("text", ""), latest["keywords"]):
                self.finish(event_id, "skipped", "keyword_removed")
                return
            message = {"text": campaign.choose(latest["opening"], person), "quick_replies": [{
                "content_type": "text", "title": latest["opt_in_label"], "payload": campaign.OPT_IN}]}
        elif kind == "message":
            if not consent and not campaign.matches(text, latest["keywords"]):
                self.finish(event_id, "skipped", "keyword_removed")
                return
            message = {"attachment": {"type": "template", "payload": {
                "template_type": "button", "text": campaign.choose(latest["link_text"], person),
                "buttons": [{"type": "web_url", "title": latest["link_label"], "url": latest["target_url"]}],
            }}}
        self.finish(event_id, "sending")
        if kind == "public":
            response = self.graph("POST", payload["comment"] + "/replies", {"message": campaign.choose(latest["public_reply"], person)})
            if not response.get("id"):
                raise GraphError(uncertain=True)
        else:
            response = self.graph("POST", own_id + "/messages", {"recipient": recipient, "message": message})
            self.remember_sent(response)
            if kind == "comment":
                actual_person = str(response.get("recipient_id", person))
                self.update_person(person, opened_at=now, link_sent_at=0)
                if actual_person.isdigit() and actual_person != person:
                    self.update_person(actual_person, opened_at=now, link_sent_at=0)
                if latest["public_reply_enabled"]:
                    self.enqueue("public:" + payload["comment"], "public", payload)
            else:
                self.update_person(person, link_sent_at=now)
        self.finish(event_id, "sent")

    def status(self, label):
        with closing(self.connect()) as con:
            counts = dict(con.execute("SELECT status,count(*) FROM events GROUP BY status").fetchall())
        value = {"updated_at": time.time(), "connected": self.connected, "label": label,
                 "counts": counts, "last_webhook_at": self.last_ingress}
        temporary = self.state_dir / ".status.tmp"
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_dir / "status.json")
        return value

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return {"busy": True}
        try:
            try:
                settings = campaign.load_settings(self.config_path)
            except (OSError, ValueError, TypeError):
                self.connected = False
                return self.status("Настройки не прочитаны. Отправка остановлена.")
            if not self.ready():
                return self.status("Meta ещё не подключён или доступ не подтверждён. Сообщения не отправляются.")
            started = time.monotonic()
            with closing(self.connect()) as con:
                events = con.execute("SELECT * FROM events WHERE status='pending' ORDER BY created_at,id LIMIT 3").fetchall()
            for event in events:
                if time.monotonic() - started > 25:
                    break
                try:
                    self.process(event)
                except GraphError as error:
                    self.finish(event["id"], "unknown" if error.uncertain else "failed", "meta_" + str(error.code))
                    self.connected, self.checked_at = False, 0
                    break
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                    # Unknown local failures after a possible POST are quarantined too.
                    self.finish(event["id"], "unknown", "local_error")
            with closing(self.connect()) as con:
                con.execute("DELETE FROM events WHERE updated_at<? AND status NOT IN ('pending','sending','unknown','failed')", (time.time() - 30 * 86400,))
                con.execute("DELETE FROM sent_messages WHERE created_at<?", (time.time() - 30 * 86400,))
                con.commit()
            label = ("Meta @biomozg: аккаунт и подписки API подтверждены. " + ("Автоответы включены." if settings["enabled"] else "Автоответы выключены.")) if self.connected else "Meta не подтвердил запрос. Отправка остановлена; требуется проверка подключения."
            return self.status(label)
        finally:
            self.lock.release()


def serve(gateway, port=5680):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # No URLs, verify tokens, bodies or Authorization headers in logs.

        def reply(self, code, value, plain=False):
            data = str(value).encode() if plain else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8" if plain else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                self.reply(200, {"ok": True})
                return
            params = parse_qs(parsed.query)
            supplied = params.get("hub.verify_token", [""])[0]
            expected = gateway.credentials().get("verify_token", "")
            challenge = params.get("hub.challenge", [""])[0]
            if (parsed.path == "/instagram/webhook" and expected and supplied
                    and hmac.compare_digest(supplied, expected) and params.get("hub.mode") == ["subscribe"]
                    and len(challenge) <= 256):
                self.reply(200, challenge, plain=True)
            else:
                self.reply(403, {"ok": False})

        def do_POST(self):
            try:
                self.connection.settimeout(10)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 262144:
                    self.reply(413, {"ok": False})
                    return
                if self.path == "/instagram/runner":
                    expected = gateway.credentials().get("runner_token", "")
                    if not expected or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + expected):
                        self.reply(403, {"ok": False})
                        return
                    self.rfile.read(size)
                    self.reply(200, gateway.tick())
                    return
                body = self.rfile.read(size)
                if self.path != "/instagram/webhook" or not gateway.verify_signature(body, self.headers.get("X-Hub-Signature-256", "")):
                    self.reply(403, {"ok": False})
                    return
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError
                gateway.ingest(value)
                self.reply(200, {"ok": True})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"ok": False})
            except (OSError, sqlite3.Error):
                self.reply(503, {"ok": False})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    serve(Gateway(Path(os.getenv("INSTAGRAM_ROOT", "/srv/biomozg-instagram")), campaign.CONFIG_PATH))
