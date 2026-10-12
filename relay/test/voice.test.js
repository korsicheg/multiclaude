// Siri's part of the relay (src/voice.js), mounted while env.ADDONS names siri.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import relay from "../src/index.js";
import { LATE_REPLY, bearerKey, spokenWords, toSiri, voiceEnvelope } from "../src/voice.js";
import { fakeSocket, namespaceOf, newRelay } from "./fakes.js";

const KEY = "alice-voice-key";

function fromShortcut(body, { key = KEY } = {}) {
  return new Request("https://relay.test/voice", { method: "POST", body: JSON.stringify(body),
    headers: { "content-type": "application/json", ...(key ? { authorization: `Bearer ${key}` } : {}) } });
}

function answerFromVm(id, answer, token = "vm-token") {
  return new Request(`https://relay.test/voice/${id}`, { method: "PUT", body: JSON.stringify(answer),
    headers: { authorization: `Bearer ${token}` } });
}

function voiceEnv(queue = newRelay(), addons = "slack,siri") {
  return { ADDONS: addons, RELAY_TOKEN: "vm-token", RELAY: namespaceOf(queue) };
}

/* The voice envelope the VM got last, once the relay has sent it. */
async function sentToVm(socket) {
  for (let tries = 0; tries < 100; tries++) {
    const envelope = socket.sent.findLast((sent) => sent.type === "voice");
    if (envelope) return envelope;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("the relay never sent the words to the VM");
}

test("the voice key comes from a bearer header", () => {
  assert.equal(bearerKey(new Headers({ authorization: "Bearer abc" })), "abc");
  assert.equal(bearerKey(new Headers({ authorization: "bearer abc" })), "abc");
  assert.equal(bearerKey(new Headers({ authorization: "Basic abc" })), "");
  assert.equal(bearerKey(new Headers()), "");
});

test("the words come from a JSON body or a plain text one", () => {
  assert.equal(spokenWords(JSON.stringify({ text: " Weather tomorrow? " })), "Weather tomorrow?");
  assert.equal(spokenWords("Weather tomorrow?"), "Weather tomorrow?");
  assert.equal(spokenWords(JSON.stringify({ text: "  " })), "");
  assert.equal(spokenWords("x".repeat(20 * 1024)), "");
});

test("the envelope carries the key's SHA-256, never the key", async () => {
  const envelope = await voiceEnvelope("0badcafe", KEY, "hi", 1_791_530_000_000);
  assert.deepEqual(envelope, { envelope_id: "voice:0badcafe", type: "voice", payload: {
    id: "0badcafe", key_hash: createHash("sha256").update(KEY).digest("hex"), text: "hi", said: 1_791_530_000 } });
  assert.ok(!JSON.stringify(envelope).includes(KEY));
});

test("spoken words reach the VM and the shortcut gets the answer the VM puts back", async () => {
  const socket = fakeSocket();
  const env = voiceEnv(newRelay([socket]));
  const asked = relay.fetch(fromShortcut({ text: "Weather tomorrow?" }), env);
  const { payload } = await sentToVm(socket);

  assert.equal(payload.text, "Weather tomorrow?");
  assert.equal((await relay.fetch(answerFromVm(payload.id, { reply: "It's sunny." }), env)).status, 204);
  const reply = await asked;
  assert.equal(reply.status, 200);
  assert.deepEqual(await reply.json(), { reply: "It's sunny." });
});

test("a key the VM doesn't know is turned away", async () => {
  const socket = fakeSocket();
  const env = voiceEnv(newRelay([socket]));
  const asked = relay.fetch(fromShortcut({ text: "hi" }), env);
  const { payload } = await sentToVm(socket);

  await relay.fetch(answerFromVm(payload.id, { error: "unknown voice key" }), env);
  assert.equal((await asked).status, 401);
});

test("with no answer in time the shortcut hears it will come on Telegram", async () => {
  assert.deepEqual(await toSiri({ late: true }).json(), { reply: LATE_REPLY });
});

test("no key or no words is answered at once, without bothering the VM", async () => {
  const socket = fakeSocket();
  const env = voiceEnv(newRelay([socket]));

  assert.equal((await relay.fetch(fromShortcut({ text: "hi" }, { key: null }), env)).status, 401);
  assert.equal((await relay.fetch(fromShortcut({ text: " " }), env)).status, 400);
  assert.deepEqual(socket.sent, []);
});

test("only the VM may answer, and only a request id", async () => {
  const env = voiceEnv();
  assert.equal((await relay.fetch(answerFromVm("0badcafe", { reply: "x" }, "guess"), env)).status, 401);
  assert.equal((await relay.fetch(answerFromVm("../panel", { reply: "x" }), env)).status, 404);
  assert.equal((await relay.fetch(answerFromVm("NOTHEX00", { reply: "x" }), env)).status, 400);
});

test("while the siri add-in is off, Siri's routes aren't there", async () => {
  const socket = fakeSocket();
  const env = voiceEnv(newRelay([socket]), "slack");
  assert.equal((await relay.fetch(fromShortcut({ text: "hi" }), env)).status, 404);
  assert.equal((await relay.fetch(answerFromVm("0badcafe", { reply: "x" }), env)).status, 404);
  assert.deepEqual(socket.sent, []);
});
