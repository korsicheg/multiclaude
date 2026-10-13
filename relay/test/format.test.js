// How the control panel's page words times, money, sizes and the job runs of its Activity
// (public/app/format.js).
import assert from "node:assert/strict";
import { test } from "node:test";

import { duration, moment, money, outcomeOf, runFacts, size, tallyText } from "../public/app/format.js";

const NOW = Date.UTC(2026, 9, 13, 12, 0);  // Tue 13 Oct 2026, 15:00 in Athens
const at = (utc) => Date.parse(utc) / 1000;
const ATHENS = "Europe/Athens";

test("a run says when it started in the person's timezone, how it went, what it cost and how long it took", () => {
  const run = { started: at("2026-10-13T05:00:00Z"), finished: at("2026-10-13T05:04:10Z"), outcome: "sent", cost: 0.123 };
  assert.equal(runFacts(run, ATHENS, NOW), "today 08:00 · Sent · $0.12 · 4 min");
});

test("a run that took under a minute or has no cost leaves them out", () => {
  const run = { started: at("2026-10-12T18:00:00Z"), finished: at("2026-10-12T18:00:20Z"), outcome: "nothing", cost: null };
  assert.equal(runFacts(run, ATHENS, NOW), "yesterday 21:00 · Nothing to tell");
});

test("each outcome has its word and its dot", () => {
  assert.deepEqual(outcomeOf({ outcome: "sent" }), { label: "Sent", dot: "ok" });
  assert.deepEqual(outcomeOf({ outcome: "failed" }), { label: "Failed", dot: "failed" });
  assert.deepEqual(outcomeOf({ outcome: "skipped" }), { label: "Skipped, the run before was still going", dot: "warn" });
  assert.deepEqual(outcomeOf({ outcome: "new-one" }), { label: "new-one", dot: "" });
});

test("a tally counts runs, failures and cost, leaving out what's nought", () => {
  assert.equal(tallyText({ runs: 3, failed: 1, cost: 0.42 }), "3 runs · 1 failed · $0.42");
  assert.equal(tallyText({ runs: 1, failed: 0, cost: 0.004 }), "1 run · <$0.01");
  assert.equal(tallyText({ runs: 2, failed: 0, cost: 0 }), "2 runs");
  assert.equal(tallyText({ runs: 0, failed: 0, cost: 0 }), "No runs");
});

test("money is in dollars and cents", () => {
  assert.equal(money(1.5), "$1.50");
  assert.equal(money(0), "$0.00");
});

test("a moment further off says its day and date", () => {
  assert.equal(moment(at("2026-10-14T06:00:00Z"), ATHENS, NOW), "tomorrow 09:00");
  assert.equal(moment(at("2026-10-18T07:00:00Z"), ATHENS, NOW), "Sun 18 Oct, 10:00");
  assert.equal(moment(at("2026-10-13T22:30:00Z"), ATHENS, NOW), "tomorrow 01:30");  // still the 13th in UTC
});

test("a duration reads in minutes, hours and days", () => {
  assert.equal(duration(240), "4 min");
  assert.equal(duration(3 * 3600 + 600), "3 h 10 min");
  assert.equal(duration(26 * 3600), "1 d 2 h");
});

test("a size reads in MB, then in GB with one decimal while it has one, as the VM's alerts say it", () => {
  assert.equal(size(513.4), "513 MB");
  assert.equal(size(1500), "1.5 GB");
  assert.equal(size(8192), "8 GB");
  assert.equal(size(98_100), "95.8 GB");
  assert.equal(size(102_400), "100 GB");
});
