/*
 * Reminders' part of the relay, mounted while the reminders add-in is on (the router's side is
 * lib/multiclaude/addons/reminders).
 *
 * An Apple Shortcut on a person's phone posts their open reminders to /reminders with
 * "Authorization: Bearer <reminders key>", and hears back which reminders to add there. Only the VM
 * can check a key (it keeps their SHA-256 hashes), so the envelope carries the key's hash, never the
 * key, and the request is held until the VM puts {"add": [...]} or {"error"} at /reminders/<id>. A
 * key the VM doesn't know gets 401. When the VM is slow or offline, the shortcut hears there's
 * nothing to add after ANSWER_WITHIN, the reminders it sent stay queued for the VM, and what waits
 * to be added goes with a later sync.
 */
import { bearerKey, sha256Hex } from "./secrets.js";
import { REQUEST_ID_RE, answerFromVm, askVm, fromTheVm, newRequestId } from "./vm.js";

export const ANSWER_WITHIN = 10 * 1000;  // ms the shortcut's request is held for the VM's answer
const SENT_MAX = 128 * 1024;             // bytes of the reminders the shortcut posts
const ANSWER_MAX = 64 * 1024;            // bytes of an answer the VM puts

export const routes = [
  { method: "POST", path: "/reminders", handle: fromShortcut },
  { method: "PUT", path: /^\/reminders\/([^/]+)$/, handle: answerForShortcut },
];

/* The open reminders from the shortcut, held until the VM answers or ANSWER_WITHIN passes. Their
 * form is the VM's to read: the shortcut may send JSON or plain text. */
async function fromShortcut(request, env) {
  const key = bearerKey(request.headers);
  if (!key) return Response.json({ error: "no reminders key" }, { status: 401 });
  const sent = await request.text();
  if (!sent.trim()) return Response.json({ error: "no reminders sent" }, { status: 400 });
  if (sent.length > SENT_MAX) return Response.json({ error: "too many reminders" }, { status: 413 });
  return toShortcut(await askVm(env, await remindersEnvelope(newRequestId(), key, sent), ANSWER_WITHIN));
}

async function answerForShortcut(request, env, [, id]) {
  if (!fromTheVm(request.headers, env)) return new Response("unauthorized", { status: 401 });
  if (!REQUEST_ID_RE.test(id)) return new Response("not a request id", { status: 400 });
  const body = await request.text();
  if (body.length > ANSWER_MAX) return new Response("too large", { status: 413 });
  return answerFromVm(env, envelopeId(id), body);
}

/* The envelope that takes what the shortcut sent to the VM, as request id, with the key's hash in its
 * place. */
export async function remindersEnvelope(id, key, sent, now = Date.now()) {
  const keyHash = await sha256Hex(key);
  return { envelope_id: envelopeId(id), type: "reminders", payload: { id, key_hash: keyHash, sent, received: now / 1000 } };
}

function envelopeId(requestId) {
  return `reminders:${requestId}`;
}

/* What the shortcut gets for askVm's outcome: the reminders to add, a key the VM didn't know turned
 * away, or nothing to add when the VM didn't answer in time. */
export function toShortcut({ answer, late }) {
  if (late) return Response.json({ add: [] });
  if (answer.error) return Response.json({ error: answer.error }, { status: 401 });
  return Response.json({ add: Array.isArray(answer.add) ? answer.add : [] });
}
