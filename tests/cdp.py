"""
Minimal Chrome DevTools Protocol client - stdlib only.

Just enough WebSocket (RFC 6455) to drive a headless Edge/Chrome: connect,
send commands, read replies and events. No dependencies, because the project
venv has no websocket library.
"""
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.request


class WebSocket:
    def __init__(self, url, timeout=30):
        assert url.startswith("ws://"), url
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        port = int(port or 80)

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            "GET /%s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ) % (path, host, port, key)
        self.sock.sendall(handshake.encode())

        header = self._read_until(b"\r\n\r\n")
        if b"101" not in header.split(b"\r\n")[0]:
            raise RuntimeError("websocket upgrade failed: %r" % header[:200])

        # The Sec-WebSocket-Accept digest is deliberately not verified. It
        # exists so an intermediary cannot be tricked into treating a cached
        # HTTP response as a WebSocket upgrade; this socket goes straight to
        # a DevTools port on loopback, so there is no intermediary to fool.
        # The 101 above, plus the command round-trips, is the real proof.
        if b"Upgrade" not in header:
            raise RuntimeError("no Upgrade header: %r" % header[:200])

    def _read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("socket closed during handshake")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(marker)
        return head + marker

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise RuntimeError("socket closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def send(self, text):
        payload = text.encode()
        header = bytearray([0x81])          # FIN + text
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)

        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        """Return the next complete text message, reassembling fragments."""
        message = b""
        while True:
            first, second = self._recv_exact(2)
            fin = first & 0x80
            opcode = first & 0x0F
            length = second & 0x7F

            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            payload = self._recv_exact(length) if length else b""

            if opcode == 0x8:                       # close
                raise RuntimeError("websocket closed by peer")
            if opcode == 0x9:                       # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0xA:                       # pong
                continue

            message += payload
            if fin:
                return message.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser():
    """Path to an installed Chromium-family browser, or None."""
    for path in CANDIDATES:
        if os.path.exists(path):
            return path
    return None


class Browser:
    """A headless Edge/Chrome instance addressed over CDP."""

    def __init__(self, exe, port=0, timeout=30):
        # Port 0 means "pick a free one": a debugging port left occupied by a
        # previous run would hand us that browser's targets instead of ours.
        if not port:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
        self.port = port
        self.timeout = timeout
        # A fresh profile per run. Chromium silently forwards to an already
        # running instance when the user-data-dir matches, which would attach
        # us to a stale browser from a previous run instead of a new one.
        self.profile = os.path.join(
            os.environ.get("TEMP", "."),
            "cdp-profile-%d-%d" % (port, int(time.time() * 1000)))
        self.proc = subprocess.Popen([
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=%s" % self.profile,
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        target = self._wait_for_target()
        self.ws = WebSocket(target, timeout=timeout)
        self.next_id = 0
        self.events = []

    def _wait_for_target(self):
        deadline = time.time() + self.timeout
        last = None
        while time.time() < deadline:
            try:
                raw = urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % self.port, timeout=2).read()
                for entry in json.loads(raw):
                    if entry.get("type") == "page" and entry.get("webSocketDebuggerUrl"):
                        return entry["webSocketDebuggerUrl"]
            except Exception as error:      # browser still starting
                last = error
            time.sleep(0.25)
        raise RuntimeError("no debuggable page target (%s)" % last)

    def call(self, method, **params):
        self.next_id += 1
        message_id = self.next_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params}))

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            message = json.loads(self.ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError("%s -> %s" % (method, message["error"]))
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)
        raise RuntimeError("timed out waiting for %s" % method)

    def evaluate(self, expression, await_promise=True):
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=await_promise,
            returnByValue=True,
        )
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise RuntimeError("page threw: %s" % text)
        return result.get("result", {}).get("value")

    def close(self):
        """
        Shut the browser down, children included.

        Chromium forks a renderer, a GPU process and several utilities.
        Terminating only the process we launched leaves those running: a
        few dozen runs had piled up over a hundred stray processes, which
        starved later runs and made them time out or flake.
        """
        try:
            self.ws.close()
        except Exception:
            pass

        # Kill the tree while it is still a tree. Terminating the launcher
        # first re-parents its renderer and GPU children, and taskkill /T can
        # then no longer find them by parent - which is how a hundred stray
        # processes accumulated even with a kill in place.
        self._kill_tree()

        try:
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

        # The profile is a fresh temporary directory per run.
        shutil.rmtree(self.profile, ignore_errors=True)

    def _kill_tree(self):
        if os.name != "nt":
            try:
                self.proc.kill()
            except Exception:
                pass
            return

        # /T takes the children with it, /F does not ask twice.
        subprocess.run(
            ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )

        # Edge's --headless=new launcher hands off to a browser process that
        # is not in our tree, so taskkill misses it. Every process of this
        # run carries our unique profile directory on its command line, which
        # identifies them exactly - and cannot match the user's own browser.
        marker = os.path.basename(self.profile)
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or "
             "Name='chrome.exe'\" | Where-Object { $_.CommandLine -like "
             "'*%s*' } | ForEach-Object { Stop-Process -Id $_.ProcessId "
             "-Force -ErrorAction SilentlyContinue }" % marker],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
