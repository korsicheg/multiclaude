/*
 * The control panel, a Telegram Mini App. It shows the snapshot the VM last put on the relay, which
 * the VM keeps current by itself, and sends each tap to the VM through the relay's queue
 * (src/panel.js; the VM's side is lib/multiclaude/router/panel.py). Opening it is one read. After a
 * tap it reads again, and the relay holds that read until the VM puts the snapshot with the answer.
 */
import { duration, moment, outcomeOf, runFacts, size, tallyText } from "./format.js";
import { heightOf, peak, sparklinePath, stepAt, stepEnd } from "./sparkline.js";

const tg = window.Telegram?.WebApp;

const ANSWER_WITHIN = 45000;     // ms before saying the VM hasn't answered a tap
const FOLLOW_FOR = 2 * 60e3;     // ms after a tap the page keeps reading changes: a switch, a server check
const SWITCH_WITHIN = 10 * 60e3; // ms a model or effort switch shows as asked for, while the assistant finishes its turn
const TURN_WITHIN = 2 * 60e3;    // ms a plugin or skill switch shows as asked for, while its command runs
const SERVERS_FRESH = 10 * 60;   // s: older server checks are done again when the panel opens
const TOAST_FOR = 3500;
const DISPLAY_MAX = 40;
const RUNS_FIRST = 10;           // job runs listed before "Show older runs"
const MODEL_NAMES = { opus: "Opus", sonnet: "Sonnet", haiku: "Haiku", fable: "Fable" };
const KEEP_KEY = "keep-switches";  // this phone's Keep after restarts switch, in localStorage
const DEFAULT_QUIET_HOURS = "00:00-09:00";
const UNSIGNED_NOTICE = "Telegram didn't say who opened this page, so it can't show your settings. "
  + "Open it with the Panel button or /config in your chat with the bot.";
const NAMELESS_BOT = "the bot";  // until the first snapshot says the bot's name
const SAMPLE_STALE = 15 * 60;    // s: a machine sample this old means sampling, the router or the VM has stopped
const SPARK_WIDTH = 300;         // the sparklines' own units: the SVG stretches to the row
const SPARK_HEIGHT = 36;
const SVG = "http://www.w3.org/2000/svg";
const HALF_HOURS = Array.from({ length: 48 }, (_, i) => `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"}`);

let snapshot = null;
let version = 0;             // the relay's version of that snapshot
let keep = storedKeep();     // the Keep after restarts switch
const waiting = new Map();   // tap id -> {action, args, since}, oldest first: taps the VM hasn't answered
const switching = {};        // "model" | "effort" -> {target, since}: asked for, not in effect yet
const turning = {};          // "plugin:<id>" | "skill:<name>" -> {on, since}: asked for, not in the snapshot yet
const opened = new Set();    // the plugins, skills and job runs shown opened, kept as the list is drawn again
let extensionsTab = "plugins";
let olderRunsShown = false;
let followUntil = 0;         // keep reading changes until then
let following = false;
let toastTimer = null;

class Unsigned extends Error {}

start();

async function start() {
  if (!tg?.initData) {
    showNotice(`${UNSIGNED_NOTICE}\n\n${launchDetails()}`);
    showStatus("");
    return;
  }
  tg.ready();
  tg.expand();
  if (tg.isVersionAtLeast("6.1")) {
    tg.setHeaderColor("secondary_bg_color");
    tg.setBackgroundColor("secondary_bg_color");
  }
  document.addEventListener("visibilitychange", () => document.hidden || read().catch(failed));
  offerHomeScreen();
  try {
    await read();
  } catch (error) {
    failed(error);
  }
  if (!snapshot || serversStale()) tap("refresh");
}

/* What the bot is called, as the VM's snapshot says. */
function botName() {
  return snapshot?.bot || NAMELESS_BOT;
}

function capitalized(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function serversStale() {
  return !snapshot.mcp.checked || now() - snapshot.mcp.checked > SERVERS_FRESH;
}

/* --- talking to the relay --- */

async function relay(path, body = {}) {
  const response = await fetch(`/app/${path}`, {
    method: "POST",
    headers: { authorization: `tma ${tg.initData}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 401) throw new Unsigned();
  if (!response.ok) throw new Error(`the relay answered ${response.status}`);
  return response.status === 202 ? null : response.json();
}

/* The latest snapshot; with after, the relay answers once there's one newer than that version. */
async function read(after = 0) {
  const latest = await relay("state", { after });
  if (latest.snapshot) [snapshot, version] = [latest.snapshot, latest.version];
  settle();
  render();
}

async function tap(action, args = {}) {
  const id = crypto.randomUUID();
  waiting.set(id, { action, args, since: Date.now() });
  render();
  try {
    await relay("action", { id, action, args });
  } catch (error) {
    waiting.delete(id);
    render();
    failed(error);
    return;
  }
  followUntil = Date.now() + FOLLOW_FOR;
  follow();
}

/* Read each new snapshot while something is on its way: a tap's answer, a switch the assistant makes
 * once it finishes its turn, a server check. One read at a time, each waiting at the relay. */
async function follow() {
  if (following) return;
  following = true;
  try {
    while (Date.now() < followUntil
      && (waiting.size || Object.keys(switching).length || Object.keys(turning).length || snapshot?.mcp?.checking)) {
      await read(version || 1);  // before any snapshot, 1 waits for the first
    }
  } catch (error) {
    failed(error);
  } finally {
    following = false;
  }
}

/* The VM answers taps in order and keeps only the latest answer, so it settles every tap sent
 * before it too. */
function settle() {
  const answer = snapshot?.answer;
  if (answer && waiting.has(answer.id)) {
    for (const id of waiting.keys()) {
      waiting.delete(id);
      if (id === answer.id) break;
    }
    if (answer.text) toast(answer.text, answer.ok);
    tg.HapticFeedback?.notificationOccurred(answer.ok ? "success" : "error");
  }
  for (const [id, sent] of waiting) {
    if (Date.now() - sent.since > ANSWER_WITHIN) {
      waiting.delete(id);
      toast("The VM hasn't answered yet, maybe it's offline. It's done once the VM is back.", false);
    }
  }
}

function failed(error) {
  if (error instanceof Unsigned) {
    showNotice("Telegram's signature on this panel didn't check out, or it's over a day old. Close the panel and open it again.");
  } else {
    showStatus("Can't reach the relay. Check your connection.");
  }
}

/* What a value will be once the VM has the taps still waiting: what the latest tap that sets it
 * says (pick gives undefined for a tap that doesn't), else what the snapshot says. */
function expected(actual, pick) {
  let value = actual;
  for (const sent of waiting.values()) value = pick(sent) ?? value;
  return value;
}

function settingPick(field) {
  return (sent) => (sent.action === "setting" && sent.args.field === field ? sent.args.value : undefined);
}

function pausePick(job) {
  return (sent) => (sent.action === "job" && sent.args.id === job.id && sent.args.command !== "delete"
    ? sent.args.command === "pause" : undefined);
}

/* --- the page --- */

function render() {
  if (!snapshot) return;
  showStatus(statusLine());
  section("claude", "Claude", claudeRows(), switchingNote());
  section("usage", "Usage", usageRows(), "");
  machineSections();
  section("jobs", "Scheduled jobs", jobRows(), jobsNote());
  if (snapshot.activity) section("activity", "Activity", activityRows(), activityNote());
  section("mcp", "MCP servers", serverRows(), serversNote());
  section("extensions", "Plugins & skills", extensionRows(), extensionsNote());
  section("profile", "Profile", profileRows(), profileNote());
  if (addonOn("quiet-hours")) section("quiet", "Quiet hours", quietRows(), quietNote());
  else document.getElementById("quiet")?.remove();
}

/* Whether the owner has an add-in on: its settings show only then. */
function addonOn(name) {
  return (snapshot.addons || []).includes(name);
}

function statusLine() {
  return waiting.size ? "Updating…" : "";
}

/* A section with its rows, left alone while something in it has the focus (a name being typed). */
function section(id, title, rows, footnote) {
  let node = document.getElementById(id);
  if (!node) {
    node = el("section", { id }, el("h2", {}, title), el("div", { className: "card" }), el("p", { className: "footnote" }));
    document.getElementById("sections").append(node);
  }
  const [, card, foot] = node.children;
  if (!card.contains(document.activeElement)) card.replaceChildren(...rows);
  foot.textContent = footnote;
}

function claudeRows() {
  const claude = snapshot.claude;
  const model = shownSwitch("model", claude.current_model);
  const effort = shownSwitch("effort", claude.effort);
  return [
    el("div", { className: "row stack" }, el("span", {}, "Model"),
      segmented(claude.models, model, (m) => askSwitch("model", m, model), modelName)),
    el("div", { className: "row stack" }, el("span", {}, "Effort"),
      segmented(claude.efforts, effort, (e) => askSwitch("effort", e, effort), (e) => e)),
    el("div", { className: "row" },
      el("span", { className: "label" }, "Keep after restarts",
        el("span", { className: "sub" }, "off: like /model opus · on: like /model opus default")),
      toggle(keep, keepAfterRestarts)),
    addonOn("model-fallback") ? restoreModelRow() : null,
    el("div", { className: "row" }, el("span", { className: "note" }, nowLine(claude))),
  ].filter(Boolean);
}

function restoreModelRow() {
  return el("div", { className: "row" },
    el("span", { className: "label" }, "Back to my model", el("span", { className: "sub" }, "after a safety filter switches it")),
    toggle(Boolean(expected(snapshot.person.restore_model, settingPick("restore_model"))), (on) => setting("restore_model", on)));
}

function nowLine(claude) {
  const now = claude.model ? `${claude.model}${claude.effort ? `, ${claude.effort} effort` : ""}` : "not known yet";
  return `Now ${now}. After a restart: ${afterRestart(claude)}.`;
}

/* Their own saved model and effort, else the VM's default, else Claude Code's own. */
function afterRestart(claude) {
  const model = claude.saved_model ? modelName(claude.saved_model)
    : `Default (${claude.shared_model ? modelName(claude.shared_model) : "recommended"})`;
  const effort = claude.saved_effort || claude.shared_effort;
  return `${model}, ${effort ? `${effort} effort` : "default effort"}`;
}

function modelName(model) {
  return MODEL_NAMES[model] || model;
}

/* A tap on what's already chosen saves it as the default, with Keep on; without, there's nothing to do. */
function askSwitch(setting, target, chosen) {
  if (target === chosen && !keep) return;
  switching[setting] = { target, since: Date.now() };
  tg.HapticFeedback?.selectionChanged();
  tap(setting, { target, keep });
}

function storedKeep() {
  try {
    return localStorage.getItem(KEEP_KEY) === "true";
  } catch {
    return false;
  }
}

function keepAfterRestarts(on) {
  keep = on;
  try {
    localStorage.setItem(KEEP_KEY, String(on));
  } catch {
    // storage is off: it holds until the panel closes
  }
}

/* A switch shows as asked for until it's in effect: the assistant switches once it finishes its turn. */
function shownSwitch(setting, actual) {
  const asked = switching[setting];
  if (asked && (asked.target === actual || Date.now() - asked.since > SWITCH_WITHIN)) delete switching[setting];
  return switching[setting]?.target ?? actual;
}

function switchingNote() {
  const pending = Object.entries(switching).map(([setting, { target }]) => `${setting} to ${target}`);
  if (pending.length) return `Switching ${pending.join(" and ")} once ${botName()} finishes what it's doing.`;
  return "Set the switch first, then tap a model or effort.";
}

function usageRows() {
  const usage = snapshot.usage;
  const rows = [];
  if (usage.context) {
    rows.push(meter("Context", usage.context.percent, `${tokens(usage.context.used)} of ${tokens(usage.context.size)}`));
  }
  for (const limit of usage.plan) {
    rows.push(meter(`${limit.label} limit`, limit.used, limit.resets ? `resets in ${duration(limit.resets - now())}` : ""));
  }
  if (usage.cost != null) {
    rows.push(el("div", { className: "row" }, el("span", { className: "label" }, "This conversation"),
      el("span", { className: "value" }, `$${usage.cost.toFixed(2)}`)));
  }
  if (!rows.length) rows.push(noteRow(`Nothing yet: it shows after ${botName()}'s next reply.`));
  return rows;
}

/* --- the machine, for the owner only: the VM's latest sample, its alerts and the last day --- */

function machineSections() {
  const machine = snapshot.machine;
  if (machine) section("machine", "Machine", machineRows(machine), machineNote(machine.sample));
  else document.getElementById("machine")?.remove();
  if (machine?.sample) section("memory", "Using the most memory", memoryRows(machine.sample.top), memoryNote());
  else document.getElementById("memory")?.remove();
}

function machineRows({ sample, day, limits }) {
  if (!sample) return [noteRow("No samples yet: cron takes one every 5 minutes, once assistant install has added its line.")];
  const { cpu, memory, swap, disks } = sample;
  const fullest = disks.reduce((most, disk) => (disk.percent > most.percent ? disk : most), disks[0]);
  const charted = (share) => ({ day, values: day[share], limit: limits[share] });
  return [
    ...sample.alerts.map(alertRow),
    cpu.percent == null ? noteRow("CPU: measured from the next sample on.")
      : chartedMeter("CPU", cpu.percent, `load ${cpu.load[0].toFixed(2)} · ${cpu.cores} cores`, charted("cpu")),
    chartedMeter("Memory", memory.percent, `${size(memory.total_mb - memory.available_mb)} of ${size(memory.total_mb)}`,
      charted("ram")),
    swap.total_mb ? meter("Swap", swap.percent, `${size(swap.used_mb)} of ${size(swap.total_mb)}`) : null,
    ...disks.map((disk) => chartedMeter(`Disk ${disk.mount}`, disk.percent, `${size(disk.free_mb)} free of ${size(disk.total_mb)}`,
      disk === fullest ? charted("disk") : null)),
    runningRow("Router", sample.router),
    ...sample.sessions.map((session) => runningRow(`${session.display}'s assistant`, session.running)),
    el("div", { className: "row" }, el("span", { className: "label" }, "Up for"),
      el("span", { className: "value" }, duration(sample.uptime))),
  ].filter(Boolean);
}

function alertRow(alert) {
  return el("div", { className: "row" }, el("span", { className: `dot ${alert.level === "critical" ? "failed" : "warn"}` }),
    el("span", { className: "label" }, alert.text,
      el("span", { className: "sub" }, `since ${moment(alert.since, snapshot.person.timezone)}`)));
}

function runningRow(label, running) {
  return el("div", { className: "row" }, el("span", { className: `dot ${running ? "ok" : "failed"}` }),
    el("span", { className: "label" }, label), el("span", { className: "value" }, running ? "running" : "not running"));
}

/* A share's bar, and under it its last day when the VM charts it: {day, values, limit}. */
function chartedMeter(label, percent, detail, chart) {
  const row = meter(label, percent, detail);
  if (chart) row.append(...sparkline(chart));
  return row;
}

/* The day of a share as a line, with a dashed line where it counts as high, and under it its highest;
 * a finger on it reads the share at that time instead. */
function sparkline({ day, values, limit }) {
  const highest = peak(values);
  const summary = `Last 24 hours${highest == null ? "" : `, highest ${highest}%`}`;
  const caption = el("span", { className: "sub spark-caption" }, summary);
  const limitAt = heightOf(limit, SPARK_HEIGHT);
  const chart = svgEl("svg", { class: "spark", viewBox: `0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`, preserveAspectRatio: "none",
    role: "img", "aria-label": summary },
  svgEl("line", { class: "limit", x1: 0, x2: SPARK_WIDTH, y1: limitAt, y2: limitAt }),
  svgEl("path", { d: sparklinePath(values, SPARK_WIDTH, SPARK_HEIGHT) }));
  const readAt = (event) => {
    const box = chart.getBoundingClientRect();
    const index = stepAt((event.clientX - box.left) / box.width, values.length);
    const value = values[index];
    caption.textContent = `${moment(stepEnd(day, index, values.length), snapshot.person.timezone)} · `
      + (value == null ? "no sample" : `highest ${value}%`);
  };
  chart.addEventListener("pointerdown", readAt);
  chart.addEventListener("pointermove", readAt);
  chart.addEventListener("pointerleave", () => (caption.textContent = summary));
  return [chart, caption];
}

function machineNote(sample) {
  if (!sample) return "";
  const age = now() - sample.time;
  if (age > SAMPLE_STALE) return `⚠️ The last sample is ${duration(age)} old: the VM, its sampling or the router has stopped.`;
  return `Sampled ${ago(sample.time)}. You hear on Telegram when something runs high or stops, and when it's fine again.`;
}

function memoryRows(top) {
  return top.map(({ name, mb, count }) => el("div", { className: "row" },
    el("span", { className: "label" }, name, count > 1 ? el("span", { className: "sub" }, `${count} processes`) : null),
    el("span", { className: "value" }, size(mb))));
}

function memoryNote() {
  return "Each program's processes together, with the memory they share split among them.";
}

function meter(label, percent, detail) {
  const level = percent >= 90 ? "bad" : percent >= 70 ? "warn" : "";
  return el("div", { className: "row stack meter" },
    el("div", { className: "line" }, el("span", {}, label),
      el("span", { className: "value" }, `${Math.round(percent ?? 0)}%${detail ? ` · ${detail}` : ""}`)),
    el("div", { className: `bar ${level}` }, el("span", { style: `width: ${Math.min(100, Math.max(0, percent ?? 0))}%` })));
}

function jobRows() {
  if (!snapshot.jobs.length) return [noteRow(`None yet. Ask ${botName()} for one, such as “every morning at 8, …”.`)];
  return snapshot.jobs.map((job) => {
    const paused = expected(job.paused, pausePick(job));
    const when = paused ? "Paused" : job.next ? `Next: ${moment(job.next, snapshot.person.timezone)}` : "No next run";
    return el("div", { className: "row" },
      el("details", { className: "job" },
        el("summary", {}, el("span", {}, job.title), el("span", { className: "sub" }, when)),
        job.detail ? el("p", { className: "detail" }, job.detail) : null,
        el("button", { className: "link destructive", onclick: () => deleteJob(job) }, "Delete this job…")),
      toggle(!paused, (on) => tap("job", { command: on ? "resume" : "pause", id: job.id })));
  });
}

function jobsNote() {
  return snapshot.jobs.length ? "Tap a job for more. The switch pauses and resumes it." : "";
}

function deleteJob(job) {
  tg.showConfirm(`Delete “${job.title}”? ${capitalized(botName())} removes it from the crontab and from your jobs list.`, (ok) => {
    if (ok) tap("job", { command: "delete", id: job.id });
  });
}

/* --- activity: what the jobs did, newest first, each run under its job's name --- */

function activityRows() {
  const { runs, today, week } = snapshot.activity;
  if (!runs.length) return [noteRow("Nothing yet: each run of your scheduled jobs shows here.")];
  const shown = olderRunsShown ? runs : runs.slice(0, RUNS_FIRST);
  const older = runs.length - shown.length;
  return [
    el("div", { className: "row" }, tally("Today", today), tally("Last 7 days", week)),
    ...shown.map(runRow),
    older ? el("button", { className: "row more", onclick: showOlderRuns }, `Show ${older} older ${older === 1 ? "run" : "runs"}`) : null,
  ].filter(Boolean);
}

function tally(label, counts) {
  return el("span", { className: "label" }, label, el("span", { className: "sub" }, tallyText(counts)));
}

/* A run's job, when, how it went and what it cost; the start of its answer, all of it once opened. */
function runRow(run) {
  const key = `run:${run.started}:${run.title}`;
  const title = el("span", {}, run.title);
  const facts = el("span", { className: "sub" }, runFacts(run, snapshot.person.timezone));
  const body = run.preview
    ? el("details", { className: "job", open: opened.has(key), ontoggle: (event) => remember(key, event.target.open) },
      el("summary", {}, title, facts, el("span", { className: "preview" }, run.preview)))
    : el("span", { className: "label" }, title, facts);
  return el("div", { className: "row run" }, el("span", { className: `dot ${outcomeOf(run).dot}` }), body);
}

function showOlderRuns() {
  olderRunsShown = true;
  render();
}

function activityNote() {
  return snapshot.activity.runs.length ? "What your scheduled jobs did, newest first. Tap a run to read more of it." : "";
}

function serverRows() {
  const { servers, checking } = snapshot.mcp;
  if (!servers.length) return [noteRow(checking ? "Checking your servers…" : "None checked yet.")];
  return servers.map((server) => el("button", { className: "row", onclick: () => serverMenu(server) },
    el("span", { className: `dot ${server.health}` }),
    el("span", { className: "label" }, shortName(server.name)),
    el("span", { className: "value" }, server.state),
    el("span", { className: "chevron" }, "›")));
}

function serversNote() {
  const { checked, checking } = snapshot.mcp;
  if (checking) return "Checking…";
  return checked ? `Checked ${ago(checked)}. Tap a server to reconnect, sign in or turn it off.` : "";
}

function serverMenu(server) {
  const buttons = server.health === "off"
    ? [{ id: "enable", type: "default", text: "Turn on" }]
    : [server.health === "auth" ? { id: "login", type: "default", text: "Sign in" } : { id: "reconnect", type: "default", text: "Reconnect" },
      { id: "disable", type: "destructive", text: "Turn off" }];
  const from = server.own ? "" : " It comes with a plugin.";
  tg.showPopup({ title: shortName(server.name), message: `${server.state}.${from}`, buttons: [...buttons, { type: "cancel" }] },
    (id) => {
      if (["enable", "disable", "login", "reconnect"].includes(id)) tap("mcp", { command: id, server: server.name });
    });
}

function shortName(name) {
  return name.replace(/^plugin:[^:]+:/, "");
}

/* --- plugins and skills: one list at a time, each switch the bot's own /plugin or /skills command --- */

function extensionRows() {
  const tabs = el("div", { className: "row tabs" }, segmented(["plugins", "skills"], extensionsTab, chooseTab, tabLabel));
  const items = snapshot[extensionsTab] ?? [];
  if (!items.length) {
    return [tabs, noteRow(extensionsTab === "plugins" ? "None yet. Add one in the chat with /plugin install."
      : `None of your own yet. Ask ${botName()} to write one.`)];
  }
  return [tabs, ...items.map(extensionsTab === "plugins" ? pluginRow : skillRow)];
}

function chooseTab(tab) {
  extensionsTab = tab;
  tg.HapticFeedback?.selectionChanged();
  render();
}

function tabLabel(tab) {
  return `${tab === "plugins" ? "Plugins" : "Skills"} · ${(snapshot[tab] ?? []).length}`;
}

function pluginRow(plugin) {
  const key = `plugin:${plugin.id}`;
  return extensionRow({
    key, name: plugin.name, chips: pluginTags(plugin), details: [plugin.description, brings(plugin)],
    on: shownTurn(key, plugin.on),
    change: (on) => turn(key, on, "plugin", { command: on ? "enable" : "disable", plugin: plugin.id }),
  });
}

function skillRow(skill) {
  const key = `skill:${skill.name}`;
  return extensionRow({
    key, name: skill.name, chips: skill.shared ? ["shared"] : [], details: [skill.description],
    on: shownTurn(key, skill.on),
    change: (on) => turn(key, on, "skill", { command: on ? "on" : "off", skill: skill.name }),
  });
}

/* A name with its tags, opened with a tap when there's more to say, and its switch. */
function extensionRow({ key, name, chips, details, on, change }) {
  const title = el("span", {}, name, ...chips.map((chip) => el("span", { className: "chip" }, chip)));
  const lines = details.filter(Boolean);
  const body = lines.length
    ? el("details", { className: "job", open: opened.has(key), ontoggle: (event) => remember(key, event.target.open) },
      el("summary", {}, title), ...lines.map((line) => el("p", { className: "detail" }, line)))
    : el("span", { className: "label" }, title);
  return el("div", { className: "row" }, body, toggle(on, change));
}

function remember(key, open) {
  if (open) opened.add(key);
  else opened.delete(key);
}

function pluginTags(plugin) {
  return [plugin.servers.length && "tools", plugin.skills.length && "skills", plugin.code.length && "code",
    plugin.shared && "shared"].filter(Boolean);
}

/* "From acme-plugins · MCP server weather": where it comes from and what it brings. */
function brings(plugin) {
  const parts = [`From ${plugin.marketplace}`];
  if (plugin.servers.length) parts.push(named(plugin.servers, "MCP server"));
  if (plugin.skills.length) parts.push(plugin.skills.length > 3 ? `${plugin.skills.length} skills` : named(plugin.skills, "skill"));
  if (plugin.code.length) parts.push(`code checking (${plugin.code.join(", ")})`);
  if (plugin.shared) parts.push("shared with everyone");
  return parts.join(" · ");
}

function named(names, noun) {
  return `${noun}${names.length === 1 ? "" : "s"} ${names.join(", ")}`;
}

function turn(key, on, action, args) {
  turning[key] = { on, since: Date.now() };
  tap(action, args);
}

/* A plugin or skill switch shows as asked for until the snapshot has it: the command takes a few seconds. */
function shownTurn(key, actual) {
  const asked = turning[key];
  if (asked && (asked.on === actual || Date.now() - asked.since > TURN_WITHIN)) delete turning[key];
  return turning[key]?.on ?? actual;
}

function extensionsNote() {
  if (Object.keys(turning).length) return `${capitalized(botName())} confirms in the chat, then reloads its plugins and skills once it's idle.`;
  return extensionsTab === "plugins"
    ? "Tap one for what it does. The switch works like /plugin enable and /plugin disable. Install and update in the chat with /plugin."
    : "Your own skills and the shared ones; a plugin's skills come with it. The switch works like /skills on and /skills off.";
}

function profileRows() {
  const person = snapshot.person;
  const zone = expected(person.timezone, settingPick("timezone"));
  return [
    el("label", { className: "row" }, el("span", { className: "label" }, "Name"),
      el("input", { className: "field", value: person.display, maxLength: DISPLAY_MAX, enterKeyHint: "done",
        onchange: (event) => setting("display", event.target.value) })),
    pickerRow("Timezone", zoneLabel(zone), timezones(zone)),
  ];
}

function profileNote() {
  const used = `Used for the times ${botName()} shows you`;
  return addonOn("quiet-hours") ? `${used} and for your quiet hours.` : `${used}.`;
}

function quietHours() {
  return expected(snapshot.person.quiet_hours, settingPick("quiet_hours"));
}

function quietRows() {
  const hours = quietHours();
  const rows = [el("div", { className: "row" }, el("span", { className: "label" }, "Quiet hours"),
    toggle(Boolean(hours), (on) => setting("quiet_hours", on ? DEFAULT_QUIET_HOURS : "")))];
  if (hours) {
    const [start, end] = hours.split("-");
    rows.push(pickerRow("Starts", start, timeSelect(start, (time) => setting("quiet_hours", `${time}-${end}`))),
      pickerRow("Ends", end, timeSelect(end, (time) => setting("quiet_hours", `${start}-${time}`))));
  }
  return rows;
}

/* Whether they're in their quiet hours right now, as quiet_hours.py decides it: the hours may pass midnight. */
function quietNote() {
  const hours = quietHours();
  if (!hours) return "When it's on, messages arrive without a sound in those hours.";
  const [start, end] = hours.split("-");
  const time = new Date().toLocaleTimeString("en-GB", { timeZone: snapshot.person.timezone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const quiet = start <= end ? start <= time && time < end : time >= start || time < end;
  return quiet ? `🌙 Quiet now, until ${end}. Messages arrive without a sound.`
    : `Messages arrive without a sound from ${start} to ${end}.`;
}

function setting(field, value) {
  tap("setting", { field, value });
}

/* A row whose value opens the system's picker: the select covers the row, unseen. */
function pickerRow(label, shown, select) {
  select.classList.add("cover");
  return el("label", { className: "row picker" }, el("span", { className: "label" }, label),
    el("span", { className: "value" }, shown), el("span", { className: "chevron" }, "›"), select);
}

function timezones(current) {
  const names = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  const all = [...new Set([current, "UTC", ...names])].sort();
  return el("select", { onchange: (event) => setting("timezone", event.target.value) },
    ...all.map((zone) => el("option", { value: zone, selected: zone === current }, zone.replaceAll("_", " "))));
}

/* "Berlin · GMT+2" for Europe/Berlin. */
function zoneLabel(zone) {
  const city = zone.split("/").pop().replaceAll("_", " ");
  if (zone === "UTC") return zone;
  try {
    const parts = new Intl.DateTimeFormat("en-GB", { timeZone: zone, timeZoneName: "shortOffset" }).formatToParts(new Date());
    const offset = parts.find((part) => part.type === "timeZoneName")?.value;
    return offset ? `${city} · ${offset}` : city;
  } catch {
    return city;
  }
}

function timeSelect(current, change) {
  const times = [...new Set([...HALF_HOURS, current])].sort();
  return el("select", { onchange: (event) => change(event.target.value) },
    ...times.map((time) => el("option", { value: time, selected: time === current }, time)));
}

/* --- small parts --- */

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child != null && child !== false));
  return node;
}

function svgEl(tag, attributes, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

function segmented(options, chosen, choose, name) {
  return el("div", { className: "segmented" }, ...options.map((option) =>
    el("button", { className: option === chosen ? "on" : "", onclick: () => choose(option) }, name(option))));
}

function toggle(on, change) {
  return el("input", { type: "checkbox", className: "switch", checked: on, onchange: (event) => {
    tg.HapticFeedback?.selectionChanged();
    change(event.target.checked);
  } });
}

function noteRow(text) {
  return el("div", { className: "row" }, el("span", { className: "note" }, text));
}

function showStatus(text) {
  const status = document.getElementById("status");
  status.textContent = text;
  status.hidden = !text;
}

/* What Telegram passed the page: shown when it isn't signed, so a screenshot says which client and how. */
function launchDetails() {
  if (!tg) return "Telegram's script didn't load.";
  const addressSigned = /tgWebAppData=/.test(location.hash + location.search);
  return `Telegram ${tg.platform}, Mini Apps ${tg.version}; the address ${addressSigned ? "has" : "has no"} sign-in data.`;
}

function showNotice(text) {
  const notice = document.getElementById("notice");
  notice.textContent = text;
  notice.hidden = false;
}

function toast(text, ok) {
  const node = document.getElementById("toast");
  node.textContent = text;
  node.className = ok ? "toast" : "toast bad";
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), TOAST_FOR);
}

function offerHomeScreen() {
  if (!tg.isVersionAtLeast("8.0")) return;
  const button = document.getElementById("home-screen");
  button.onclick = () => tg.addToHomeScreen();
  tg.onEvent("homeScreenAdded", () => (button.hidden = true));
  tg.checkHomeScreenStatus((status) => (button.hidden = status !== "missed"));
}

function now() {
  return Date.now() / 1000;
}

function tokens(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

function ago(when) {
  const seconds = now() - when;
  return seconds < 60 ? "just now" : `${duration(seconds)} ago`;
}
