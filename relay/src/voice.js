/*
 * Siri's part of the relay, mounted while the siri add-in is on (the router's side is
 * lib/multiclaude/addons/siri).
 *
 * An Apple Shortcut posts the words to /voice with "Authorization: Bearer <voice key>". Only the VM
 * can check a key (it keeps their SHA-256 hashes), so the envelope carries the key's hash, never the
 * key, and the request is held until the VM puts {"reply"} or {"error"} at /voice/<id>. Siri gives up
 * soon after 25 seconds, so after ANSWER_WITHIN the shortcut hears the answer will come on Telegram;
 * the words stay queued.
 */
import { sha256Hex } from "./secrets.js";
import { REQUEST_ID_RE, answerFromVm, askVm, fromTheVm, newRequestId } from "./vm.js";

export const ANSWER_WITHIN = 25 * 1000;  // ms Siri's request is held for the VM's answer
export const LATE_REPLY = "On it. I'll send you the answer on Telegram.";
export const ANSWER_MAX = 64 * 1024;     // bytes of an answer the VM puts
const WORDS_MAX = 16 * 1024;             // bytes of what the shortcut posts

export const routes = [
  { method: "POST", path: "/voice", handle: fromSiri },
  { method: "PUT", path: /^\/voice\/([^/]+)$/, handle: answerForSiri },
];

/* Spoken words, held until the VM answers or ANSWER_WITHIN passes. */
async function fromSiri(request, env) {
  const key = bearerKey(request.headers);
  if (!key) return Response.json({ error: "no voice key" }, { status: 401 });
  const text = spokenWords(await request.text());
  if (!text) return Response.json({ error: "no words" }, { status: 400 });
  return toSiri(await askVm(env, await voiceEnvelope(newRequestId(), key, text), ANSWER_WITHIN));
}

async function answerForSiri(request, env, [, id]) {
  if (!fromTheVm(request.headers, env)) return new Response("unauthorized", { status: 401 });
  if (!REQUEST_ID_RE.test(id)) return new Response("not a request id", { status: 400 });
  const body = await request.text();
  if (body.length > ANSWER_MAX) return new Response("too large", { status: 413 });
  return answerFromVm(env, envelopeId(id), body);
}

export function bearerKey(headers) {
  const [scheme, key] = (headers.get("authorization") || "").split(" ");
  return scheme?.toLowerCase() === "bearer" && key ? key.trim() : "";
}

/* The words from a JSON {"text": …} body or a plain text one; "" when there are none. */
export function spokenWords(body) {
  if (!body || body.length > WORDS_MAX) return "";
  try {
    const sent = JSON.parse(body);
    if (sent && typeof sent === "object") return String(sent.text ?? "").trim();
  } catch {
    // plain text
  }
  return body.trim();
}

/* The envelope that takes the words to the VM, as request id, with the key's hash in its place. */
export async function voiceEnvelope(id, key, text, now = Date.now()) {
  const keyHash = await sha256Hex(key);
  return { envelope_id: envelopeId(id), type: "voice", payload: { id, key_hash: keyHash, text, said: now / 1000 } };
}

function envelopeId(requestId) {
  return `voice:${requestId}`;
}

/* What Siri gets for askVm's outcome: the VM's reply, a key it didn't know turned away, or the
 * promise of an answer on Telegram. */
export function toSiri({ answer, late }) {
  if (late) return Response.json({ reply: LATE_REPLY });
  return answer.error ? Response.json({ error: answer.error }, { status: 401 }) : Response.json({ reply: answer.reply });
}
