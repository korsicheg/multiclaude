"""How the machine's numbers read, in the owner's alerts and on the terminal."""
from multiclaude.telegram import markdown_literal

MB_PER_GB = 1024


def size(mb):
    """513 MB, 4.1 GB, 8 GB, 96 GB."""
    if mb < 1000:
        return f"{round(mb)} MB"
    gb = mb / MB_PER_GB
    return f"{gb:.1f}".removesuffix(".0") + " GB" if gb < 100 else f"{round(gb)} GB"


def share(percent):
    """92%, or a dash when it isn't known (the CPU, before a second reading)."""
    return "–" if percent is None else f"{round(percent)}%"


def load(cpu):
    """0.62, 0.61, 0.55 on 6 cores: the load over 1, 5 and 15 minutes, which is high near the core count."""
    return f"{', '.join(f'{value:.2f}' for value in cpu['load'])} on {cpu['cores']} cores"


def program(row):
    """A top memory user: claude (ada), or Chrome, 34 processes."""
    return row["name"] if row["count"] == 1 else f"{row['name']}, {row['count']} processes"


def top_list(top):
    """The top memory users as a Markdown list."""
    return "\n".join(f"- {markdown_literal(program(row))}: {size(row['mb'])}" for row in top)


def duration(seconds):
    """4 min, 3 h 10 min, 2 d 5 h."""
    minutes = max(0, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h {minutes % 60} min"
    return f"{hours // 24} d {hours % 24} h"
