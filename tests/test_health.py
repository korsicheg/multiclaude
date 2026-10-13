"""Tests for the machine's health (lib/multiclaude/health): reading the machine from a fake /proc, the
week of samples kept, each alert rule going up and back to fine, and what the owner is told. The
control panel's Machine section is tested in test_panel.py, `assistant health` from cron in
test_assistant_cli.py, and the page's sparklines in relay/test/sparkline.test.js."""
import json
import os
import subprocess
from collections import namedtuple
from types import SimpleNamespace

import pytest

from conftest import FakeProc, fresh_multiclaude_in, read_json, write_json

T0 = 1_791_500_000  # a sample's time
FIVE_MINUTES = 300


@pytest.fixture
def health(tmp_path, monkeypatch):
    """The health package with every location in tmp_path, its /proc a fake one there."""
    monkeypatch.setenv("ASSISTANT_PROC", str(tmp_path / "proc"))
    fresh_multiclaude_in(tmp_path, monkeypatch)
    from multiclaude import paths, tmux
    from multiclaude.health import alerts, history, machine, sample, status
    return SimpleNamespace(alerts=alerts, history=history, machine=machine, sample=sample, status=status, paths=paths,
                           tmux=tmux, proc=FakeProc(tmp_path / "proc"))


# --- reading the machine ---

def test_cpu_busy_is_the_share_of_ticks_spent_working_between_two_readings(health):
    machine = health.machine
    before = machine.cpu_ticks("cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n")
    after = machine.cpu_ticks("cpu  400 0 150 1300 150 0 0 0 0 0\n")
    assert before == (150, 1000)  # waiting for the disk isn't busy
    assert machine.cpu_percent(before, after) == 40.0


def test_cpu_busy_isnt_known_across_a_restart_or_without_time_passing(health):
    machine = health.machine
    before = machine.CpuTicks(5000, 9000)
    assert machine.cpu_percent(before, machine.CpuTicks(100, 1000)) is None
    assert machine.cpu_percent(before, before) is None


def test_memory_used_is_what_memavailable_doesnt_count_as_available(health):
    fields = health.machine.meminfo("MemTotal:       8388608 kB\nMemFree:  100000 kB\nMemAvailable:    838861 kB\n"
                                    "SwapTotal: 4194304 kB\nSwapFree: 3145728 kB\nHugePages_Total:       0\n")
    assert health.machine.memory(fields) == {"total_mb": 8192, "available_mb": 819, "percent": 90.0}
    assert health.machine.swap(fields) == {"total_mb": 4096, "used_mb": 1024, "percent": 25.0}


def test_a_machine_without_swap_has_none_used(health):
    assert health.machine.swap({"MemTotal": 1000}) == {"total_mb": 0, "used_mb": 0, "percent": 0.0}


def test_disks_are_read_as_df_shows_them_each_file_system_once(health, tmp_path, monkeypatch):
    usage = namedtuple("usage", "total used free")
    mb = 1024 * 1024
    monkeypatch.setattr(health.machine.shutil, "disk_usage", lambda folder: usage(100_000 * mb, 80_000 * mb, 15_000 * mb))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    disks = health.machine.disks([tmp_path / "a", tmp_path / "b", tmp_path / "gone"])

    assert len(disks) == 1  # a and b are on the same file system; gone isn't there
    assert disks[0]["percent"] == 84.2  # of what's used and free to use, without root's reserve
    assert (disks[0]["used_mb"], disks[0]["free_mb"]) == (80_000, 15_000)
    assert os.path.ismount(disks[0]["mount"])


def test_the_top_memory_users_are_programs_named_so_the_owner_knows_whose_they_are(health):
    proc, paths = health.proc, health.paths
    alice = paths.workspace("alice")
    proc.process(10, "claude", 600, "claude", "--name", "alice", cwd=alice, pss_mb=500)
    proc.process(11, "claude", 400, "claude", "-p", "--output-format", "json", cwd=alice / "sub", pss_mb=300)
    proc.process(12, "claude", 300, "claude", "--channels", "plugin:telegram", cwd=paths.ROOT, pss_mb=250)
    for pid in (20, 21, 22):  # a browser's processes share most of what they hold
        proc.process(pid, "chrome", 500, "/opt/google/chrome/chrome", "--type=renderer", pss_mb=120)
    proc.process(23, "chrome_crashpad", 50, "/opt/google/chrome/chrome_crashpad_handler", pss_mb=10)
    proc.process(30, "python", 150, "/venv/bin/python", "-m", "dei_mcp", pss_mb=140)
    proc.process(31, "python3", 40, "python3", "-I", str(paths.BIN / "watcher.py"), pss_mb=35)
    proc.process(32, "agent-browser-l", 200, "/usr/lib/agent-browser-linux-x64", pss_mb=190)
    proc.process(40, "systemd-journal", 90, "/usr/lib/systemd/systemd-journald")  # root's: only its resident size
    proc.process(2, "kthreadd", 0)  # the kernel's own: no memory, no command line

    running = health.machine.processes()

    assert 2 not in running
    assert health.machine.top_memory(running) == [
        {"name": "claude (alice)", "mb": 500, "count": 1}, {"name": "Chrome", "mb": 370, "count": 4},
        {"name": "claude job (alice)", "mb": 300, "count": 1}, {"name": "claude (front desk)", "mb": 250, "count": 1},
        {"name": "agent-browser-linux-x64", "mb": 190, "count": 1}]
    named = {pid: health.machine.program(process) for pid, process in running.items()}
    assert (named[30], named[31], named[40]) == ("dei_mcp", "router", "systemd-journald")
    assert running[40].memory_mb == pytest.approx(90, abs=0.01)


def test_an_assistant_runs_while_claude_runs_in_its_tmux_session(health, monkeypatch):
    health.proc.process(4243, "claude", 300, "claude", ppid=4242)
    health.proc.process(5000, "bash", 5, "bash", ppid=5001)

    def tmux(*args, stdin=None):
        shells, target = {"=u-alice:": b"4242\n", "=u-bob:": b"5001\n"}, args[args.index("-t") + 1]
        if target not in shells:
            raise subprocess.CalledProcessError(1, "tmux")
        return subprocess.CompletedProcess(args, 0, shells[target], b"")

    monkeypatch.setattr(health.tmux, "run", tmux)
    running = health.machine.processes()
    person = lambda name: SimpleNamespace(pane=f"u-{name}")  # noqa: E731
    assert health.machine.assistant_running(person("alice"), running)
    assert not health.machine.assistant_running(person("bob"), running)  # its claude has exited, its shell waits
    assert not health.machine.assistant_running(person("carol"), running)  # no session at all


def test_the_router_runs_while_systemd_says_it_is_active(health, tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "systemctl"
    fake.parent.mkdir()
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    for status, running in ((0, True), (3, False)):
        fake.write_text(f"#!/bin/sh\n[ \"$*\" = 'is-active --quiet assistant-watcher' ] && exit {status}\nexit 9\n")
        fake.chmod(0o755)
        assert health.machine.router_running() is running


def test_a_sample_reads_the_whole_machine(health, monkeypatch):
    health.proc.machine(total_mb=8192, available_mb=2048, busy=600, idle=1400)
    monkeypatch.setattr(health.machine, "router_running", lambda: True)
    monkeypatch.setattr(health.machine, "disks", lambda folders: [{"mount": "/", "percent": 50.0}])

    taken = health.machine.sample([], since=health.machine.CpuTicks(100, 1000))

    assert taken["cpu"] == {"percent": 50.0, "load": [0.52, 0.40, 0.35], "cores": os.cpu_count()}
    assert taken["memory"]["percent"] == 75.0 and taken["uptime"] == 47663
    assert taken["ticks"] == [600, 2000] and taken["sessions"] == [] and taken["router"]


# --- the samples kept ---

def numbers(when, **shares):
    return {"time": when, "cpu": 10, "ram": 50, "swap": 0, "disk": 40, **shares}


def a_sample(when=T0, cpu=10.0, ram=50.0, swap=0.0, disks=(("/", 40.0),), router=True, assistants=(("alice", True),)):
    """A sample as machine.sample takes it, with these shares."""
    return {"time": when, "uptime": 3600, "cpu": {"percent": cpu, "load": [7.2, 6.8, 5.1], "cores": 4},
            "memory": {"total_mb": 8192, "available_mb": round(8192 * (100 - ram) / 100), "percent": ram},
            "swap": {"total_mb": 4096, "used_mb": round(40.96 * swap), "percent": swap},
            "disks": [{"mount": mount, "total_mb": 102_400, "used_mb": 1024 * full, "free_mb": 1024 * (100 - full),
                       "percent": full} for mount, full in disks],
            "top": [{"name": "Chrome", "mb": 1500, "count": 34}, {"name": "dei_mcp", "mb": 140, "count": 1}],
            "router": router, "sessions": [{"name": n, "display": n.title(), "running": up} for n, up in assistants],
            "ticks": [1, 2]}


def test_a_week_of_samples_is_kept(health):
    week = 7 * 24 * 3600
    write_json(health.history.LATEST, {})
    health.history.HISTORY.write_text(f"{json.dumps(numbers(T0 - week - 1, cpu=1))}\nnot json\n"
                                      f"{json.dumps(numbers(T0 - week + 1, cpu=2))}\n")

    health.history.keep(a_sample(T0, cpu=3.0))

    assert [point["cpu"] for point in health.history.points()] == [2, 3.0]
    assert health.history.points()[-1] == {"time": T0, "cpu": 3.0, "ram": 50.0, "swap": 0.0, "disk": 40.0}
    assert health.history.latest()["cpu"]["percent"] == 3.0


def test_the_last_day_is_each_quarter_hours_highest_with_gaps_where_nothing_was_sampled(health):
    day, step = 24 * 3600, 15 * 60
    start = T0 - day
    points = [numbers(start, cpu=99),  # just over a day before: left out
              numbers(start + 60, cpu=20, ram=None), numbers(start + 600, cpu=35.4),  # the first quarter hour
              numbers(start + 3 * step + 1, cpu=None, disk=81.6),
              numbers(T0, cpu=70)]  # the last one
    health.history.HISTORY.parent.mkdir(parents=True)
    health.history.HISTORY.write_text("".join(json.dumps(point) + "\n" for point in points))

    charted = health.history.last_day(T0)

    assert (charted["until"], charted["step"], len(charted["cpu"])) == (T0, step, 96)
    assert charted["cpu"][:4] == [35, None, None, None] and charted["cpu"][-1] == 70
    assert charted["ram"][0] == 50 and charted["disk"][3] == 82


# --- the alerts ---

def changes_over(health, *samples, settings=None):
    """What the owner is told over these samples, one list of texts for each."""
    settings = settings or health.alerts.settings()
    standings, told = {}, []
    for n, taken in enumerate(samples):
        standings, changes = health.alerts.check({**taken, "time": T0 + n * FIVE_MINUTES}, standings, settings)
        told.append([change.text for change in changes])
    return told


def test_memory_alerts_after_three_samples_in_a_row_and_only_once(health):
    told = changes_over(health, *(a_sample(ram=95.0) for _ in range(5)))
    assert [len(texts) for texts in told] == [0, 0, 1, 0, 0]
    assert told[2][0].startswith("⚠️ **Memory has been running low for 15 min**: 95% used, 410 MB of 8 GB available.")
    assert "- Chrome, 34 processes: 1.5 GB\n- dei\\_mcp: 140 MB" in told[2][0]


def test_a_sample_under_the_line_starts_the_count_again(health):
    told = changes_over(health, *(a_sample(ram=ram) for ram in (95.0, 95.0, 89.9, 95.0, 95.0)))
    assert told == [[]] * 5


def test_memory_clears_only_once_back_under_80_percent(health):
    told = changes_over(health, *(a_sample(ram=ram) for ram in (95.0, 95.0, 95.0, 85.0, 89.0, 79.0, 85.0)))
    assert [len(texts) for texts in told] == [0, 0, 1, 0, 0, 1, 0]
    assert told[5] == ["✅ **Memory is fine again**: 79% used, 1.7 GB available."]


def test_the_cpu_alerts_after_15_minutes_busy_with_the_load_and_the_top_memory_users(health):
    told = changes_over(health, *(a_sample(cpu=cpu) for cpu in (96.0, 92.0, 90.0, 75.0, 69.0)))
    assert [len(texts) for texts in told] == [0, 0, 1, 0, 1]
    assert told[2][0].startswith("🔥 **The CPU has been busy for 15 min**: 90% in the last 5 min, load 7.20, 6.80, "
                                 "5.10 on 4 cores. I'll say when it's under 70% again.\n\nUsing the most memory:\n- Chrome")
    assert told[4] == ["✅ **The CPU is calm again**: 69% busy in the last 5 min."]


def test_a_sample_whose_cpu_isnt_known_changes_nothing(health):
    told = changes_over(health, a_sample(cpu=95.0), a_sample(cpu=None), a_sample(cpu=95.0), a_sample(cpu=95.0))
    assert [len(texts) for texts in told] == [0, 0, 0, 1]  # the unknown one didn't count, nor break the streak


def test_swap_alerts_after_three_samples_over_half_full(health):
    told = changes_over(health, *(a_sample(swap=swap) for swap in (60.0, 60.0, 60.0, 45.0, 39.0)))
    assert [len(texts) for texts in told] == [0, 0, 1, 0, 1]
    assert told[2][0].startswith("⚠️ **Swap has been filling up for 15 min**: 60% used, 2.4 GB of 4 GB")


def test_a_disk_is_high_at_85_critical_at_95_and_says_so_only_going_up_or_back_to_fine(health):
    fullness = (84.0, 86.0, 96.0, 92.0, 88.0, 96.0, 81.0, 79.0)
    told = changes_over(health, *(a_sample(disks=(("/", full), ("/home", 10.0))) for full in fullness))
    assert [len(texts) for texts in told] == [0, 1, 1, 0, 0, 1, 0, 1]
    assert told[1] == ["💽 **The disk is filling up**: `/` is 86% full, 14 GB of 100 GB free. "
                       "I'll say when it's under 80% again."]
    assert told[2][0].startswith("🚨 **The disk is nearly full**: `/` is 96% full, only 4 GB of 100 GB free.")
    assert told[7] == ["✅ **Disk space is fine again**: `/` is 79% full, 21 GB free."]


def test_the_router_alerts_at_once_and_says_when_its_back(health):
    told = changes_over(health, a_sample(router=False), a_sample(router=False), a_sample(router=True))
    assert [len(texts) for texts in told] == [1, 0, 1]
    assert told[0][0].startswith("🛑 **The router is down**: `assistant-watcher` isn't running")
    assert told[2][0].startswith("✅ **The router is running again.**")


def test_an_assistant_alerts_once_it_has_been_missing_for_two_samples(health):
    missing = a_sample(assistants=(("alice", True), ("bob", False)))
    told = changes_over(health, missing, a_sample(assistants=(("alice", True), ("bob", True))), missing, missing)
    assert [len(texts) for texts in told] == [0, 0, 0, 1]
    assert told[3][0].startswith("⚠️ **Bob's assistant isn't running**: no Claude Code in its tmux session `u-bob` "
                                 "for 10 min, so Bob's messages wait.")


def test_the_owner_can_change_the_thresholds_in_config_json(health):
    write_json(health.paths.CONFIG, {"health": {"ram": {"at": 97, "samples": 1, "colour": 1}, "disk": {"at": "lots"},
                                                "cpu": "off"}})
    settings = health.alerts.settings()
    assert settings["ram"] == {"at": 97, "clear": 80, "samples": 1}
    assert settings["disk"]["at"] == 85 and settings["cpu"] == health.alerts.DEFAULTS["cpu"]
    told = changes_over(health, a_sample(ram=96.0), a_sample(ram=98.0), settings=settings)
    assert [len(texts) for texts in told] == [0, 1]


def test_the_alerts_standing_say_what_how_bad_and_since_when(health):
    settings = health.alerts.settings()
    standings = {}
    for n, full in enumerate((96.0, 96.0)):
        taken = a_sample(T0 + n * FIVE_MINUTES, disks=(("/", full),), router=False)
        standings, _ = health.alerts.check(taken, standings, settings)
    assert health.alerts.standing_alerts(taken, standings, settings) == [
        {"rule": "disk:/", "level": "critical", "since": T0, "text": "Disk / 96% full"},
        {"rule": "router", "level": "high", "since": T0, "text": "The router is down"}]


def test_where_the_alerts_stand_is_kept_between_samples(health):
    standings = {"ram": health.alerts.Standing(1, 0, T0), "cpu": health.alerts.Standing(0, 2, None)}
    health.alerts.save_standings(standings)
    assert health.alerts.load_standings() == standings
    write_json(health.alerts.STATE, {"ram": {"level": "high"}, "cpu": []})
    assert health.alerts.load_standings() == {"ram": health.alerts.Standing(), "cpu": health.alerts.Standing()}


# --- a run of `assistant health sample` ---

@pytest.fixture
def sampler(health, monkeypatch):
    """take() with the machine reading as each sample in health.readings says, and what the owner is told recorded."""
    health.readings, health.told, health.tell = [], [], lambda text: True

    def tell_owner(text):
        health.told.append(text)
        return health.tell(text)

    monkeypatch.setattr(health.machine, "sample", lambda people, since: health.readings.pop(0))
    monkeypatch.setattr(health.sample, "tell_owner", tell_owner)
    return health


def test_each_sample_is_kept_with_the_alerts_standing_and_the_owner_told_once(sampler):
    sampler.readings = [a_sample(T0 + n * FIVE_MINUTES, ram=95.0) for n in range(4)]
    for _ in range(4):
        sampler.sample.take()

    assert len(sampler.told) == 1 and "Memory has been running low" in sampler.told[0]
    latest = sampler.history.latest()
    assert latest["time"] == T0 + 3 * FIVE_MINUTES
    assert latest["alerts"] == [{"rule": "ram", "level": "high", "since": T0 + 2 * FIVE_MINUTES, "text": "Memory 95% used"}]
    assert len(sampler.history.points()) == 4


def test_an_alert_the_owner_couldnt_be_told_is_told_at_the_next_sample(sampler):
    sampler.readings = [a_sample(T0, router=False), a_sample(T0 + FIVE_MINUTES, router=False)]
    sampler.tell = lambda text: len(sampler.told) > 1
    sampler.sample.take()
    assert sampler.history.latest()["alerts"] == []

    sampler.sample.take()
    assert len(sampler.told) == 2 and sampler.told[0] == sampler.told[1]
    assert read_json(sampler.alerts.STATE)["router"]["level"] == 1


def test_a_sample_isnt_taken_while_the_one_before_still_is(sampler):
    from multiclaude import jsonfile
    sampler.readings = [a_sample()]
    with jsonfile.locked(sampler.sample.LOCK):
        sampler.sample.run()
    assert sampler.history.latest() is None and len(sampler.readings) == 1


def test_the_terminal_shows_the_machine_now_and_what_was_kept(sampler, monkeypatch):
    now = a_sample(T0 + 600, disks=(("/", 96.0),))
    monkeypatch.setattr(sampler.status, "CPU_WINDOW", 0)
    sampler.proc.machine()
    sampler.readings = [a_sample(T0, disks=(("/", 96.0),)), now]
    sampler.sample.take()

    text = sampler.status.report()

    assert "  CPU                      10%  load 7.20, 6.80, 5.10 on 4 cores" in text
    assert "  Disk /                   96%  96 GB of 100 GB used, 4 GB free" in text
    assert "  Alice's assistant       running" in text
    assert "\n    1.5 GB  Chrome, 34 processes\n" in text
    assert "Alerts at the last sample, 10 min ago:\n  critical: Disk / 96% full, since " in text
    assert text.endswith("The last day's highs: CPU 10%, memory 50%, disk 96%")
