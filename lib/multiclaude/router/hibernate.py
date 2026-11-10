"""Putting idle assistants to sleep, for a machine short of memory: with
"hibernate_after_minutes" in config.json, an assistant that has been idle that long has its whole
tmux session ended (ending claude alone wouldn't do: its run loop starts it again), and its
worker starts it again, resuming its conversation, for the next work queued for it (work.wake).
See asleep.py for how the rest of the system knows."""
from multiclaude import config
from multiclaude.router.work import Hibernate

CHECK_EVERY = 60  # how often to look for assistants that have been idle long enough


class Hibernator:
    def __init__(self):
        self.last_check = 0.0

    def tick(self, now, sessions):
        """sessions: every person's assistant. Its worker puts an idle one to sleep, after
        checking again."""
        if now - self.last_check < CHECK_EVERY:
            return
        self.last_check = now
        after = config.hibernate_after()
        if not after:
            return
        for session in sessions:
            proc = None if session.asleep() else session.claude_proc()
            if proc and session.can_hibernate(now, proc, after):
                session.enqueue(Hibernate(now, pid=proc[0], after=after))
