"""Reading Claude Code's pickers and dialogs off the screen and answering them."""
import re
import time
from dataclasses import dataclass

PICKER_ROW_RE = re.compile(r"^\s*(❯)?\s*\d+\.\s+(\S+)")
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# The line each picker shows once it's open.
OPEN_HINT = {"model": "s to use this session only", "effort": "s for this session only"}
# Mid-conversation, Claude Code asks first: the new setting can't use the prompt cache.
CONFIRM_PROMPTS = ("Switch model?", "Change effort level?")
SETTLE = 0.4  # for the screen to catch up after moving the picker


def picker_rows(screen):
    """[(has the cursor, label)] for the rows of the newest /model picker on screen."""
    lines = screen.splitlines()
    start = max((i for i, line in enumerate(lines) if "Select model" in line), default=0)
    return [(bool(m[1]), m[2]) for line in lines[start:] if (m := PICKER_ROW_RE.match(line))]


def effort_slider(screen):
    """(levels on the /effort slider, the level its ▲ points at)."""
    lines = screen.splitlines()
    for i in range(len(lines) - 1, 0, -1):
        if "▲" not in lines[i - 1]:
            continue
        levels = [(m.start() + len(m[0]) / 2, m[0]) for m in re.finditer(r"[a-z]+", lines[i]) if m[0] in EFFORTS]
        if levels:
            col = lines[i - 1].index("▲")
            return [name for _, name in levels], min(levels, key=lambda lv: abs(lv[0] - col))[1]
    return [], None


def asks_to_confirm(screen):
    return any(prompt in screen for prompt in CONFIRM_PROMPTS) and "❯ 1. Yes" in screen


def pick_model(pane, screen, target):
    """Move the /model picker's cursor to target's row. Returns a problem, or None."""
    rows = picker_rows(screen)
    labels = [label.lower() for _, label in rows]
    if target not in labels:
        return f"there's no {target} in the model list (it has {', '.join(labels)})"
    here = next((i for i, (cursor, _) in enumerate(rows) if cursor), 0)
    there = labels.index(target)
    pane.press("Down" if there > here else "Up", times=abs(there - here))
    time.sleep(SETTLE)
    if (True, target) not in [(cursor, label.lower()) for cursor, label in picker_rows(pane.screen())]:
        return f"couldn't move the model list to {target}"
    return None


def pick_effort(pane, screen, target):
    """Slide the /effort slider to target. Returns a problem, or None."""
    levels, _ = effort_slider(screen)
    if target not in levels:
        return f"there's no {target} on the effort slider (it has {', '.join(levels)})"
    pane.press("Left", times=len(levels))  # all the way down, then count up
    pane.press("Right", times=levels.index(target))
    time.sleep(SETTLE)
    if effort_slider(pane.screen())[1] != target:
        return f"couldn't move the effort slider to {target}"
    return None


PICKERS = {"model": pick_model, "effort": pick_effort}


# Claude Code's approval dialog. Even with bypassPermissions a few built-in safety checks stop
# and ask: a full-width rule, the tool and what it wants to do, the reason, a countdown to the
# automatic deny, "Do you want to proceed?" and numbered options with a ❯ at the cursor.
PROCEED = "Do you want to proceed?"
RULE_RE = re.compile(r"^─{20,}$")
OPTION_RE = re.compile(r"^\s*(❯)?\s*\d+\.\s+(.+?)\s*$")
COUNTDOWN_RE = re.compile(r"automatically deny this request in (\d+):(\d{2})")


@dataclass(frozen=True)
class Dialog:
    text: str        # what it asks about: the tool, what it wants to do, the command, the reason
    options: tuple   # ((has the cursor, label), ...)
    deny_in: int     # seconds until Claude Code denies it by itself, or None

    def find(self, word):
        """The index of the option whose label starts with word ("Yes", "No"), or None."""
        return next((i for i, (_, label) in enumerate(self.options) if label.startswith(word)), None)

    @property
    def cursor(self):
        return next((i for i, (cursor, _) in enumerate(self.options) if cursor), 0)


def approval_dialog(screen):
    """The approval dialog on screen, or None."""
    lines = screen.splitlines()
    ask = max((i for i, line in enumerate(lines) if line.strip() == PROCEED), default=None)
    if ask is None:
        return None
    options = []
    for line in lines[ask + 1:]:
        if m := OPTION_RE.match(line):
            options.append((bool(m[1]), m[2]))
        elif line.strip() and options:
            break
    if not options:
        return None
    top = max((i for i in range(ask) if RULE_RE.match(lines[i].strip())), default=-1)
    body, deny_in = [], None
    for line in lines[top + 1:ask]:
        text = line.strip()
        if not text or set(text) <= {"╌"}:
            continue
        if m := COUNTDOWN_RE.search(text):
            deny_in = int(m[1]) * 60 + int(m[2])
            continue
        body.append(text.removeprefix("│").strip())
    return Dialog("\n".join(body), tuple(options), deny_in)


def choose(pane, dialog, word):
    """Move the dialog's cursor to the option starting with word, check it got there, press
    Enter. (Number keys aren't used: some of them answer at once.) Returns a problem, or None."""
    target = dialog.find(word)
    if target is None:
        return f"the dialog has no {word} option"
    pane.press("Down" if target > dialog.cursor else "Up", times=abs(target - dialog.cursor))
    time.sleep(SETTLE)
    now = approval_dialog(pane.screen())
    if now is None or now.text != dialog.text:
        return "the dialog closed meanwhile"
    if now.cursor != target:
        return f"couldn't move the cursor to {word}"
    pane.press("Enter")
    return None
