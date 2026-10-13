/*
 * The control panel's sparklines: a day of a share (CPU busy, memory used, the fullest disk) under
 * its bar on the Machine section. app.js draws the SVG with them; they're kept apart from it so the
 * relay's tests check them (test/sparkline.test.js).
 */

/* The SVG path of a line through values, percentages with null where nothing was sampled, across
 * width × height with 0% at the bottom: each run of values between gaps a stroke of its own, a lone
 * value a dot (with round line caps). */
export function sparklinePath(values, width, height) {
  const step = width / Math.max(1, values.length - 1);
  return runs(values).map((run) => run.map(({ index, value }, n) =>
    `${n ? "L" : "M"}${rounded(index * step)},${rounded(heightOf(value, height))}`).join("")
    + (run.length === 1 ? "h0" : "")).join("");
}

/* How far down a share sits in a chart that tall: 100% at the top, 0% at the bottom. */
export function heightOf(percent, height) {
  return height * (1 - Math.min(100, Math.max(0, percent)) / 100);
}

/* The highest value sampled, or null when none was. */
export function peak(values) {
  const sampled = values.filter((value) => value != null);
  return sampled.length ? Math.max(...sampled) : null;
}

/* Which of count steps a point that far across (0 to 1) is on. */
export function stepAt(fraction, count) {
  return Math.min(count - 1, Math.max(0, Math.round(fraction * (count - 1))));
}

/* When step index of a day ({until, step} in Unix seconds, the VM's history.last_day) ends. */
export function stepEnd(day, index, count) {
  return day.until - (count - 1 - index) * day.step;
}

/* The runs of values between nulls, each value with its index. */
function runs(values) {
  const found = [];
  values.forEach((value, index) => {
    if (value == null) return;
    const last = found.at(-1);
    if (last && last.at(-1).index === index - 1) last.push({ index, value });
    else found.push([{ index, value }]);
  });
  return found;
}

function rounded(number) {
  return Math.round(number * 100) / 100;
}
