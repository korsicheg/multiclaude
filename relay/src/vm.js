/*
 * The way to the VM, for the relay's core routes and the add-ins' alike: the one Durable Object
 * (index.js, Relay) that keeps what waits for the VM, and the token that shows a request is the VM's.
 */
import { sameSecret } from "./secrets.js";

const RELAY_NAME = "relay";  // the one Durable Object; another name would start an empty one

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
