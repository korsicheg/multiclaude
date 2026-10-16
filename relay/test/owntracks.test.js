// OwnTracks' part of the relay (src/owntracks.js), mounted while env.ADDONS names locations.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import relay from "../src/index.js";
import { basicPassword, ownTracksEnvelope, ownTracksMessage, toOwnTracks } from "../src/owntracks.js";
import { fakeSocket, namespaceOf, newRelay } from "./fakes.js";

const KEY = "alice-owntracks-key";
const LOCATION = { _type: "location", lat: 37.98, lon: 23.73, acc: 12, tst: 1_791_530_000, tid: "al" };

function fromApp(body, { key = KEY } = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return new Request("https://relay.test/owntracks", { method: "POST", body: text,
    headers: { "content-type": "application/json", ...(key ? { authorization: basic("alice", key) } : {}) } });
}

function basic(user, password) {
  return `Basic ${btoa(`${user}:${password}`)}`;
}

function answerFromVm(id, answer, token = "vm-token") {
  return new Request(`https://relay.test/owntracks/${id}`, { method: "PUT", body: JSON.stringify(answer),
    headers: { authorization: `Bearer ${token}` } });
}

function ownTracksEnv(queue = newRelay(), addons = "siri,locations") {
  return { ADDONS: addons, RELAY_TOKEN: "vm-token", RELAY: namespaceOf(queue) };
}

/* The owntracks envelope the VM got last, once the relay has sent it. */
async function sentToVm(socket) {
  for (let tries = 0; tries < 100; tries++) {
    const envelope = socket.sent.findLast((sent) => sent.type === "owntracks");
    if (envelope) return envelope;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("the relay never sent the update to the VM");
}

test("the key is the password of a Basic header, whatever the user name", () => {
  assert.equal(basicPassword(new Headers({ authorization: basic("alice", "k:ey") })), "k:ey");
  assert.equal(basicPassword(new Headers({ authorization: basic("", "key") })), "key");
  assert.equal(basicPassword(new Headers({ authorization: "Bearer key" })), "");
  assert.equal(basicPassword(new Headers({ authorization: "Basic %%%" })), "");
  assert.equal(basicPassword(new Headers()), "");
});

test("a message is a JSON object that names its _type", () => {
  assert.deepEqual(ownTracksMessage(JSON.stringify(LOCATION)), LOCATION);
  assert.equal(ownTracksMessage(JSON.stringify([LOCATION])), null);
  assert.equal(ownTracksMessage(JSON.stringify({ lat: 1 })), null);
  assert.equal(ownTracksMessage("not json"), null);
  assert.equal(ownTracksMessage(JSON.stringify({ ...LOCATION, pad: "x".repeat(20 * 1024) })), null);
});

test("the envelope carries the key's SHA-256, never the key", async () => {
  const envelope = await ownTracksEnvelope("0badcafe", KEY, LOCATION, 1_791_530_001_000);
  assert.deepEqual(envelope, { envelope_id: "owntracks:0badcafe", type: "owntracks", payload: {
    id: "0badcafe", key_hash: createHash("sha256").update(KEY).digest("hex"), message: LOCATION,
    received: 1_791_530_001 } });
  assert.ok(!JSON.stringify(envelope).includes(KEY));
});

test("an update reaches the VM, and the app hears it's taken once the VM says so", async () => {
  const socket = fakeSocket();
  const env = ownTracksEnv(newRelay([socket]));
  const posted = relay.fetch(fromApp(LOCATION), env);
  const { payload } = await sentToVm(socket);

  assert.deepEqual(payload.message, LOCATION);
  assert.equal((await relay.fetch(answerFromVm(payload.id, { ok: true }), env)).status, 204);
  const reply = await posted;
  assert.equal(reply.status, 200);
  assert.deepEqual(await reply.json(), []);
});

test("a key the VM doesn't know is turned away", async () => {
  const socket = fakeSocket();
  const env = ownTracksEnv(newRelay([socket]));
  const posted = relay.fetch(fromApp(LOCATION), env);
  const { payload } = await sentToVm(socket);

  await relay.fetch(answerFromVm(payload.id, { error: "unknown OwnTracks key" }), env);
  assert.equal((await posted).status, 401);
});

test("with no answer in time the update counts as taken, since it stays queued", async () => {
  const reply = toOwnTracks({ late: true });
  assert.equal(reply.status, 200);
  assert.deepEqual(await reply.json(), []);
});

test("no key, nothing posted or a body that isn't a message is answered at once, without bothering the VM", async () => {
  const socket = fakeSocket();
  const env = ownTracksEnv(newRelay([socket]));

  assert.equal((await relay.fetch(fromApp(LOCATION, { key: null }), env)).status, 401);
  const empty = await relay.fetch(fromApp(""), env);
  assert.equal(empty.status, 200);
  assert.deepEqual(await empty.json(), []);
  assert.equal((await relay.fetch(fromApp({ lat: 1 }), env)).status, 400);
  assert.deepEqual(socket.sent, []);
});

test("only the VM may answer, and only a request id", async () => {
  const env = ownTracksEnv();
  assert.equal((await relay.fetch(answerFromVm("0badcafe", { ok: true }, "guess"), env)).status, 401);
  assert.equal((await relay.fetch(answerFromVm("../panel", { ok: true }), env)).status, 404);
  assert.equal((await relay.fetch(answerFromVm("NOTHEX00", { ok: true }), env)).status, 400);
});

test("while the locations add-in is off, OwnTracks' routes aren't there", async () => {
  const socket = fakeSocket();
  const env = ownTracksEnv(newRelay([socket]), "siri");
  assert.equal((await relay.fetch(fromApp(LOCATION), env)).status, 404);
  assert.equal((await relay.fetch(answerFromVm("0badcafe", { ok: true }), env)).status, 404);
  assert.deepEqual(socket.sent, []);
});
