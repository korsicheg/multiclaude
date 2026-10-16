/*
 * The relay: the way to the VM, through the VM's network dropouts, for the control panel and for
 * the apps the add-ins that are on bring.
 *
 * What comes for the VM is kept in a Durable Object until the VM has it. The router holds a
 * WebSocket to /queue: it gets "hello" and everything still kept, then each new envelope
 * ({envelope_id, type, payload}), and acknowledges each by its envelope_id. The control panel's page
 * is served from public/app; its requests are checked here (panel.js) and its taps queued. An
 * add-in's routes (ADDON_ROUTES) are mounted while env.ADDONS, set by `assistant relay deploy`,
 * names it.
 */
import { SNAPSHOT_MAX, initDataOf, tapEnvelope, webAppUser } from "./panel.js";
import { routes as ownTracksRoutes } from "./owntracks.js";
import { routes as slackRoutes } from "./slack.js";
import { fromTheVm, keepForVm, relayObject } from "./vm.js";
import { routes as siriRoutes } from "./voice.js";

const KEPT_FOR = 24 * 60 * 60 * 1000;      // an envelope the VM hasn't collected in a day is dropped
const PANEL_WAIT_FOR = 25 * 1000;          // ms a page's read waits for a snapshot newer than the one it has

const ADDON_ROUTES = { slack: slackRoutes, siri: siriRoutes, locations: ownTracksRoutes };

/* {method (any when missing), path (a string, or a RegExp whose match goes to handle), handle}. */
const CORE_ROUTES = [
  { path: "/queue", handle: toVm },
  { method: "POST", path: "/app/state", handle: panelState },
  { method: "POST", path: "/app/action", handle: panelTap },
  { method: "PUT", path: /^\/panel\/(\d+)$/, handle: panelFromVm },  // a person's snapshot, by Telegram id
];

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    for (const route of routesFor(env)) {
      const match = matching(route, request.method, pathname);
      if (match) return route.handle(request, env, match);
    }
    return new Response("not found", { status: 404 });
  },
};

/* The core's routes, then those of the add-ins env.ADDONS names (comma-separated). */
export function routesFor(env) {
  const on = (env.ADDONS || "").split(",").map((name) => name.trim());
  return [...CORE_ROUTES, ...on.flatMap((name) => ADDON_ROUTES[name] || [])];
}

function matching(route, method, pathname) {
  if (route.method && route.method !== method) return null;
  if (typeof route.path === "string") return route.path === pathname ? [pathname] : null;
  return route.path.exec(pathname);
}

async function toVm(request, env) {
  if (request.headers.get("upgrade") !== "websocket") return new Response("WebSocket only", { status: 426 });
  if (!fromTheVm(request.headers, env)) return new Response("unauthorized", { status: 401 });
  return relayObject(env).fetch(request);
}

/* The latest snapshot for whoever opened the panel. With {after: version} the read is held until a
 * newer one comes, so a page waiting for a tap's answer asks once instead of polling. */
async function panelState(request, env) {
  const user = await webAppUser(initDataOf(request.headers), env.TELEGRAM_WEBAPP_KEY);
  if (!user) return new Response("unsigned", { status: 401 });
  const { after } = await request.json().catch(() => ({}));
  return relayObject(env).fetch(`https://relay/panel?user=${user.id}&after=${Number(after) || 0}`);
}

async function panelTap(request, env) {
  const initData = initDataOf(request.headers);
  if (!(await webAppUser(initData, env.TELEGRAM_WEBAPP_KEY))) return new Response("unsigned", { status: 401 });
  const envelope = tapEnvelope(initData, await request.text());
  if (!envelope) return new Response("not a tap", { status: 400 });
  await keepForVm(env, envelope);
  return new Response(null, { status: 202 });
}

async function panelFromVm(request, env, [, user]) {
  if (!fromTheVm(request.headers, env)) return new Response("unauthorized", { status: 401 });
  const body = await request.text();
  if (body.length > SNAPSHOT_MAX) return new Response("too large", { status: 413 });
  return relayObject(env).fetch(`https://relay/panel?user=${user}`, { method: "PUT", body });
}

/* The queue, each person's latest panel snapshot, and the requests waiting for the VM's answer.
 * Hibernatable WebSockets, so it sleeps between messages. */
export class Relay {
  constructor(ctx) {
    this.ctx = ctx;
    this.sql = ctx.storage.sql;
    this.sql.exec("CREATE TABLE IF NOT EXISTS envelopes (id TEXT PRIMARY KEY, body TEXT NOT NULL, kept INTEGER NOT NULL)");
    this.sql.exec("CREATE TABLE IF NOT EXISTS panels (user TEXT PRIMARY KEY, body TEXT NOT NULL, kept INTEGER NOT NULL)");
    this.waiting = new Set();  // pages' reads waiting for a newer snapshot: {user, wake}
    this.unanswered = new Map();  // envelope id -> its answer's resolver, while a request waits for it (vm.js, askVm)
  }

  async fetch(request) {
    const url = new URL(request.url);
    const param = (name) => url.searchParams.get(name);
    if (url.pathname === "/keep") return this.keep(await request.json());
    if (url.pathname === "/ask") return this.ask(await request.json(), Number(param("wait")));
    if (url.pathname === "/answer") return this.answer(param("id"), await request.text());
    if (url.pathname === "/panel" && request.method === "PUT") return this.keepPanel(param("user"), await request.text());
    if (url.pathname === "/panel") return this.panel(param("user"), Number(param("after")) || 0);
    return this.connect();
  }

  /* Queue the envelope for the VM, and answer with {answer} once the VM puts one, or {late: true}
   * after waitFor ms. */
  async ask(envelope, waitFor) {
    const id = envelope.envelope_id;
    const outcome = new Promise((resolve) => {
      const timer = setTimeout(() => this.settle(id, { late: true }), waitFor);
      this.unanswered.set(id, (settled) => {
        clearTimeout(timer);
        this.unanswered.delete(id);
        resolve(settled);
      });
    });
    this.keep(envelope);
    return Response.json(await outcome);
  }

  /* The VM's answer to the envelope id; one that comes after the request stopped waiting is dropped. */
  answer(id, body) {
    let answer;
    try {
      answer = JSON.parse(body);
    } catch {
      return new Response("not JSON", { status: 400 });
    }
    return new Response(null, { status: this.settle(id, { answer }) ? 204 : 410 });
  }

  settle(id, outcome) {
    const resolve = this.unanswered.get(id);
    if (resolve) resolve(outcome);
    return Boolean(resolve);
  }

  keepPanel(user, body, now = Date.now()) {
    this.sql.exec("INSERT OR REPLACE INTO panels (user, body, kept) VALUES (?, ?, ?)", user, body, now);
    for (const read of this.waiting) if (read.user === user) read.wake();
    return new Response(null, { status: 204 });
  }

  /* {snapshot, version}: user's latest snapshot (null before the first) and when it was kept, in ms.
   * With after, it waits up to waitFor for one newer than that. */
  async panel(user, after = 0, waitFor = PANEL_WAIT_FOR) {
    let row = this.latestPanel(user);
    if (after && (!row || row.kept <= after)) {
      await this.newerPanel(user, waitFor);
      row = this.latestPanel(user);
    }
    return new Response(`{"snapshot":${row ? row.body : "null"},"version":${row ? row.kept : 0}}`,
      { headers: { "content-type": "application/json" } });
  }

  latestPanel(user) {
    return this.sql.exec("SELECT body, kept FROM panels WHERE user = ?", user).toArray()[0];
  }

  newerPanel(user, waitFor) {
    return new Promise((resolve) => {
      const read = { user, wake: () => {
        clearTimeout(timer);
        this.waiting.delete(read);
        resolve();
      } };
      const timer = setTimeout(read.wake, waitFor);
      this.waiting.add(read);
    });
  }

  keep(envelope, now = Date.now()) {
    this.sql.exec("DELETE FROM envelopes WHERE kept < ?", now - KEPT_FOR);
    const body = JSON.stringify(envelope);
    const added = this.sql.exec("INSERT OR IGNORE INTO envelopes (id, body, kept) VALUES (?, ?, ?)",
      envelope.envelope_id, body, now).rowsWritten;
    if (added) this.ctx.getWebSockets().forEach((ws) => sendQuietly(ws, body));
    return new Response(null, { status: 204 });
  }

  connect() {
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server);
    this.greet(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  /* A new connection gets "hello", then everything the VM hasn't acknowledged yet, oldest first. */
  greet(ws) {
    ws.send(JSON.stringify({ type: "hello" }));
    for (const { body } of this.sql.exec("SELECT body FROM envelopes ORDER BY kept").toArray()) ws.send(body);
  }

  webSocketMessage(ws, message) {
    let ack;
    try {
      ack = JSON.parse(message);
    } catch {
      return;
    }
    if (ack.envelope_id) this.sql.exec("DELETE FROM envelopes WHERE id = ?", ack.envelope_id);
  }

  webSocketClose(ws) {
    try {
      ws.close();
    } catch {
      // already closed on both sides
    }
  }
}

/* A socket whose VM went quiet may already be gone; the envelope waits for the next one. */
function sendQuietly(ws, body) {
  try {
    ws.send(body);
  } catch {
    // kept anyway
  }
}
