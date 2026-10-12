"""/model and /effort: show the setting, or switch it for this session only (Claude Code's s),
and with "default" also save it in the person's workspace settings for restarts and jobs."""
import re
import subprocess

from multiclaude import bot, jsonfile, paths
from multiclaude.log import log
from multiclaude.router import claude_ui
from multiclaude.router.usage import model_and_effort, snapshot

SETTING_KEYS = {"model": "model", "effort": "effortLevel"}
MODELS = ["opus", "sonnet", "haiku", "fable"]  # listed by a bare /model
SAVABLE_EFFORTS = ["low", "medium", "high", "xhigh"]  # Claude Code ignores max in settings files
SETTINGS_LOCK_WAIT = 5
SWITCHED = ("Set model to ", "Set effort level to ")


def current(user, setting):
    """What /model or /effort with no argument shows."""
    model, effort = model_and_effort(snapshot(user) or {})
    if setting == "model":
        now = (f"🧠 Model: {model}" + (f", {effort} effort" if effort else "")) if model else "🧠 Model: not known yet"
        names, label, example = MODELS, "Models", MODELS[0]
    else:
        now = (f"⚙️ Effort: {effort}" + (f" on {model}" if model else "")) if effort else "⚙️ Effort: not known yet"
        names, label, example = claude_ui.EFFORTS, "Levels", "high"
    saved = (workspace_settings(user) or {}).get(SETTING_KEYS[setting])
    if not saved:
        shared = jsonfile.read_json(paths.USER_SETTINGS, {}).get("model") if setting == "model" else None
        saved = f"the shared default ({shared})" if shared else "the shared default"
    return (f"{now}\nAfter a restart: {saved}\n\n"
            f"/{setting} {example}: this session only\n"
            f"/{setting} {example} default: also after restarts\n\n"
            f"{label}: {', '.join(names)}")


def switch(user, setting, target, keep, quiet=False):
    """Switch it in the assistant's terminal and tell the person how it went. quiet: putting a
    session-only switch back after an update restart, so only a failure is reported."""
    try:
        result, warned = user.switch(setting, target)
    except (subprocess.SubprocessError, OSError) as e:
        result, warned = f"the keystrokes failed ({e})", False
    log(f"{user.name}: /{setting} {target}: {result}")
    if not result.startswith(SWITCHED):
        problem = result.replace("`", "")
        user.say(f"⚠️ {bot.name()} restarted for a Claude Code update and couldn't switch its "
                 f"{setting} back to {target}: {problem}" if quiet
                 else f"⚠️ Couldn't switch {setting}: {problem}")
        return
    problem = save_default(user, setting, target) if keep else None
    if keep and not problem:
        user.session_switches.pop(setting, None)
    else:
        user.session_switches[setting] = target  # put back after a restart
    if not quiet:
        user.say(switched_text(setting, target, result, keep, problem, warned))


def switched_text(setting, target, result, keep, problem, warned):
    if setting == "model":
        shown = re.search(r"`([^`]+)`", result)
        text = f"✅ Model: {shown[1] if shown else target}"
    else:
        text = f"✅ Effort: {result.split()[4]}"
    if not keep or problem:
        text += " (this session only)" + (f". Not saved as your default: {problem}" if problem else "")
    else:
        text += ". Also saved as your default after restarts"
    if warned:
        text += "\n\nYour next reply will be slower, since it re-reads the whole conversation."
    return text


def workspace_settings(user):
    """The person's workspace .claude/settings.json, {} if there is none, None if it's unreadable."""
    try:
        return jsonfile.read_object(paths.workspace_settings(user.name))
    except (jsonfile.NotAnObject, OSError):
        return None


def save_default(user, setting, value):
    """Save a model/effort in the person's workspace settings, which their restarts and jobs
    start with; model "default" removes it. Returns a problem, or None."""
    if setting == "effort" and value not in SAVABLE_EFFORTS:
        return f"Claude Code keeps {value} for one session only"
    path = paths.workspace_settings(user.name)
    try:
        with jsonfile.locked(paths.SETTINGS_LOCK, wait=SETTINGS_LOCK_WAIT):
            settings = workspace_settings(user)
            if settings is None:
                return f"{path} isn't valid JSON, so I left it alone"
            if setting == "model" and value == "default":
                settings.pop("model", None)
            else:
                settings[SETTING_KEYS[setting]] = value
            jsonfile.write_json(path, settings)
    except TimeoutError:
        return "your settings file is busy (plugins are being shared right now); try again in a minute"
    except OSError as e:
        log(f"{user.name}: can't save {path}: {e}")
        return f"couldn't write {path}"
    return None
