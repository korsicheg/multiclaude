/*
 * How the control panel puts times, money and the job runs of its Activity into words. app.js draws
 * the page with them; they're kept apart from it so the relay's tests check them (test/format.test.js).
 */
const DAY = 864e5;  // ms

/* What each outcome of a job run is called, and the colour of its dot (app.css). */
const OUTCOMES = {
  sent: { label: "Sent", dot: "ok" },
  nothing: { label: "Nothing to tell", dot: "" },
  failed: { label: "Failed", dot: "failed" },
  skipped: { label: "Skipped, the run before was still going", dot: "warn" },
};

export function outcomeOf(run) {
  return OUTCOMES[run.outcome] ?? { label: run.outcome, dot: "" };
}

/* "today 08:00 · Sent · $0.12 · 4 min": when a run started, how it went, what it cost and how long it took. */
export function runFacts(run, timeZone, now = Date.now()) {
  const took = run.finished - run.started;
  return [moment(run.started, timeZone, now), outcomeOf(run).label, run.cost != null ? money(run.cost) : null,
    took >= 60 ? duration(took) : null].filter(Boolean).join(" · ");
}

/* "3 runs · 1 failed · $0.42": how many runs ran in a while, how many failed and what they cost. */
export function tallyText({ runs, failed, cost }) {
  if (!runs) return "No runs";
  return [`${runs} ${runs === 1 ? "run" : "runs"}`, failed ? `${failed} failed` : null, cost > 0 ? money(cost) : null]
    .filter(Boolean).join(" · ");
}

export function money(usd) {
  return usd > 0 && usd < 0.01 ? "<$0.01" : `$${usd.toFixed(2)}`;
}

export function duration(seconds) {
  const minutes = Math.max(0, Math.round(seconds / 60));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ${minutes % 60} min`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

/* A moment (Unix seconds) in timeZone: "today 21:00", "yesterday 08:00", "tomorrow 09:00", "Sun 18 Oct, 10:00". */
export function moment(when, timeZone, now = Date.now()) {
  const date = new Date(when * 1000);
  const day = (d) => d.toLocaleDateString("en-CA", { timeZone });
  const time = date.toLocaleTimeString("en-GB", { timeZone, hour: "2-digit", minute: "2-digit" });
  const nearby = { [day(new Date(now))]: "today", [day(new Date(now - DAY))]: "yesterday", [day(new Date(now + DAY))]: "tomorrow" };
  const named = nearby[day(date)];
  if (named) return `${named} ${time}`;
  return `${date.toLocaleDateString("en-GB", { timeZone, weekday: "short", day: "numeric", month: "short" })}, ${time}`;
}
