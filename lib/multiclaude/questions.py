"""Questions with buttons, kept so the router can answer a tap on one (router/asks.py): `tg ask`
keeps its own (and add-ins' apps theirs), and the router its own, such as the Cancel under a
waiting /clear."""
import time

from multiclaude import jsonfile

KEPT_FOR = 30 * 86400


def option_buttons(options):
    """Telegram's inline keyboard for a question: one button per option, each carrying "ask:<n>"."""
    return {"inline_keyboard": [[{"text": o, "callback_data": f"ask:{n}"}] for n, o in enumerate(options)]}


def save(folder, message_id, question, options, kind=None):
    """Keep a question in folder (paths.ASKS/<chat>) by its message id, and drop the month-old
    ones. kind marks a question the router asked itself; a tap on it isn't typed to the assistant."""
    jsonfile.write_json(folder / f"{message_id}.json",
                        {"question": question, "options": options, **({"kind": kind} if kind else {})})
    cutoff = time.time() - KEPT_FOR
    for old in folder.glob("*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
