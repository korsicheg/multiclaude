"""The core's way in from the relay: the WebSocket client, the relay entrance and state/relay.json.
Nothing here reaches the relay."""
import json
import socket
import threading

import pytest
from conftest import fresh_multiclaude


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_ROOT", str(tmp_path / "assistant"))
    fresh_multiclaude(monkeypatch)
    from multiclaude import websocket
    from multiclaude.router import relay_entrance
    return type("Lib", (), {"websocket": websocket, "entrance": relay_entrance})


def server_frame(opcode, payload):
    """A frame as a server sends it: unmasked."""
    assert len(payload) < 126
    return bytes([0x80 | opcode, len(payload)]) + payload


def test_websocket_reads_text_answers_pings_and_masks_what_it_sends(lib):
    ours, theirs = socket.socketpair()
    ws = lib.websocket.WebSocket(ours)
    theirs.sendall(server_frame(0x9, b"hi") + server_frame(0x1, b'{"type":"hello"}'))
    assert ws.recv() == '{"type":"hello"}'
    pong = theirs.recv(64)
    assert pong[0] == 0x80 | 0xA and pong[1] & 0x80  # a masked pong
    mask, body = pong[2:6], pong[6:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == b"hi"

    ws.send("ack")
    sent = theirs.recv(64)
    mask, body = sent[2:6], sent[6:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == b"ack"

    theirs.sendall(server_frame(0x8, b"\x03\xe8bye"))
    with pytest.raises(lib.websocket.WebSocketClosed, match="bye"):
        ws.recv()


def test_websocket_long_frames_use_the_extended_length(lib):
    ours, theirs = socket.socketpair()
    ws = lib.websocket.WebSocket(ours)
    received = []
    reader = threading.Thread(target=lambda: received.append(theirs.recv(70000)))
    reader.start()
    ws.send("x" * 300)
    reader.join(5)
    assert received[0][1] == 0x80 | 126 and int.from_bytes(received[0][2:4], "big") == 300


def test_websocket_reads_a_frame_whole_when_a_timeout_cuts_it_off(lib):
    ours, theirs = socket.socketpair()
    ws = lib.websocket.WebSocket(ours)
    ws.settimeout(0.05)
    frame = server_frame(0x1, b'{"type":"hello"}')
    theirs.sendall(frame[:5])
    with pytest.raises(socket.timeout):
        ws.recv()
    theirs.sendall(frame[5:])
    assert ws.recv() == '{"type":"hello"}'


class Stop(BaseException):
    """Ends RelayEntrance.run's endless loop in a test."""


def test_a_connection_that_goes_quiet_is_pinged_then_dropped(lib, monkeypatch):
    ours, theirs = socket.socketpair()
    monkeypatch.setattr(lib.entrance, "QUIET_PING", 0.05)
    monkeypatch.setattr(lib.entrance, "DEAD_AFTER", 0.3)
    handshakes = []
    monkeypatch.setattr(lib.entrance.WebSocket, "connect",
                        lambda url, headers: handshakes.append((url, headers)) or lib.websocket.WebSocket(ours))
    entrance = lib.entrance.RelayEntrance("wss://relay.test/queue", "vm-token")
    theirs.sendall(server_frame(0x1, b'{"type":"hello"}'))

    with pytest.raises(lib.websocket.WebSocketClosed, match="nothing heard"):
        entrance.listen()

    assert entrance.greeted
    assert handshakes == [("wss://relay.test/queue", [("Authorization", "Bearer vm-token")])]
    theirs.settimeout(1)
    assert theirs.recv(64)[0] == 0x80 | 0x9  # pinged before giving up


def test_after_a_working_connection_drops_the_relay_is_reached_again_at_once(lib, monkeypatch):
    entrance = lib.entrance.RelayEntrance("wss://relay.test/queue", "vm-token")
    tries, sleeps = [], []

    def listen():
        tries.append(1)
        if len(tries) == 3:
            raise Stop
        entrance.greeted = len(tries) == 1  # the first connection worked, the second never did
        raise lib.websocket.WebSocketClosed("connection closed")

    monkeypatch.setattr(entrance, "listen", listen)
    monkeypatch.setattr(lib.entrance.time, "sleep", sleeps.append)
    with pytest.raises(Stop):
        entrance.run()
    assert sleeps == [lib.entrance.RETRY_FIRST]


def test_the_relay_is_read_from_state_and_none_is_no_relay(lib, tmp_path):
    assert lib.entrance.settings() == {} and lib.entrance.address("/app/") is None and lib.entrance.token() is None
    (tmp_path / "assistant" / "state").mkdir(parents=True)
    (tmp_path / "assistant" / "state" / "relay.json").write_text(
        json.dumps({"relay_url": "wss://relay.test/queue", "relay_token": "vm-token"}))
    assert lib.entrance.address("/app/") == "https://relay.test/app/"
    assert lib.entrance.token() == "vm-token"
