/*
 * The control panel's part of the relay (the router's side is lib/multiclaude/router/panel.py).
 *
 * The page sends Telegram's signed init data as "Authorization: tma <init data>". TELEGRAM_WEBAPP_KEY,
 * the key Telegram derives from the bot token, checks that signature but can't drive the bot; the VM
 * checks it again before it acts on a tap.
 */
import { fromHex, hmacHex, sameSecret } from "./secrets.js";

const SIGNED_FOR = 24 * 60 * 60;         // seconds Telegram's signature is trusted, as on the VM
export const SNAPSHOT_MAX = 256 * 1024;  // bytes
const TAP_MAX = 4096;                    // bytes of a tap's JSON
const ID_RE = /^[\w-]{1,64}$/;

/* The Telegram user who opened the panel, from its init data, when Telegram signed it less than
 * SIGNED_FOR ago; null otherwise. The data checked is every field but the hash, sorted, one
 * "key=value" a line. */
export async function webAppUser(initData, keyHex, now = Date.now()) {
  if (!initData || !keyHex) return null;
  const fields = new URLSearchParams(initData);
  const hash = fields.get("hash") || "";
  fields.delete("hash");
  const checked = [...fields].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([key, value]) => `${key}=${value}`).join("\n");
  if (!sameSecret(hash, await hmacHex(fromHex(keyHex), checked))) return null;
  if (now / 1000 - Number(fields.get("auth_date")) > SIGNED_FOR) return null;
  try {
    const user = JSON.parse(fields.get("user"));
    return user && Number.isSafeInteger(user.id) ? user : null;
  } catch {
    return null;
  }
}

export function initDataOf(headers) {
  return (headers.get("authorization") || "").replace(/^tma /, "");
}

/* The envelope that takes a tap ({id, action, args}) to the VM, or null for one that isn't. */
export function tapEnvelope(initData, body) {
  if (body.length > TAP_MAX) return null;
  let tap;
  try {
    tap = JSON.parse(body);
  } catch {
    return null;
  }
  if (!tap || !ID_RE.test(tap.id || "") || !ID_RE.test(tap.action || "")) return null;
  const args = tap.args && typeof tap.args === "object" ? tap.args : {};
  return { envelope_id: `panel:${tap.id}`, type: "panel", payload: { init_data: initData, id: tap.id, action: tap.action, args } };
}
