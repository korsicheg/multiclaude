/*
 * OwnTracks' part of the relay, mounted while the locations add-in is on (the router's side is
 * lib/multiclaude/addons/locations/owntracks.py).
 *
 * The OwnTracks app on a person's phone posts where it is to /owntracks, as JSON with HTTP Basic
 * auth whose password is their OwnTracks key (the user name is the app's to choose). Only the VM can
 * check a key (it keeps their SHA-256 hashes), so the envelope carries the key's hash, never the key,
 * and the request is held until the VM puts {"ok"} or {"error"} at /owntracks/<id>. A key the VM
 * doesn't know gets 401, which the app shows. When the VM is slow or offline, the app hears its
 * update is taken after ANSWER_WITHIN, and the update stays queued for the VM.
 */
import { sha256Hex } from "./secrets.js";
import { REQUEST_ID_RE, answerFromVm, askVm, fromTheVm, newRequestId } from "./vm.js";

export const ANSWER_WITHIN = 10 * 1000;  // ms the app's request is held for the VM's answer
const MESSAGE_MAX = 16 * 1024;           // bytes of what the app posts
const ANSWER_MAX = 1024;                 // bytes of an answer the VM puts
const TAKEN = "[]";                      // OwnTracks reads the answer as messages for it: none

export const routes = [
  { method: "POST", path: "/owntracks", handle: fromOwnTracks },
  { method: "PUT", path: /^\/owntracks\/([^/]+)$/, handle: answerForOwnTracks },
];

/* An update from the app, held until the VM answers or ANSWER_WITHIN passes. */
async function fromOwnTracks(request, env) {
  const key = basicPassword(request.headers);
  if (!key) return Response.json({ error: "no OwnTracks key" }, { status: 401 });
  const body = await request.text();
  if (!body) return taken();  // OwnTracks posts nothing when a friend is deleted in the app
  const message = ownTracksMessage(body);
  if (!message) return Response.json({ error: "not an OwnTracks message" }, { status: 400 });
  return toOwnTracks(await askVm(env, await ownTracksEnvelope(newRequestId(), key, message), ANSWER_WITHIN));
}

async function answerForOwnTracks(request, env, [, id]) {
  if (!fromTheVm(request.headers, env)) return new Response("unauthorized", { status: 401 });
  if (!REQUEST_ID_RE.test(id)) return new Response("not a request id", { status: 400 });
  const body = await request.text();
  if (body.length > ANSWER_MAX) return new Response("too large", { status: 413 });
  return answerFromVm(env, envelopeId(id), body);
}

/* The password of an HTTP Basic authorization header; "" when there is none. */
export function basicPassword(headers) {
  const [scheme, credentials] = (headers.get("authorization") || "").split(" ");
  if (scheme?.toLowerCase() !== "basic" || !credentials) return "";
  try {
    const decoded = atob(credentials.trim());
    return decoded.slice(decoded.indexOf(":") + 1);
  } catch {
    return "";
  }
}

/* The JSON object OwnTracks posted (every one names its _type), or null. */
export function ownTracksMessage(body) {
  if (body.length > MESSAGE_MAX) return null;
  try {
    const message = JSON.parse(body);
    return message && typeof message === "object" && !Array.isArray(message) && message._type ? message : null;
  } catch {
    return null;
  }
}

/* The envelope that takes the message to the VM, as request id, with the key's hash in its place. */
export async function ownTracksEnvelope(id, key, message, now = Date.now()) {
  const keyHash = await sha256Hex(key);
  const payload = { id, key_hash: keyHash, message, received: now / 1000 };
  return { envelope_id: envelopeId(id), type: "owntracks", payload };
}

function envelopeId(requestId) {
  return `owntracks:${requestId}`;
}

/* What the app gets for askVm's outcome: a key the VM didn't know turned away, else taken. */
export function toOwnTracks({ answer, late }) {
  return !late && answer.error ? Response.json({ error: answer.error }, { status: 401 }) : taken();
}

function taken() {
  return new Response(TAKEN, { headers: { "content-type": "application/json" } });
}
