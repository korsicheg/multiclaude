"""What the person does on the take-over page, as the input events agent-browser's stream server
takes. The page sends one action at a time ({"kind": "press", "x": 0.42, "y": 0.17}), its points
as fractions of the frame it shows; they become the page's CSS pixels here. The stream server
drops an event that lacks a field it expects, so every event carries all of them."""

KEY_CODES = {"Enter": 13, "Backspace": 8, "Tab": 9}  # the keys the page has buttons for: Windows key codes
TYPED_BY_KEY = {"Enter": "\r"}                       # what a key types, for those that type something
KEY_FOR_CHARACTER = {"\n": "Enter", "\t": "Tab"}
SPACE_KEY_CODE = 32
SCROLL_STEP = 400  # CSS pixels per tap on a scroll button
SCROLL_DIRECTIONS = {"up": -1, "down": 1}
TEXT_MAX = 1000    # characters in one Send


def events(action, frame):
    """The stream input events for one action from the page; ValueError for one it can't be.
    frame: the latest Frame, which points are mapped onto."""
    kind = action.get("kind") if isinstance(action, dict) else None
    if kind == "text":
        return typed(action.get("text"))
    if kind == "key":
        return key_press(action.get("key"))
    pointing = {"press": press, "move": move, "release": release,
                "scroll": lambda point: scroll(point, action.get("direction"))}
    if kind not in pointing:
        raise ValueError(f"no such action: {kind!r}")
    if frame is None:
        raise ValueError("no frame to point at yet")
    return pointing[kind](frame.device_point(*fractions(action)))


def press(point):
    """The pointer goes down: over the point first, as a real mouse would be."""
    return [mouse("mouseMoved", point, button="none"), mouse("mousePressed", point, click_count=1)]


def move(point):
    """The pointer moves while it's down: a drag, such as a slider's."""
    return [mouse("mouseMoved", point)]


def release(point):
    return [mouse("mouseReleased", point, click_count=1)]


def scroll(point, direction):
    """A wheel turn at the point the person last touched, so a scrolling box under it scrolls."""
    if direction not in SCROLL_DIRECTIONS:
        raise ValueError(f"no such scroll direction: {direction!r}")
    return [mouse("mouseWheel", point, button="none", deltaX=0, deltaY=SCROLL_DIRECTIONS[direction] * SCROLL_STEP)]


def fractions(action):
    """The action's point, each coordinate a fraction of the frame, kept within it."""
    try:
        return tuple(min(1.0, max(0.0, float(action[axis]))) for axis in ("x", "y"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("a point needs x and y") from None


def mouse(event_type, point, button="left", click_count=0, **wheel):
    x, y = point
    return {"type": "input_mouse", "eventType": event_type, "x": x, "y": y, "button": button,
            "clickCount": click_count, "modifiers": 0, **wheel}


def typed(text):
    """Each character as a key going down, which types it, and up again."""
    if not isinstance(text, str) or not text or len(text) > TEXT_MAX:
        raise ValueError(f"text to type: 1 to {TEXT_MAX} characters")
    return [event for character in text for event in character_press(character)]


def character_press(character):
    if character in KEY_FOR_CHARACTER:
        return key_press(KEY_FOR_CHARACTER[character])
    return key_events(character, "", character, character_key_code(character))


def character_key_code(character):
    """The Windows key code pages see for a typed character: a letter's or digit's key, or none."""
    if character == " ":
        return SPACE_KEY_CODE
    return ord(character.upper()) if character.isascii() and character.isalnum() else 0


def key_press(key):
    if key not in KEY_CODES:
        raise ValueError(f"no button for the key {key!r}")
    return key_events(key, key, TYPED_BY_KEY.get(key, ""), KEY_CODES[key])


def key_events(key, code, typed_text, key_code):
    """A key going down, which types typed_text, and up again."""
    fields = {"type": "input_keyboard", "key": key, "code": code, "windowsVirtualKeyCode": key_code, "modifiers": 0}
    return [{**fields, "eventType": "keyDown", "text": typed_text}, {**fields, "eventType": "keyUp", "text": ""}]
