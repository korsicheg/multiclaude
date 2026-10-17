// Reminders' part of the relay (src/reminders.js), mounted while env.ADDONS names reminders.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import relay from "../src/index.js";
import { remindersEnvelope, toShortcut } from "../src/reminders.js";
import { fakeSocket, namespaceOf, newRelay } from "./fakes.js";

const KEY = "alice-reminders-key";
const SENT = JSON.stringify({ reminders: [{ list: "Groceries", title: "Milk" }] });
const ADD = [{ id: "a1b2c3", list: "Groceries", title: "Bread", notes: "", due: "" }];

function fromShortcut(body, { key = KEY } = {}) {
  return new Request("https://relay.test/reminders", { method: "POST", body,
    headers: { "content-type": "application/json", ...(key ? { authorization: `Bearer ${key}` } : {}) } });
}

function answerFromVm(id, answer, token = "vm-token") {
  return new Request(`https://relay.test/reminders/${id}`, { method: "PUT", body: JSON.stringify(answer),
    headers: { authorization: `Bearer ${token}` } });
}

function remindersEnv(queue = newRelay(), addons = "siri,reminders") {
  return { ADDONS: addons, RELAY_TOKEN: "vm-token", RELAY: namespaceOf(queue) };
}

/* The reminders envelope the VM got last, once the relay has sent it. */
async function sentToVm(socket) {
  for (let tries = 0; tries < 100; tries++) {
    const envelope = socket.sent.findLast((sent) => sent.type === "reminders");
    if (envelope) return envelope;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("the relay never sent the reminders to the VM");
}

test("the envelope carries what the shortcut sent as it came, and the key's SHA-256, never the key", async () => {
  const envelope = await remindersEnvelope("0badcafe", KEY, SENT, 1_791_530_001_000);
  assert.deepEqual(envelope, { envelope_id: "reminders:0badcafe", type: "reminders", payload: {
    id: "0badcafe", key_hash: createHash("sha256").update(KEY).digest("hex"), sent: SENT, received: 1_791_530_001 } });
  assert.ok(!JSON.stringify(envelope).includes(KEY));
});

test("a sync reaches the VM, and the shortcut gets the reminders the VM says to add", async () => {
  const socket = fakeSocket();
  const env = remindersEnv(newRelay([socket]));
  const posted = relay.fetch(fromShortcut(SENT), env);
  const { payload } = await sentToVm(socket);

  assert.equal(payload.sent, SENT);
  assert.equal((await relay.fetch(answerFromVm(payload.id, { add: ADD }), env)).status, 204);
  const reply = await posted;
  assert.equal(reply.status, 200);
  assert.deepEqual(await reply.json(), { add: ADD });
});

test("plain text is passed on too, for the VM to read", async () => {
  const socket = fakeSocket();
  const env = remindersEnv(newRelay([socket]));
  const posted = relay.fetch(fromShortcut("Milk\nBread"), env);
  const { payload } = await sentToVm(socket);

  assert.equal(payload.sent, "Milk\nBread");
  await relay.fetch(answerFromVm(payload.id, { add: [] }), env);
  assert.equal((await posted).status, 200);
});

test("a key the VM doesn't know is turned away", async () => {
  const socket = fakeSocket();
  const env = remindersEnv(newRelay([socket]));
  const posted = relay.fetch(fromShortcut(SENT), env);
  const { payload } = await sentToVm(socket);

  await relay.fetch(answerFromVm(payload.id, { error: "unknown reminders key" }), env);
  const reply = await posted;
  assert.equal(reply.status, 401);
  assert.deepEqual(await reply.json(), { error: "unknown reminders key" });
});

test("with no answer in time there's nothing to add, and the sync stays queued", async () => {
  const reply = toShortcut({ late: true });
  assert.equal(reply.status, 200);
  assert.deepEqual(await reply.json(), { add: [] });
});

test("an answer without a list of reminders adds nothing", async () => {
  assert.deepEqual(await toShortcut({ answer: { add: "Bread" } }).json(), { add: [] });
  assert.deepEqual(await toShortcut({ answer: {} }).json(), { add: [] });
});

test("no key, nothing sent or too much is answered at once, without bothering the VM", async () => {
  const socket = fakeSocket();
  const env = remindersEnv(newRelay([socket]));

  assert.equal((await relay.fetch(fromShortcut(SENT, { key: null }), env)).status, 401);
  assert.equal((await relay.fetch(fromShortcut(" \n"), env)).status, 400);
  assert.equal((await relay.fetch(fromShortcut("x".repeat(129 * 1024)), env)).status, 413);
  assert.deepEqual(socket.sent, []);
});

test("only the VM may answer, and only a request id", async () => {
  const env = remindersEnv();
  assert.equal((await relay.fetch(answerFromVm("0badcafe", { add: [] }, "guess"), env)).status, 401);
  assert.equal((await relay.fetch(answerFromVm("../panel", { add: [] }), env)).status, 404);
  assert.equal((await relay.fetch(answerFromVm("NOTHEX00", { add: [] }), env)).status, 400);
});

test("while the reminders add-in is off, its routes aren't there", async () => {
  const socket = fakeSocket();
  const env = remindersEnv(newRelay([socket]), "siri,locations");
  assert.equal((await relay.fetch(fromShortcut(SENT), env)).status, 404);
  assert.equal((await relay.fetch(answerFromVm("0badcafe", { add: [] }), env)).status, 404);
  assert.deepEqual(socket.sent, []);
});
