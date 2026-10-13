"""The bridge to an agent-browser session's stream server, a WebSocket on 127.0.0.1. The server
sends a JPEG frame of the page whenever it changes (`{"type": "frame", "data", "metadata"}`) and
takes input events; a thread keeps only the latest frame, for the page to fetch."""
import base64
import json
import struct
import threading
from dataclasses import dataclass

from multiclaude.websocket import WebSocket, WebSocketClosed

# The JPEG markers that start a frame header, which holds the image's size: SOF0 to SOF15
# without DHT (C4), JPG (C8) and DAC (CC), which share the range.
FRAME_HEADER_MARKERS = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


@dataclass(frozen=True)
class Frame:
    seq: int             # counts up from 1 with each frame
    jpeg: bytes
    width: int           # the image's pixels
    height: int
    device_width: float  # the page's CSS pixels across

    def device_point(self, across, down):
        """The page's CSS pixel at a point given as fractions of the frame across and down. The
        frame shows the page's viewport, which can be shorter than the deviceHeight the stream
        reports (the window's), so both axes take the scale of the width."""
        scale = self.device_width / self.width
        return round(across * self.width * scale, 1), round(down * self.height * scale, 1)


class Stream:
    def __init__(self, ws):
        self.ws = ws
        self.frame = None    # the latest Frame
        self.closed = False  # the stream ended: the browser closed, or the take-over did
        self.changed = threading.Condition()
        self.sending = threading.Lock()

    @classmethod
    def connect(cls, port):
        """Connect to the stream server on port and start following its frames."""
        ws = WebSocket.connect(f"ws://127.0.0.1:{port}/")
        ws.settimeout(None)  # a page that doesn't change sends nothing
        stream = cls(ws)
        threading.Thread(target=stream.follow, daemon=True).start()
        return stream

    def follow(self):
        try:
            while True:
                self.take(self.ws.recv())
        except (WebSocketClosed, OSError):
            self.close()

    def take(self, message):
        """Keep a frame message as the latest frame. Other messages (status, tabs) are left, and so
        is a frame that can't be read: the next one replaces it."""
        try:
            message = json.loads(message)
            if message.get("type") != "frame":
                return
            jpeg = base64.b64decode(message["data"])
            width, height = jpeg_size(jpeg)
            device_width = float(message["metadata"]["deviceWidth"])
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        with self.changed:
            seq = self.frame.seq + 1 if self.frame else 1
            self.frame = Frame(seq, jpeg, width, height, device_width)
            self.changed.notify_all()

    def next_frame(self, after, wait):
        """The latest frame once it's newer than the seq after; None if none came within wait
        seconds or the stream ended."""
        with self.changed:
            self.changed.wait_for(lambda: self.closed or self.newer_than(after), wait)
            return self.frame if self.newer_than(after) and not self.closed else None

    def newer_than(self, seq):
        return self.frame is not None and self.frame.seq > seq

    def send(self, events):
        """Send input events, in order; OSError if the stream is gone."""
        with self.sending:
            for event in events:
                self.ws.send(json.dumps(event))

    def close(self):
        with self.changed:
            if self.closed:
                return
            self.closed = True
            self.changed.notify_all()
        self.ws.close()


def jpeg_size(jpeg):
    """(width, height) of a JPEG image, from its frame header; ValueError if it has none."""
    at = 2  # past the start-of-image marker
    while at + 9 <= len(jpeg):
        marker, length = jpeg[at + 1], int.from_bytes(jpeg[at + 2:at + 4], "big")
        if marker in FRAME_HEADER_MARKERS:
            height, width = struct.unpack(">HH", jpeg[at + 5:at + 9])
            if width and height:
                return width, height
            break
        at += 2 + length
    raise ValueError("a frame that isn't a JPEG image")
