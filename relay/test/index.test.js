// The relay's core (src/index.js): the queue the VM collects from, and which routes are mounted.
// Nothing here reaches Cloudflare: the Durable Object's SQL storage is node:sqlite.
import assert from "node:assert/strict";
import { test } from "node:test";

import relay, { routesFor } from "../src/index.js";
import { fakeSocket, newRelay, queueStub } from "./fakes.js";

const NOW = 1_791_530_000_000;
const DAY = 24 * 60 * 60 * 1000;

const tap = (id) => ({ envelope_id: id, type: "panel", payload: { id } });

test("the queue is only for the VM, with its token", async () => {
  const env = { RELAY_TOKEN: "vm-token", RELAY: queueStub([]) };
  const reply = await relay.fetch(new Request("https://relay.test/queue", {
    headers: { upgrade: "websocket", authorization: "Bearer guess" } }), env);
  assert.equal(reply.status, 401);
});

test("a kept envelope goes to the connected VM once, even if it comes twice", () => {
  const vm = fakeSocket();
  const queue = newRelay([vm]);
  queue.keep(tap("T1"), NOW);
  queue.keep(tap("T1"), NOW);
  assert.deepEqual(vm.sent, [tap("T1")]);
});

test("a new connection gets hello and whatever the VM hasn't acknowledged, oldest first", () => {
  const queue = newRelay();
  queue.keep(tap("T1"), NOW);
  queue.keep(tap("T2"), NOW + 1);
  queue.keep(tap("T3"), NOW + 2);
  queue.webSocketMessage(fakeSocket(), JSON.stringify({ envelope_id: "T2" }));
  const vm = fakeSocket();
  queue.greet(vm);
  assert.deepEqual(vm.sent, [{ type: "hello" }, tap("T1"), tap("T3")]);
});

test("an envelope nobody collected for a day is dropped", () => {
  const queue = newRelay();
  queue.keep(tap("T1"), NOW);
  queue.keep(tap("T2"), NOW + DAY + 1);
  const vm = fakeSocket();
  queue.greet(vm);
  assert.deepEqual(vm.sent, [{ type: "hello" }, tap("T2")]);
});

test("a socket that's already gone doesn't lose the envelope", () => {
  const gone = { send() { throw new Error("closed"); } };
  const queue = newRelay([gone]);
  queue.keep(tap("T1"), NOW);
  const vm = fakeSocket();
  queue.greet(vm);
  assert.deepEqual(vm.sent, [{ type: "hello" }, tap("T1")]);
});

test("a request asking the VM gets its answer, or hears it's late, and the envelope stays queued", async () => {
  const queue = newRelay();
  const asked = queue.ask(tap("A1"), 60_000);
  assert.equal(queue.answer("A1", JSON.stringify({ reply: "done" })).status, 204);
  assert.deepEqual(await (await asked).json(), { answer: { reply: "done" } });

  assert.deepEqual(await (await queue.ask(tap("A2"), 5)).json(), { late: true });
  assert.equal(queue.answer("A2", JSON.stringify({ reply: "too late" })).status, 410);
  const vm = fakeSocket();
  queue.greet(vm);
  assert.deepEqual(vm.sent, [{ type: "hello" }, tap("A1"), tap("A2")]);
});

test("with no add-ins on, only the core's routes are there", () => {
  const paths = (env) => routesFor(env).map((route) => String(route.path));
  assert.deepEqual(paths({}), paths({ ADDONS: "" }));
  assert.deepEqual(paths({ ADDONS: "quiet-hours, no-such-thing" }), paths({}));
  assert.ok(paths({ ADDONS: "slack, siri" }).length > paths({}).length);
});

test("an address nothing has is not found", async () => {
  assert.equal((await relay.fetch(new Request("https://relay.test/nothing"), { RELAY: queueStub([]) })).status, 404);
});
