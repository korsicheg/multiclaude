// The control panel's part of the relay (src/panel.js and its routes in src/index.js). Init data
// is signed here as Telegram signs it.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import relay from "../src/index.js";
import { tapEnvelope, webAppUser } from "../src/panel.js";
import { fakeSocket, namespaceOf, newRelay } from "./fakes.js";

const BOT_TOKEN = "123:bot-token";
const WEBAPP_KEY = createHmac("sha256", "WebAppData").update(BOT_TOKEN).digest("hex");
const NOW = 1_791_530_000_000;
const ALICE = 100;

function signed({ id = ALICE, token = BOT_TOKEN, authDate = NOW / 1000 } = {}) {
  const fields = { auth_date: String(authDate), query_id: "AAH", signature: "sig",
    user: JSON.stringify({ id, first_name: "Alice" }) };
  const checked = Object.keys(fields).sort().map((key) => `${key}=${fields[key]}`).join("\n");
  const secret = createHmac("sha256", "WebAppData").update(token).digest();
  return new URLSearchParams({ ...fields, hash: createHmac("sha256", secret).update(checked).digest("hex") }).toString();
}

function fromPage(path, initData, body = {}) {
  return new Request(`https://relay.test${path}`, { method: "POST", body: JSON.stringify(body),
    headers: { authorization: `tma ${initData}`, "content-type": "application/json" } });
}

function panelEnv(queue = newRelay()) {
  return { TELEGRAM_WEBAPP_KEY: WEBAPP_KEY, RELAY_TOKEN: "vm-token", RELAY: namespaceOf(queue) };
}

test("init data Telegram signed for this bot names who opened the panel", async () => {
  assert.equal((await webAppUser(signed(), WEBAPP_KEY, NOW)).id, ALICE);
});

test("init data signed for another bot, a day old, changed, or missing names no one", async () => {
  assert.equal(await webAppUser(signed({ token: "999:another-bot" }), WEBAPP_KEY, NOW), null);
  assert.equal(await webAppUser(signed({ authDate: NOW / 1000 - 25 * 3600 }), WEBAPP_KEY, NOW), null);
  assert.equal(await webAppUser(signed().replace("Alice", "Eve"), WEBAPP_KEY, NOW), null);
  assert.equal(await webAppUser("", WEBAPP_KEY, NOW), null);
  assert.equal(await webAppUser(signed(), undefined, NOW), null);
});

test("a tap becomes a panel envelope that carries the init data for the VM to check again", () => {
  const envelope = tapEnvelope("init", JSON.stringify({ id: "t1", action: "job", args: { command: "pause", id: "abc" } }));
  assert.deepEqual(envelope, { envelope_id: "panel:t1", type: "panel",
    payload: { init_data: "init", id: "t1", action: "job", args: { command: "pause", id: "abc" } } });
  assert.equal(tapEnvelope("init", "not json"), null);
  assert.equal(tapEnvelope("init", JSON.stringify({ id: "t 1", action: "job" })), null);
  assert.equal(tapEnvelope("init", JSON.stringify({ id: "t1", action: "x".repeat(5000) })), null);
});

test("before the VM has put a snapshot, the page gets null", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const reply = await relay.fetch(fromPage("/app/state", signed()), panelEnv());
  assert.deepEqual(await reply.json(), { snapshot: null, version: 0 });
});

test("the page gets the snapshot the VM put last for whoever opened it, and no one else's", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const env = panelEnv();
  for (const [user, display] of [[ALICE, "Alice"], [101, "Bob"], [ALICE, "Alice again"]]) {
    const put = await relay.fetch(new Request(`https://relay.test/panel/${user}`, { method: "PUT",
      body: JSON.stringify({ person: { display } }), headers: { authorization: "Bearer vm-token" } }), env);
    assert.equal(put.status, 204);
  }
  const reply = await relay.fetch(fromPage("/app/state", signed()), env);
  assert.deepEqual(await reply.json(), { snapshot: { person: { display: "Alice again" } }, version: NOW });
});

test("a read for a newer snapshot waits until the VM puts one", async () => {
  const queue = newRelay();
  queue.keepPanel("100", '{"n":1}', 1000);
  const reading = queue.panel("100", 1000, 60_000);
  queue.keepPanel("101", '{"n":"theirs"}', 1500);
  queue.keepPanel("100", '{"n":2}', 2000);
  assert.deepEqual(await (await reading).json(), { snapshot: { n: 2 }, version: 2000 });
  assert.equal(queue.waiting.size, 0);
});

test("a read for a newer snapshot answers at once when there is one", async () => {
  const queue = newRelay();
  queue.keepPanel("100", '{"n":2}', 2000);
  assert.deepEqual(await (await queue.panel("100", 1000)).json(), { snapshot: { n: 2 }, version: 2000 });
});

test("a read that waited in vain gets the snapshot there is", async () => {
  const queue = newRelay();
  queue.keepPanel("100", '{"n":1}', 1000);
  assert.deepEqual(await (await queue.panel("100", 1000, 5)).json(), { snapshot: { n: 1 }, version: 1000 });
  assert.equal(queue.waiting.size, 0);
});

test("only the VM, with its token, puts snapshots", async () => {
  const reply = await relay.fetch(new Request("https://relay.test/panel/100", { method: "PUT", body: "{}",
    headers: { authorization: "Bearer guess" } }), panelEnv());
  assert.equal(reply.status, 401);
});

test("a tap is queued for the VM and goes to it at once when it's connected", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const vm = fakeSocket();
  const initData = signed();
  const reply = await relay.fetch(fromPage("/app/action", initData, { id: "t1", action: "refresh" }), panelEnv(newRelay([vm])));
  assert.equal(reply.status, 202);
  assert.deepEqual(vm.sent, [{ envelope_id: "panel:t1", type: "panel",
    payload: { init_data: initData, id: "t1", action: "refresh", args: {} } }]);
});

test("an unsigned page gets nothing and queues nothing", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const vm = fakeSocket();
  const env = panelEnv(newRelay([vm]));
  const forged = signed({ token: "999:another-bot" });
  assert.equal((await relay.fetch(fromPage("/app/state", forged), env)).status, 401);
  assert.equal((await relay.fetch(fromPage("/app/action", forged, { id: "t1", action: "refresh" }), env)).status, 401);
  assert.deepEqual(vm.sent, []);
});
