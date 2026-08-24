#!/usr/bin/env python3
"""
Connect this computer to the Amelia's Agent app.

Your machine sits behind a router that will not accept incoming connections, so
the app cannot dial it. This dials *out* instead and holds the connection open;
requests from your phone arrive down that pipe and are handed to the server
already running on this machine.

Run it, read out the six-digit code, and type that into the app once. After that
the pairing is remembered and this just needs to be running.

    python3 connect.py

Nothing but the standard library is used on purpose: the only promise the README
makes is python3, and a connector that needs `pip install` first is a connector
that fails for the exact beginner it was written for.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.request
from typing import Optional

DEFAULT_API = os.environ.get("AMELIA_API", "https://api.ameliasagent.com")
DEFAULT_LOCAL = os.environ.get("AMELIA_LOCAL", "127.0.0.1:8787")
STATE = os.path.expanduser("~/.amelia/machine.json")


# ── WebSocket, by hand ───────────────────────────────────────────────────────
# RFC 6455 is small enough to implement directly, and doing so keeps the
# dependency list empty.

class WS:
    """A minimal client. Text frames only, which is all the protocol uses."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._send_lock = threading.Lock()  # replies are written from many threads
        self._buf = b""

    @classmethod
    def connect(cls, url: str, timeout: float = 30.0) -> "WS":
        secure = url.startswith("wss://")
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port_s = hostport.partition(":")
        port = int(port_s) if port_s else (443 if secure else 80)

        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)

        key = base64.b64encode(secrets.token_bytes(16)).decode()
        raw.sendall(
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n".encode()
        )

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                raise ConnectionError("The server closed during the handshake")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raise ConnectionError(f"The server refused the connection: {status}")

        ws = cls(raw)
        ws._buf = head.split(b"\r\n\r\n", 1)[1]
        # Reads block on a long-poll, so no timeout after the handshake.
        raw.settimeout(None)
        return ws

    # -- framing ------------------------------------------------------------
    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("The connection dropped")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> Optional[str]:
        """Next complete text message, or None once the socket closes."""
        payload = b""
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            masked, length = b1 & 0x80, b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else b""
            data = self._read(length) if length else b""
            if masked:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))

            if opcode == 0x8:            # close
                return None
            if opcode == 0x9:            # ping — must be answered or we get dropped
                self._frame(0xA, data)
                continue
            if opcode == 0xA:            # pong
                continue

            payload += data
            # opcode 0x0 is a continuation, so keep going until FIN.
            if fin:
                return payload.decode("utf-8", errors="replace")

    def _frame(self, opcode: int, data: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(data)
        # The client side must mask every frame; servers reject unmasked ones.
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        header += mask
        body = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        with self._send_lock:
            self.sock.sendall(bytes(header) + body)

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode())

    def ping(self) -> None:
        self._frame(0x9, b"")

    def close(self) -> None:
        try:
            self._frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ── Pairing ──────────────────────────────────────────────────────────────────

def post(api: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        api + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def load_token() -> Optional[str]:
    try:
        with open(STATE) as f:
            return json.load(f).get("token")
    except (OSError, ValueError):
        return None


def save_token(token: str, machine_id: str) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    # This token authorises running commands on this computer, so it is written
    # owner-only and the mode is set before the secret goes in.
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "machine_id": machine_id}, f)


def enrol(api: str, name: str) -> str:
    """Register this machine and show the code that attaches it to an account."""
    r = post(api, "/v1/machines/code", {"name": name})
    code, token = r["code"], r["token"]

    # Saved before the code is shown, not after it is claimed. The machine is
    # parked on a placeholder account until someone claims the code, so the
    # token grants no access to anybody yet — and saving first means a Ctrl-C at
    # the wrong moment does not strand a machine that can never be reconnected.
    save_token(token, r.get("machineId", ""))

    # Sized from the content rather than hand-drawn, so the frame cannot drift
    # out of alignment when the wording changes.
    label = f"  Your code:   {code}  "
    print()
    print("  ┌" + "─" * len(label) + "┐")
    print("  │" + label + "│")
    print("  └" + "─" * len(label) + "┘")
    print()
    print("  Open Amelia's Agent → Profile, sign in, and type that code.")
    print("  It is good for 10 minutes. Re-run with --reset for a new one.")
    print()
    sys.stdout.flush()
    return token


# ── Serving requests ─────────────────────────────────────────────────────────

def serve_one(ws: WS, local: str, frame: dict) -> None:
    """Hand one proxied request to the local server and send back the reply."""
    rid = frame.get("id")
    try:
        host, _, port = local.partition(":")
        conn = http.client.HTTPConnection(host, int(port or 80), timeout=180)
        body = base64.b64decode(frame["body"]) if frame.get("body") else None
        headers = dict(frame.get("headers") or {})

        # React Native cannot send a Cookie header — it is a forbidden header
        # name, and the native cookie jar is not replayed for fetch. So the app
        # carries its session in X-Hermes-Session instead, and it is turned back
        # into a cookie here, at the last hop before the server sees it.
        #
        # Doing the translation in the connector rather than patching the server
        # means this works against an unmodified upstream Hermes: nothing
        # downstream has to know the app has that limitation.
        token = headers.pop("x-hermes-session", None)
        if token and "cookie" not in headers:
            headers["Cookie"] = token if "=" in token else f"hermes_session={token}"

        conn.request(frame.get("method", "GET"), frame.get("path", "/"),
                     body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        out = {
            "t": "res", "id": rid, "status": res.status,
            "headers": {k.lower(): v for k, v in res.getheaders()
                        if k.lower() not in ("transfer-encoding", "connection", "content-length")},
            "body": base64.b64encode(data).decode() if data else None,
        }
        conn.close()
    except Exception as e:
        # A local failure is reported as a gateway error rather than silence, so
        # the app can say something true instead of timing out.
        out = {"t": "res", "id": rid, "status": 502, "headers": {"content-type": "application/json"},
               "body": base64.b64encode(json.dumps({"error": str(e)}).encode()).decode()}
    try:
        ws.send(json.dumps(out))
    except OSError:
        pass


def pump(api: str, token: str, local: str) -> None:
    ws_url = api.replace("https://", "wss://").replace("http://", "ws://")
    ws = WS.connect(f"{ws_url}/v1/relay/machine?token={token}")
    print(f"  Connected. Serving {local} to your phone. Ctrl-C to stop.")

    # Idle connections get culled by proxies; a periodic ping keeps it alive.
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(25):
            try:
                ws.ping()
            except OSError:
                return

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        while True:
            msg = ws.recv()
            if msg is None:
                return
            try:
                frame = json.loads(msg)
            except ValueError:
                continue
            if frame.get("t") == "req":
                # One thread per request so a long agent turn cannot block the
                # status polls the app makes while waiting for it.
                threading.Thread(target=serve_one, args=(ws, local, frame), daemon=True).start()
    finally:
        stop.set()
        ws.close()


def main() -> None:
    # Without this, Python block-buffers stdout whenever it is not a terminal,
    # so under launchd or any `> log` redirect the pairing code sits in a buffer
    # and the user stares at an empty file waiting for a number that was printed
    # minutes ago.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    p = argparse.ArgumentParser(description="Connect this computer to Amelia's Agent")
    p.add_argument("--api", default=DEFAULT_API)
    p.add_argument("--local", default=DEFAULT_LOCAL, help="host:port of the server on this machine")
    p.add_argument("--name", default=socket.gethostname())
    p.add_argument("--reset", action="store_true", help="forget this machine and pair again")
    a = p.parse_args()

    if a.reset:
        try:
            os.remove(STATE)
        except OSError:
            pass

    # Fail early with a clear cause: without a local server there is nothing to
    # serve, and a connector that pairs and then 502s teaches nothing.
    try:
        h, _, prt = a.local.partition(":")
        socket.create_connection((h, int(prt or 80)), timeout=5).close()
    except OSError:
        raise SystemExit(
            f"Nothing is listening on {a.local}.\n"
            "Start it first:  python3 bootstrap.py"
        )

    token = load_token() or enrol(a.api, a.name)

    # Reconnect for as long as it takes; a laptop lid or a dropped Wi-Fi should
    # not mean walking back to the terminal.
    backoff = 1.0
    while True:
        try:
            pump(a.api, token, a.local)
            backoff = 1.0
        except KeyboardInterrupt:
            print("\n  Disconnected.")
            return
        except Exception as e:
            print(f"  Lost the connection ({e}). Retrying in {int(backoff)}s…")
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            print("\n  Disconnected.")
            return
        backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    main()
