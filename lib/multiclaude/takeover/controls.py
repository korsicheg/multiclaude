"""What the person does on the take-over page, as the input events agent-browser's stream server
takes. The page sends one action at a time, its points and wheel turns as fractions of the frame
it shows; they become the page's CSS pixels here. The stream server drops an event that lacks a
field it expects, so every event carries all of them.

  {"kind": "press", "x": 0.42, "y": 0.17, "button": "left", "clicks": 1, "modifiers": 0}
      a mouse button or a finger going down; "release", with the same fields, is it coming up
  {"kind": "move", "x": 0.42, "y": 0.17, "button": "none"}
      the pointer moving: hovering, or dragging with a button down ("left")
  {"kind": "wheel", "x": 0.42, "y": 0.17, "dx": 0, "dy": 0.25}
      a wheel or trackpad turn over the point
  {"kind": "text", "text": "Hello"}
      characters to type
  {"kind": "key", "key": "ArrowLeft", "modifiers": 8, "times": 1}
      a key that types nothing (Enter types its return), pressed and let go, or a shortcut's
      letter ({"key": "a", "modifiers": 2} is Control+A); times presses it over and over, as a
      phone's keyboard does to delete a word autocorrect replaces

The fields shown after the point are optional, with those defaults. Modifiers are the keys held,
added up: Alt 1, Control 2, Meta 4, Shift 8."""
from dataclasses import dataclass

KEY_CODES = {"Enter": 13, "Tab": 9, "Backspace": 8, "Escape": 27, "Delete": 46,  # Windows key codes
             "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
             "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40}
TYPED_BY_KEY = {"Enter": "\r"}  # what a key types, for those that type something
KEY_FOR_CHARACTER = {"\n": "Enter", "\t": "Tab"}
SPACE_KEY_CODE = 32
BUTTONS = ("left", "middle", "right")
MOST_CLICKS = 3            # a triple click selects a paragraph
MODIFIERS = range(16)      # every sum of Alt 1, Control 2, Meta 4 and Shift 8
MOST_TURN = 10             # frames' worth of scrolling in one wheel turn
TEXT_MAX = 1000            # characters in one action
MOST_PRESSES = 100         # of a key in one action


@dataclass(frozen=True)
class Key:
    name: str      # KeyboardEvent.key
    code: str      # KeyboardEvent.code, the key's place on the keyboard
    text: str      # what it types
    key_code: int  # the Windows key code pages see as keyCode

    def events(self, modifiers=0):
        """The key going down, which types its text, and up again."""
        fields = {"type": "input_keyboard", "key": self.name, "code": self.code,
                  "windowsVirtualKeyCode": self.key_code, "modifiers": modifiers}
        return [{**fields, "eventType": "keyDown", "text": self.text}, {**fields, "eventType": "keyUp", "text": ""}]


def events(action, frame):
    """The stream input events for one action from the page; ValueError for one it can't be.
    frame: the latest Frame, which points are mapped onto."""
    kind = action.get("kind") if isinstance(action, dict) else None
    if kind == "text":
        return typed(action.get("text"))
    if kind == "key":
        return pressed_key(action.get("key")).events(modifiers(action)) * presses(action)
    pointing = {"press": press, "release": release, "move": move, "wheel": wheel}
    if not isinstance(kind, str) or kind not in pointing:  # a list or an object can't be looked up
        raise ValueError(f"no such action: {kind!r}")
    if frame is None:
        raise ValueError("no frame to point at yet")
    return pointing[kind](action, frame)


def press(action, frame):
    """A button goes down: over the point first, as a real mouse would be."""
    point = point_of(action, frame)
    return [mouse("mouseMoved", point), mouse("mousePressed", point, **clicking(action))]


def release(action, frame):
    return [mouse("mouseReleased", point_of(action, frame), **clicking(action))]


def move(action, frame):
    """The pointer moves: hovering, or dragging with a button down (a slider's, a selection's)."""
    return [mouse("mouseMoved", point_of(action, frame), button=choice(action, "button", ("none", *BUTTONS), "none"))]


def wheel(action, frame):
    """A wheel turn over the point, so the scrolling box under it scrolls."""
    delta_x, delta_y = frame.css_pixels(*(number(action, axis, -MOST_TURN, MOST_TURN) for axis in ("dx", "dy")))
    return [mouse("mouseWheel", point_of(action, frame), deltaX=delta_x, deltaY=delta_y)]


def point_of(action, frame):
    """The action's point, in the page's CSS pixels: x and y are fractions of the frame, kept within it."""
    return frame.css_pixels(*(number(action, axis, 0, 1) for axis in ("x", "y")))


def clicking(action):
    """A press's or release's button, which click of a double or triple click it is, and the keys held."""
    return {"button": choice(action, "button", BUTTONS, "left"),
            "clickCount": round(number(action, "clicks", 1, MOST_CLICKS, default=1)), "modifiers": modifiers(action)}


def mouse(event_type, point, **fields):
    """A mouse event at point, with no button down and no keys held unless fields say so."""
    x, y = point
    return {"type": "input_mouse", "eventType": event_type, "x": x, "y": y,
            "button": "none", "clickCount": 0, "modifiers": 0, **fields}


def number(action, name, low, high, default=None):
    """action[name], or default if it has none, as a number kept within low and high."""
    try:
        value = float(action.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name}: a number") from None
    return min(high, max(low, value))


def choice(action, name, choices, default):
    value = action.get(name, default)
    if value not in choices:
        raise ValueError(f"no such {name}: {value!r}")
    return value


def modifiers(action):
    value = action.get("modifiers", 0)
    if value not in MODIFIERS:
        raise ValueError(f"no such modifiers: {value!r}")
    return int(value)


def presses(action):
    return round(number(action, "times", 1, MOST_PRESSES, default=1))


def typed(text):
    """Each character as a key going down, which types it, and up again."""
    if not isinstance(text, str) or not text or len(text) > TEXT_MAX:
        raise ValueError(f"text to type: 1 to {TEXT_MAX} characters")
    return [event for character in text for event in character_key(character).events()]


def character_key(character):
    if character in KEY_FOR_CHARACTER:
        return named_key(KEY_FOR_CHARACTER[character])
    return Key(character, "", character, character_key_code(character))


def character_key_code(character):
    """The Windows key code pages see for a typed character: a letter's or digit's key, or none."""
    if character == " ":
        return SPACE_KEY_CODE
    return ord(character.upper()) if character.isascii() and character.isalnum() else 0


def pressed_key(name):
    """A key pressed for itself: one of KEY_CODES, or a letter, which types nothing (a shortcut's)."""
    if isinstance(name, str) and name in KEY_CODES:
        return named_key(name)
    if isinstance(name, str) and len(name) == 1 and name.isascii() and name.isalpha():
        return Key(name, f"Key{name.upper()}", "", ord(name.upper()))
    raise ValueError(f"no such key: {name!r}")


def named_key(name):
    return Key(name, name, TYPED_BY_KEY.get(name, ""), KEY_CODES[name])
