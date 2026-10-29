"""Reading Claude Code's pickers and dialogs off the screen and answering them."""
import re
import time
from collections import Counter
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
# and ask, and so does a tool behind an `ask` permission rule: a full-width rule, the tool and
# what it wants to do, the reason, a countdown to the automatic deny, "Do you want to proceed?"
# and numbered options with a ❯ at the cursor.
PROCEED = "Do you want to proceed?"
# A side panel without a border puts its own text on the rule's line.
RULE_RE = re.compile(r"^─{20,}(\s|$)")
# Lines Claude Code starts with these belong to the conversation above a dialog, never to it.
CONVERSATION_MARKS = ("●", "⎿", "✻", "❯")
OPTION_RE = re.compile(r"^\s*(❯)?\s*\d+\.\s+(.+?)\s*$")
COUNTDOWN_RE = re.compile(r"automatically deny this request in (\d+):(\d{2})")

# Claude Code can show a panel on the right of the screen ("No changes this session"), its
# border a │ down one column. Beside it, every line of the conversation ends in the border and
# the panel's text, so it is cut off before a dialog is read.
PANEL_BORDER = "│"
PANEL_MIN_COLUMN = 40     # nearer the left, a │ belongs to what's on screen, not to a panel
PANEL_MIN_SHARE = 0.5     # of the lines wide enough to reach the border's column
WIDE_CHAR_SLACK = 8       # an emoji is one character but two columns: the border moves left
# Once files have changed, the panel shows their diff and has no border: a dialog's rules then
# stop short of it, and the diff runs on beside every line of the dialog.
MAIN_RULE_RE = re.compile(r"^([─╌]{20,})")


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


def without_side_panel(screen):
    """The screen with the panel on its right, if one is open, cut off."""
    lines = screen.splitlines()
    border = side_panel_border(lines)
    if border is not None:
        return "\n".join(cut_at_border(line, border) for line in lines)
    edge = borderless_panel_edge(lines)
    if edge is not None:
        return "\n".join(line[:edge].rstrip() for line in lines)
    return screen


def side_panel_border(lines):
    """The column of the side panel's border, or None when no panel is open."""
    columns = Counter(i for line in lines for i, char in enumerate(line)
                      if char == PANEL_BORDER and i >= PANEL_MIN_COLUMN)
    if not columns:
        return None
    column, lines_with_it = columns.most_common(1)[0]
    lines_reaching_it = sum(len(line) > column for line in lines)
    return column if lines_with_it >= PANEL_MIN_SHARE * lines_reaching_it else None


def cut_at_border(line, border):
    at = line.rfind(PANEL_BORDER, max(border - WIDE_CHAR_SLACK, 0), border + 1)
    return line[:at].rstrip() if at != -1 else line


def borderless_panel_edge(lines):
    """Where a panel without a border starts, or None: the column the rules starting the
    screen's lines stop at, when text runs on past it."""
    widths = [len(m[1]) for line in lines if (m := MAIN_RULE_RE.match(line))]
    if not widths:
        return None
    edge = max(widths)
    return edge if any(line[edge:].strip() for line in lines) else None


def approval_dialog(screen):
    """The approval dialog on screen, or None."""
    lines = without_side_panel(screen).splitlines()
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
    top = dialog_top(lines, ask)
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


def dialog_top(lines, ask):
    """The line above the dialog: its rule, or failing that the conversation's last line, so the
    dialog's text never takes in the conversation, whose status dots blink."""
    above = range(ask)
    rule = max((i for i in above if RULE_RE.match(lines[i].strip())), default=None)
    if rule is not None:
        return rule
    return max((i for i in above if lines[i].lstrip().startswith(CONVERSATION_MARKS)), default=-1)


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


# Claude Code's message box sits between the screen's last two full-width rules, its first line
# after a ❯. Every message the router types starts with its header in brackets: "[Telegram from …".
PROMPT = "❯"


def message_box_lines(screen):
    """The lines between the screen's last two rules, where the message box sits; [] without two rules."""
    lines = without_side_panel(screen).splitlines()
    rules = [i for i, line in enumerate(lines) if RULE_RE.match(line.strip())]
    return lines[rules[-2] + 1:rules[-1]] if len(rules) >= 2 else []


def message_box(screen):
    """The text in the message box, "" when it's empty."""
    box = "\n".join(line.strip() for line in message_box_lines(screen))
    return box.removeprefix(PROMPT).strip()


def message_box_shown(screen):
    """Whether the message box is on screen to type into. A dialog, the router's to relay or one
    it can't read, takes its place, and keys typed there would answer it."""
    box = message_box_lines(screen)
    return bool(box) and box[0].startswith(PROMPT)


def holds_typed_message(screen):
    """Whether a message the router typed is back in the message box: Escape before any reply
    takes it out of the conversation and puts it there."""
    return message_box(screen).startswith("[")
