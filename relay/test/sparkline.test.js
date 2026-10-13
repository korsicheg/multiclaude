// The control panel's sparklines: a day of a share on the Machine section (public/app/sparkline.js).
import assert from "node:assert/strict";
import { test } from "node:test";

import { heightOf, peak, sparklinePath, stepAt, stepEnd } from "../public/app/sparkline.js";

test("a day of shares is a line from left to right, 0% at the bottom and 100% at the top", () => {
  assert.equal(sparklinePath([0, 50, 100], 100, 20), "M0,20L50,10L100,0");
});

test("a gap where nothing was sampled breaks the line, and a lone value is a dot", () => {
  assert.equal(sparklinePath([10, 20, null, 30, null, null, 40, 50], 70, 10), "M0,9L10,8M30,7h0M60,6L70,5");
  assert.equal(sparklinePath([null, null], 10, 10), "");
});

test("a share past either end stays in the chart", () => {
  assert.equal(heightOf(120, 36), 0);
  assert.equal(heightOf(-5, 36), 36);
  assert.equal(heightOf(75, 36), 9);
});

test("the highest value leaves out the gaps, and a day without samples has none", () => {
  assert.equal(peak([null, 12, 85, 40]), 85);
  assert.equal(peak([null, null]), null);
});

test("a finger on the chart reads the step under it, which ends at its time", () => {
  assert.equal(stepAt(0, 96), 0);
  assert.equal(stepAt(0.5, 96), 48);
  assert.equal(stepAt(1.2, 96), 95);
  const day = { until: 1_791_500_000, step: 900 };
  assert.equal(stepEnd(day, 95, 96), day.until);
  assert.equal(stepEnd(day, 0, 96), day.until - 95 * 900);
});
