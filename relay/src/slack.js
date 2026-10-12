/*
 * Slack's part of the relay, mounted while the slack add-in is on (the router's side is
 * lib/multiclaude/addons/slack).
 *
 * Slack posts to /slack, and people back from "Sign in with Slack" land on /slack/oauth. The relay
 * checks Slack's signature, answers at once (a slash command fails unless answered within 3
 * seconds), and queues each one for the VM in Slack's Socket Mode shape: {envelope_id, type, payload}.
 */
import { hmacHex, sameSecret } from "./secrets.js";
import { keepForVm } from "./vm.js";

const SIGNATURE_MAX_AGE = 5 * 60;  // seconds; older requests may be replays

export const routes = [
  { method: "POST", path: "/slack", handle: fromSlack },
  { method: "GET", path: "/slack/oauth", handle: fromSignIn },
];

async function fromSlack(request, env) {
  const body = await request.text();
  if (!(await signedBySlack(request.headers, body, env.SLACK_SIGNING_SECRET))) {
    return new Response("bad signature", { status: 401 });
  }
  const sent = readSlack(request.headers.get("content-type") || "", body);
  if (sent.type === "url_verification") return Response.json({ challenge: sent.challenge });
  const envelope = asEnvelope(sent);
  if (envelope) await keepForVm(env, envelope);
  return new Response(null, { status: 200 });
}

/* Slack sends a person here after they let the bot delete their own messages ("Sign in with
 * Slack"). The VM trades the code for their token and checks the state, so nothing here is trusted. */
async function fromSignIn(request, env) {
  const envelope = signInEnvelope(new URL(request.url).searchParams);
  if (!envelope) return page("Slack didn't sign you in, so nothing changed. Ask me for a new link.");
  await keepForVm(env, envelope);
  return page("✅ Done. I'll confirm in Slack in a moment; you can close this tab.");
}

/* The envelope for a person's way back from Slack's sign-in, or null when they declined. */
export function signInEnvelope(params) {
  const code = params.get("code");
  const state = params.get("state");
  if (!code || !state) return null;
  return { envelope_id: `sign-in:${code}`, type: "sign_in", payload: { code, state } };
}

function page(text) {
  const html = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">' +
    `<title>Slack sign-in</title><p style="font:18px system-ui;margin:3em auto;max-width:30em;padding:0 1em">${text}</p>`;
  return new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } });
}

/* Slack signs "v0:<timestamp>:<body>" with the app's signing secret (HMAC-SHA256). */
export async function signedBySlack(headers, body, secret, now = Date.now()) {
  const timestamp = headers.get("x-slack-request-timestamp") || "";
  const signature = headers.get("x-slack-signature") || "";
  if (!secret || !/^\d+$/.test(timestamp) || Math.abs(now / 1000 - Number(timestamp)) > SIGNATURE_MAX_AGE) {
    return false;
  }
  const mac = await hmacHex(new TextEncoder().encode(secret), `v0:${timestamp}:${body}`);
  return sameSecret(signature, `v0=${mac}`);
}

/* What Slack sent: events come as JSON, slash commands as form fields, button taps as a form
 * field "payload" holding JSON. */
export function readSlack(contentType, body) {
  if (contentType.startsWith("application/json")) return JSON.parse(body);
  const fields = Object.fromEntries(new URLSearchParams(body));
  if (fields.payload) return { type: "interactive", payload: JSON.parse(fields.payload) };
  return { type: "slash_commands", payload: fields };
}

/* The Socket Mode envelope for what Slack sent, or null for anything the router doesn't read. */
export function asEnvelope(sent) {
  if (sent.type === "event_callback") return { envelope_id: sent.event_id, type: "events_api", payload: sent };
  if (sent.type === "slash_commands") {
    return { envelope_id: sent.payload.trigger_id || crypto.randomUUID(), type: "slash_commands", payload: sent.payload };
  }
  if (sent.type === "interactive") return { envelope_id: crypto.randomUUID(), type: "interactive", payload: sent.payload };
  return null;
}
