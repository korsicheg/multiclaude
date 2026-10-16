/* HMAC signatures, SHA-256, hex and constant-time comparison, shared by the relay's entrances. */

export async function hmacHex(keyBytes, text) {
  const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text)));
}

/* What the VM knows an app's key by: its SHA-256, in hex. */
export async function sha256Hex(text) {
  return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

export function hex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function fromHex(text) {
  return new Uint8Array((text.match(/../g) || []).map((pair) => parseInt(pair, 16)));
}

/* Compares in time that doesn't depend on where the strings differ. */
export function sameSecret(given, expected) {
  if (given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}
