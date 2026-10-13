"""The browser's text fields, for a phone's keyboard. A phone brings its keyboard up only while a
tap is being handled, too soon to ask the browser what the tap focused, so the page keeps a map of
the fields in view and opens the keyboard when a tap lands on one; then it asks which field has the
focus, to put that right. Both come from one `eval` of FIELDS_SCRIPT, which looks into open shadow
roots and same-site frames, and takes a field's labels as more of it, since a tap on one focuses
it. A frame from another site can't be looked into, so a focus inside one is only known to be
there ("frame").

The page's own scripts could meddle with what FIELDS_SCRIPT finds, so in_view passes on numbers and
the few words below only: each field as fractions of the viewport (as the take-over page's points
are), with the keyboard it brings up.

  {"fields": [{"x": 0.05, "y": 0.2, "width": 0.9, "height": 0.06,
               "keyboard": {"mode": "email", "capitalize": "off", "correct": false}}],
   "focus": a field like those, "frame", or null}"""
import math

FRAME = "frame"
MOST_FIELDS = 50
# What the take-over page's keyboard needs to know of a field: where it is, its type ("textarea"
# and "editable" for those that aren't inputs), and its own keyboard settings, if it has any.
FIELDS_SCRIPT = """(() => {
  const TEXT_TYPES = new Set(["text", "search", "email", "url", "tel", "password", "number"]);
  const found = [];
  const typesInto = (el) => el.isContentEditable ? !el.parentElement?.isContentEditable
    : (el.localName === "textarea" || (el.localName === "input" && TEXT_TYPES.has(el.type))) && !el.disabled && !el.readOnly;
  const field = (el, offset, area = el) => {
    const box = area.getBoundingClientRect();
    return {x: offset.x + box.left, y: offset.y + box.top, width: box.width, height: box.height,
            type: el.localName === "input" ? el.type : el.localName === "textarea" ? "textarea" : "editable",
            inputmode: el.getAttribute("inputmode"), autocapitalize: el.getAttribute("autocapitalize"),
            autocorrect: el.getAttribute("autocorrect"), spellcheck: el.getAttribute("spellcheck")};
  };
  const onTop = (area, el, root, view) => {
    const box = area.getBoundingClientRect();
    const left = Math.max(box.left, 0), right = Math.min(box.right, view.innerWidth);
    const top = Math.max(box.top, 0), bottom = Math.min(box.bottom, view.innerHeight);
    if (right - left < 1 || bottom - top < 1) return false;
    const hit = root.elementFromPoint((left + right) / 2, (top + bottom) / 2);
    return area.contains(hit) || hit?.closest("label")?.control === el;
  };
  const inside = (frame) => { try { return frame.contentDocument; } catch (e) { return null; } };
  const past = (frame, offset) => {
    const box = frame.getBoundingClientRect();
    return {x: offset.x + box.left + frame.clientLeft, y: offset.y + box.top + frame.clientTop};
  };
  const look = (root, view, offset) => {
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) look(el.shadowRoot, view, offset);
      const doc = /^i?frame$/.test(el.localName) && inside(el);
      if (doc?.defaultView) look(doc, doc.defaultView, past(el, offset));
      else if (typesInto(el)) {
        for (const area of [el, ...(el.labels ?? [])]) if (onTop(area, el, root, view)) found.push(field(el, offset, area));
      }
    }
  };
  const focus = () => {
    let el = document.activeElement, offset = {x: 0, y: 0};
    while (el) {
      if (el.shadowRoot?.activeElement) { el = el.shadowRoot.activeElement; continue; }
      if (!/^i?frame$/.test(el.localName)) break;
      const doc = inside(el);
      if (!doc) return "frame";
      offset = past(el, offset);
      el = doc.activeElement;
    }
    return el && typesInto(el) ? field(el, offset) : null;
  };
  look(document, window, {x: 0, y: 0});
  return {width: innerWidth, height: innerHeight, fields: found.slice(0, %d), focus: focus()};
})()""" % MOST_FIELDS
KEYBOARD_MODES = {"text", "search", "email", "url", "tel", "numeric", "decimal"}  # inputmode, but "none"
MODE_FOR_TYPE = {"search": "search", "email": "email", "url": "url", "tel": "tel", "number": "decimal"}
PLAIN_TYPES = {"email", "url", "password", "tel", "number"}  # phones type into these without capitals or corrections
CAPITALIZE = {"off", "none", "on", "sentences", "words", "characters"}
SWITCHES = {"on": True, "off": False}


class Fields:
    def __init__(self, session):
        """session: the browser's agent-browser Session."""
        self.session = session

    def read(self):
        """The text fields in view and the focus, as the page takes them; NoBrowser if agent-browser
        fails, ValueError if what the browser's page answered isn't that."""
        return in_view(self.session.eval(FIELDS_SCRIPT))


def in_view(result):
    """FIELDS_SCRIPT's result as the take-over page takes it (see above). A field outside the
    viewport is left out, unless it has the focus."""
    try:
        size = (across(result["width"]), across(result["height"]))
        fields = [field(found, size) for found in result["fields"][:MOST_FIELDS]]
        focus = result["focus"]
        focus = FRAME if focus == FRAME else field(focus, size) if focus else None
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"not the fields in view: {e}") from None
    return {"fields": [shown for shown in fields if shown["width"] and shown["height"]], "focus": focus}


def field(found, size):
    """A field found as fractions of the viewport, kept within it, with the keyboard it brings up."""
    width, height = size
    left, top = finite(found["x"]) / width, finite(found["y"]) / height
    right, bottom = left + length(found["width"]) / width, top + length(found["height"]) / height
    left, right = within(left), within(right)
    top, bottom = within(top), within(bottom)
    return {"x": round(left, 4), "y": round(top, 4), "width": round(right - left, 4), "height": round(bottom - top, 4),
            "keyboard": keyboard(found)}


def keyboard(found):
    """The keyboard a field brings up on a phone: the one its inputmode asks for or its type's, and
    whether it capitalizes and corrects, as the field says or as phones do for its type."""
    kind = found.get("type")
    plain = kind in PLAIN_TYPES
    mode, capitalize = found.get("inputmode"), found.get("autocapitalize")
    correct = SWITCHES.get(found.get("autocorrect"), False if found.get("spellcheck") == "false" else not plain)
    return {"mode": mode if mode in KEYBOARD_MODES else MODE_FOR_TYPE.get(kind, "text"),
            "capitalize": capitalize if capitalize in CAPITALIZE else "off" if plain else "sentences",
            "correct": correct}


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError(f"not a number: {value!r}")
    return float(value)


def length(value):
    value = finite(value)
    if value < 0:
        raise TypeError(f"not a length: {value!r}")
    return value


def across(value):
    """A viewport's width or height, which the fields are fractions of."""
    value = finite(value)
    if value < 1:
        raise TypeError(f"not a viewport's size: {value!r}")
    return value


def within(fraction):
    return min(1.0, max(0.0, fraction))
