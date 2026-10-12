// Slack's part of the relay (src/slack.js), mounted while env.ADDONS names slack. Nothing here
// reaches Slack or Cloudflare.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import relay from "../src/index.js";
import { asEnvelope, readSlack, signInEnvelope, signedBySlack } from "../src/slack.js";
import { queueStub } from "./fakes.js";

const SECRET = "signing-secret";
const NOW = 1_791_530_000_000;

function slackHeaders(body, { secret = SECRET, timestamp = Math.floor(NOW / 1000), type } = {}) {
  const signature = "v0=" + createHmac("sha256", secret).update(`v0:${timestamp}:${body}`).digest("hex");
  return new Headers({ "x-slack-request-timestamp": String(timestamp), "x-slack-signature": signature,
    ...(type ? { "content-type": type } : {}) });
}

function slackEnv(kept, addons = "slack") {
  return { ADDONS: addons, SLACK_SIGNING_SECRET: SECRET, RELAY: queueStub(kept) };
}

function fromSlack(body, type) {
  return new Request("https://relay.test/slack", { method: "POST", body, headers: slackHeaders(body, { type }) });
}

test("a request Slack signed is accepted", async () => {
  const body = "command=%2Fclear";
  assert.equal(await signedBySlack(slackHeaders(body), body, SECRET, NOW), true);
});

test("a wrong signature, secret or stale timestamp is refused", async () => {
  const body = "command=%2Fclear";
  assert.equal(await signedBySlack(slackHeaders(body, { secret: "other" }), body, SECRET, NOW), false);
  assert.equal(await signedBySlack(slackHeaders(body), "command=%2Fmodel", SECRET, NOW), false);
  assert.equal(await signedBySlack(slackHeaders(body, { timestamp: NOW / 1000 - 600 }), body, SECRET, NOW), false);
  assert.equal(await signedBySlack(slackHeaders(body), body, undefined, NOW), false);
});

test("an event, a slash command and a button tap become Socket Mode envelopes", () => {
  const event = { type: "event_callback", event_id: "Ev1", event: { type: "message", text: "hi" } };
  assert.deepEqual(asEnvelope(readSlack("application/json", JSON.stringify(event))),
    { envelope_id: "Ev1", type: "events_api", payload: event });

  const command = asEnvelope(readSlack("application/x-www-form-urlencoded",
    "command=%2Fmodel&text=opus&user_id=U1&channel_id=D1&trigger_id=T9"));
  assert.equal(command.envelope_id, "T9");
  assert.equal(command.type, "slash_commands");
  assert.deepEqual(command.payload, { command: "/model", text: "opus", user_id: "U1", channel_id: "D1", trigger_id: "T9" });

  const tap = { type: "block_actions", actions: [{ action_id: "ask:0" }] };
  const envelope = asEnvelope(readSlack("application/x-www-form-urlencoded",
    new URLSearchParams({ payload: JSON.stringify(tap) }).toString()));
  assert.equal(envelope.type, "interactive");
  assert.deepEqual(envelope.payload, tap);
  assert.ok(envelope.envelope_id);
});

test("Slack's URL check gets its challenge back, and nothing is queued", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const kept = [];
  const reply = await relay.fetch(fromSlack(JSON.stringify({ type: "url_verification", challenge: "c-123" }),
    "application/json"), slackEnv(kept));
  assert.deepEqual(await reply.json(), { challenge: "c-123" });
  assert.deepEqual(kept, []);
});

test("a slash command is answered at once and handed to the queue", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const kept = [];
  const reply = await relay.fetch(fromSlack("command=%2Fclear&text=&user_id=U1&channel_id=D1&trigger_id=T1",
    "application/x-www-form-urlencoded"), slackEnv(kept));
  assert.equal(reply.status, 200);
  assert.equal(kept[0].type, "slash_commands");
  assert.equal(kept[0].envelope_id, "T1");
});

test("a person back from Slack's sign-in is shown a page and their code goes to the queue", async () => {
  const kept = [];
  const reply = await relay.fetch(new Request("https://relay.test/slack/oauth?code=C1&state=S1"), slackEnv(kept));
  assert.equal(reply.status, 200);
  assert.match(await reply.text(), /Done/);
  assert.deepEqual(kept, [{ envelope_id: "sign-in:C1", type: "sign_in", payload: { code: "C1", state: "S1" } }]);
});

test("a declined sign-in queues nothing", () => {
  assert.equal(signInEnvelope(new URLSearchParams("error=access_denied&state=S1")), null);
});

test("an unsigned request is refused", async () => {
  const reply = await relay.fetch(new Request("https://relay.test/slack", { method: "POST", body: "command=%2Fclear" }),
    slackEnv([]));
  assert.equal(reply.status, 401);
});

test("while the slack add-in is off, Slack's routes aren't there", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const kept = [];
  for (const addons of [undefined, "", "siri"]) {
    const reply = await relay.fetch(fromSlack("command=%2Fclear&trigger_id=T1", "application/x-www-form-urlencoded"),
      { ...slackEnv(kept), ADDONS: addons });
    assert.equal(reply.status, 404);
  }
  assert.equal((await relay.fetch(new Request("https://relay.test/slack/oauth?code=C1&state=S1"), slackEnv(kept, ""))).status,
    404);
  assert.deepEqual(kept, []);
});
