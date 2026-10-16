/*
 * The way to the VM, for the relay's core routes and the add-ins' alike: the one Durable Object
 * (index.js, Relay) that keeps what waits for the VM, and the token that shows a request is the VM's.
 */
import { hex, sameSecret } from "./secrets.js";

const RELAY_NAME = "relay";  // the one Durable Object; another name would start an empty one
export const REQUEST_ID_RE = /^[0-9a-f]{8}$/;  // a request that waits for the VM's answer, as it names it

/* A new id for a request that waits for the VM's answer (askVm). */
export function newRequestId() {
  return hex(crypto.getRandomValues(new Uint8Array(4)));
}

export function relayObject(env) {
  return env.RELAY.get(env.RELAY.idFromName(RELAY_NAME));
}

/* Queue an envelope ({envelope_id, type, payload}) for the VM. */
export function keepForVm(env, envelope) {
  return relayObject(env).fetch("https://relay/keep", { method: "POST", body: JSON.stringify(envelope) });
}

/* Queue an envelope for the VM and wait up to waitFor ms for the VM to answer it (answerFromVm):
 * {answer} with what it put, or {late: true}. The envelope stays queued either way. */
export async function askVm(env, envelope, waitFor) {
  const reply = await relayObject(env).fetch(`https://relay/ask?wait=${waitFor}`,
    { method: "POST", body: JSON.stringify(envelope) });
  return reply.json();
}

/* The VM's answer (JSON text) to the envelope askVm is waiting on: 204, or 410 once nothing waits. */
export function answerFromVm(env, envelopeId, body) {
  return relayObject(env).fetch(`https://relay/answer?id=${encodeURIComponent(envelopeId)}`, { method: "PUT", body });
}

export function fromTheVm(headers, env) {
  return sameSecret(headers.get("authorization") || "", `Bearer ${env.RELAY_TOKEN}`);
}
