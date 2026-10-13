"""A small WebSocket client (RFC 6455) on the standard library, enough for the relay and for a
browser's stream server: text messages, pings and close. The bot's Python runs with no
dependencies, so this stands in for a library."""
import base64
import hashlib
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455's constant for the handshake
TEXT, CLOSE, PING, PONG, CONTINUATION = 0x1, 0x8, 0x9, 0xA, 0x0
HEADERS_MAX = 64 * 1024


class WebSocketClosed(Exception):
    pass


class WebSocket:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""
        self.fragments = []  # the parts of a message still arriving
        self.heard = time.monotonic()  # when the server last sent anything, a ping included

    @classmethod
    def connect(cls, url, headers=(), timeout=15):
        """A wss:// URL, or ws:// for a server on this machine. headers: extra (name, value) pairs
        for the handshake, such as Authorization."""
        parts = urlsplit(url)
        secure = parts.scheme == "wss"
        sock = socket.create_connection((parts.hostname, parts.port or (443 if secure else 80)), timeout)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        extra = "".join(f"{name}: {value}\r\n" for name, value in headers)
        sock.sendall((f"GET {target} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"{extra}\r\n").encode())
        ws = cls(sock)
        ws.check_handshake(key)
        return ws

    def check_handshake(self, key):
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > HEADERS_MAX:
                raise WebSocketClosed("handshake reply too long")
            self.fill()
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if " 101 " not in lines[0] + " " or headers.get("sec-websocket-accept") != accept:
            raise WebSocketClosed(f"handshake refused: {lines[0]}")

    def settimeout(self, seconds):
        self.sock.settimeout(seconds)

    def recv(self):
        """The next text message; WebSocketClosed when the server closes. Pings are answered
        on the way; socket.timeout passes through, so the caller can ping a quiet server, and
        what it cut off is read whole by the next call."""
        while True:
            final, opcode, payload = self.frame()
            if opcode == PING:
                self.send_frame(PONG, payload)
            elif opcode == CLOSE:
                raise WebSocketClosed(f"closed by the server ({payload[2:].decode(errors='replace') or 'no reason'})")
            elif opcode in (TEXT, CONTINUATION):
                self.fragments.append(payload)
                if final:
                    message, self.fragments = b"".join(self.fragments), []
                    return message.decode()

    def send(self, text):
        self.send_frame(TEXT, text.encode())

    def ping(self):
        self.send_frame(PING, b"")

    def close(self):
        try:
            self.send_frame(CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()

    def frame(self):
        """The next frame as (final, opcode, payload). It leaves the buffer only once it has all
        arrived, so a timeout halfway through loses nothing."""
        while (frame := self.take_frame()) is None:
            self.fill()
        return frame

    def take_frame(self):
        """The first frame in the buffer, taken out of it, or None while it's still arriving."""
        header = self.frame_header()
        if header is None:
            return None
        first, masked, length, start = header
        end = start + (4 if masked else 0) + length
        if len(self.buffer) < end:
            return None
        payload, mask = self.buffer[end - length:end], self.buffer[start:end - length]
        self.buffer = self.buffer[end:]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(first & 0x80), first & 0x0F, payload

    def frame_header(self):
        """(first byte, masked, payload length, header size) of the frame at the start of the
        buffer, or None while its header is still arriving."""
        if len(self.buffer) < 2:
            return None
        first, second = self.buffer[0], self.buffer[1]
        length, size = second & 0x7F, 2
        if length >= 126:
            size += 2 if length == 126 else 8
            if len(self.buffer) < size:
                return None
            length = int.from_bytes(self.buffer[2:size], "big")
        return first, bool(second & 0x80), length, size

    def send_frame(self, opcode, payload):
        """A client's frames are always masked (RFC 6455, 5.3)."""
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def fill(self):
        data = self.sock.recv(65536)
        if not data:
            raise WebSocketClosed("connection closed")
        self.heard = time.monotonic()
        self.buffer += data
