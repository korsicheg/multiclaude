// Stand-ins for the Durable Object runtime, shared by the relay's tests.
import { DatabaseSync } from "node:sqlite";

import { Relay } from "../src/index.js";

/* ctx.storage.sql as the Durable Object runtime has it: exec(query, ...bindings). */
function fakeSql() {
  const db = new DatabaseSync(":memory:");
  return {
    exec(query, ...bindings) {
      const statement = db.prepare(query);
      if (/^\s*SELECT/i.test(query)) {
        const rows = statement.all(...bindings);
        return { toArray: () => rows, rowsWritten: 0 };
      }
      const { changes } = statement.run(...bindings);
      return { toArray: () => [], rowsWritten: Number(changes) };
    },
  };
}

export function fakeSocket() {
  return { sent: [], send(text) { this.sent.push(JSON.parse(text)); } };
}

export function newRelay(sockets = []) {
  return new Relay({ storage: { sql: fakeSql() }, getWebSockets: () => sockets });
}

/* env.RELAY: the Durable Object namespace, whose one object is relay. */
export function namespaceOf(relay) {
  return { idFromName: (name) => name, get: () => ({ fetch: (url, init) => relay.fetch(new Request(url, init)) }) };
}

/* env.RELAY whose one object only records the envelopes it's asked to keep, in kept. */
export function queueStub(kept) {
  return {
    idFromName: (name) => name,
    get: () => ({
      async fetch(url, init) {
        kept.push(JSON.parse(init.body));
        return new Response(null, { status: 204 });
      },
    }),
  };
}
