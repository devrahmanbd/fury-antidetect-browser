// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 Bogdan Shapovalov and the Fury authors
//
// Two machines, one team profile, a real server, a real browser.
//
// What a tester does by hand -- open a team profile, star a site, change a
// setting, close it, open it on another computer -- done by a script, with the
// time and size of every step written down. Driven through the agent's own IPC,
// the way the desktop shell drives it, and through the browser's DevTools
// protocol for what a person would click.
//
// Started by tools/team-e2e/run.sh, which brings up PostgreSQL, fury-server and
// two agents with separate FURY_HOMEs. Run directly, it expects:
//
//   FURY_E2E_SERVER   http://127.0.0.1:18080 (FURY_OPEN_SIGNUP=1)
//   FURY_E2E_SOCK_A   agent A's socket (FURY_SOCKET)
//   FURY_E2E_SOCK_B   agent B's socket
//   FURY_E2E_HOME_A   agent A's FURY_HOME, for its log and profile directory
//   FURY_E2E_HOME_B   agent B's FURY_HOME
//   FURY_E2E_BUNDLES  the server's FURY_BUNDLE_DIR, to measure what was stored
//
// What it does NOT cover, said so it is not trusted for more: both machines are
// one person signed in twice, not a colleague invited with a narrower role, and
// the desktop shell is not running -- its Stop button is reproduced by doing
// what commands.rs does, not by clicking it.

import net from "node:net";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const env = (k) => {
  const v = process.env[k];
  if (!v) throw new Error(`${k} is not set; see the top of this file`);
  return v;
};
const SERVER = env("FURY_E2E_SERVER");
const BUNDLES = env("FURY_E2E_BUNDLES");
const MACHINES = {
  A: { sock: env("FURY_E2E_SOCK_A"), home: env("FURY_E2E_HOME_A") },
  B: { sock: env("FURY_E2E_SOCK_B"), home: env("FURY_E2E_HOME_B") },
};
const PERSONA = process.env.FURY_E2E_PERSONA ||
  (process.platform === "darwin" ? "macos-m2-1512x982" : "win11-rtx3060-1920x1080");
// A page that answers anywhere and sets nothing of its own.
const SITE = process.env.FURY_E2E_SITE || "https://example.com/";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => performance.now();
const secs = (ms) => `${(ms / 1000).toFixed(1)} s`;
const mb = (n) => `${(n / 1048576).toFixed(1)} MB`;

let passed = 0;
let failed = 0;
const claim = (ok, what) => {
  if (ok) { passed++; console.log(`  OK   ${what}`); }
  else { failed++; console.log(`  FAIL ${what}`); }
};

// --- the agent ---------------------------------------------------------------

function ipc(machine, method, params = {}, timeoutMs = 400_000) {
  return new Promise((resolve, reject) => {
    const s = net.connect(MACHINES[machine].sock);
    let buf = "";
    const timer = setTimeout(() => { s.destroy(); reject(new Error(`${method}: no answer`)); }, timeoutMs);
    s.on("error", (e) => { clearTimeout(timer); reject(e); });
    s.on("data", (d) => {
      buf += d;
      const nl = buf.indexOf("\n");
      if (nl < 0) return;
      clearTimeout(timer);
      s.end();
      const r = JSON.parse(buf.slice(0, nl));
      if (r.err !== undefined) reject(new Error(`${method}: ${typeof r.err === "string" ? r.err : JSON.stringify(r.err)}`));
      else resolve(r.ok);
    });
    s.write(JSON.stringify({ id: 1, method, params }) + "\n");
  });
}

const logFile = (m) => path.join(MACHINES[m].home, "logs", "agent.log");
const logSize = (m) => { try { return fs.statSync(logFile(m)).size; } catch { return 0; } };
const logSince = (m, from) => {
  try { return fs.readFileSync(logFile(m)).subarray(from).toString(); } catch { return ""; }
};

// --- the server --------------------------------------------------------------

async function api(method, p, token, body) {
  const res = await fetch(SERVER + p, {
    method,
    headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch { data = text; }
  if (!res.ok) throw new Error(`${method} ${p} -> ${res.status} ${text.slice(0, 300)}`);
  return data;
}

// --- the browser -------------------------------------------------------------

class Cdp {
  static async open(wsUrl) {
    const c = new Cdp();
    c.ws = new WebSocket(wsUrl);
    c.next = 1;
    c.waiting = new Map();
    c.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && c.waiting.has(msg.id)) {
        const { resolve, reject } = c.waiting.get(msg.id);
        c.waiting.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    };
    await new Promise((resolve, reject) => {
      c.ws.onopen = resolve;
      c.ws.onerror = () => reject(new Error(`cannot reach ${wsUrl}`));
    });
    return c;
  }
  send(method, params = {}, sessionId) {
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.waiting.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
  // Fire and forget: Browser.close never answers once the browser is gone.
  fire(method, params = {}) {
    try { this.ws.send(JSON.stringify({ id: this.next++, method, params })); } catch {}
  }
  async page(url) {
    const { targetId } = await this.send("Target.createTarget", { url });
    const { sessionId } = await this.send("Target.attachToTarget", { targetId, flatten: true });
    // WebUI pages bind their chrome.* objects as they load.
    for (let i = 0; i < 50; i++) {
      const r = await this.send("Runtime.evaluate", { expression: "document.readyState", returnByValue: true }, sessionId);
      if (r.result.value === "complete") break;
      await sleep(200);
    }
    await sleep(500);
    return {
      eval: async (expression) => {
        const r = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId);
        if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 400));
        return r.result.value;
      },
      close: () => this.send("Target.closeTarget", { targetId }).catch(() => {}),
    };
  }
  close() { try { this.ws.close(); } catch {} }
}

// What a person does in the profile: star a site, change a setting, sign in.
async function work(cdp, tag) {
  const bm = await cdp.page("chrome://bookmarks");
  await bm.eval(`new Promise(r => chrome.bookmarks.create(
    { parentId: "1", title: "e2e ${tag}", url: "${SITE}#${tag}" }, b => r(b.id)))`);
  await bm.close();

  const site = await cdp.page(SITE);
  await site.eval(`document.cookie = "e2e_${tag}=1; max-age=86400; path=/";
    localStorage.setItem("e2e_${tag}", "1"); true`);
  await site.close();
}

async function setPref(cdp, name, value) {
  const s = await cdp.page("chrome://settings");
  await s.eval(`new Promise(r => chrome.settingsPrivate.setPref(${JSON.stringify(name)}, ${JSON.stringify(value)}, "", ok => r(ok)))`);
  await s.close();
}

async function readPref(cdp, name) {
  const s = await cdp.page("chrome://settings");
  const v = await s.eval(`new Promise(r => chrome.settingsPrivate.getPref(${JSON.stringify(name)}, p => r(p && p.value)))`);
  await s.close();
  return v;
}

async function seen(cdp, tag) {
  const bm = await cdp.page("chrome://bookmarks");
  const bookmark = await bm.eval(`new Promise(r => chrome.bookmarks.search({ url: "${SITE}#${tag}" }, l => r(l.length > 0)))`);
  await bm.close();
  const site = await cdp.page(SITE);
  const cookie = await site.eval(`document.cookie.includes("e2e_${tag}=1")`);
  const storage = await site.eval(`localStorage.getItem("e2e_${tag}") === "1"`);
  await site.close();
  return { bookmark, cookie, storage };
}

// --- the exit -----------------------------------------------------------------

// A team profile cannot open without a proxy, by design, so the test brings
// one: plain HTTP, CONNECT and absolute-form requests, on loopback.
function startProxy() {
  const server = net.createServer((client) => {
    client.once("data", (head) => {
      const text = head.toString("latin1");
      const [line] = text.split("\r\n");
      const [method, target] = line.split(" ");
      if (method === "CONNECT") {
        const [host, port] = target.split(":");
        const up = net.connect(Number(port) || 443, host, () => {
          client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
          up.pipe(client);
          client.pipe(up);
        });
        up.on("error", () => client.destroy());
        client.on("error", () => up.destroy());
        return;
      }
      let url;
      try { url = new URL(target); } catch { client.destroy(); return; }
      const up = net.connect(Number(url.port) || 80, url.hostname, () => {
        up.write(head.toString("latin1").replace(target, url.pathname + url.search));
        up.pipe(client);
        client.pipe(up);
      });
      up.on("error", () => client.destroy());
      client.on("error", () => up.destroy());
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

// --- one team launch, the way commands.rs::launch does it ---------------------

async function lock(t, machine) {
  return api("POST", `/v1/profiles/${t.profileId}/lock`, t.tokens[machine], {
    machine_id: `e2e-${machine}`, machine_name: `e2e machine ${machine}`, force: false,
  });
}

async function launch(t, machine) {
  const grant = await lock(t, machine);
  const before = logSize(machine);
  const started = now();
  let out;
  try {
    out = await ipc(machine, "profile.launch", {
      id: t.profileId,
      profile: {
        id: t.profileId, project_id: null, name: "e2e team profile", notes: "", tags: [],
        persona_id: PERSONA, fp_seed: 12345,
        proxy: {
          id: "e2e", name: "e2e", kind: "http", host: "127.0.0.1", port: t.proxyPort,
          username: null, password: null, last_country: null, last_ip: null,
          rotate_url: null, checker_url: null,
        },
        timezone: "Europe/Berlin", languages: ["en-US", "en"],
        start_urls: [], inline_lists: [], last_opened_at: null,
      },
      server: { url: SERVER, token: t.tokens[machine] },
      lock_token: grant.lock_token,
      profile_key: t.profileKey,
      cdp: true,
    });
  } catch (e) {
    await api("POST", `/v1/profiles/${t.profileId}/unlock`, t.tokens[machine], { lock_token: grant.lock_token }).catch(() => {});
    throw e;
  }
  const took = now() - started;
  let cdp;
  try {
    cdp = await Cdp.open(out.ws_endpoint);
  } catch (e) {
    // The address the agent answered with was not this browser's. Until
    // 01.10.2026 the bundle carried DevToolsActivePort from the machine that
    // packed it, and the agent read that one back. Say so, and find the real
    // one, so the rest of the run still measures what it came for.
    console.log(`  FAIL the agent's DevTools address was dead (${out.ws_endpoint}); reading the browser's own`);
    failed++;
    const file = path.join(MACHINES[machine].home, "profiles", t.profileId, "DevToolsActivePort");
    for (let i = 0; i < 50 && !cdp; i++) {
      await sleep(200);
      const [port, ws] = fs.readFileSync(file, "utf8").split("\n");
      cdp = await Cdp.open(`ws://127.0.0.1:${port.trim()}${ws.trim()}`).catch(() => null);
    }
    if (!cdp) throw e;
  }
  const pulled = logSince(machine, before).split("\n").find((l) => /pulled bundle|no bundle on the server|keeping this machine|already holds/.test(l)) || "";
  return { machine, grant, cdp, took, pulled: pulled.replace(/\x1b\[[0-9;]*m/g, "").trim() };
}

async function serverVersion(t) {
  const list = await api("GET", `/v1/projects/${t.projectId}/profiles`, t.tokens.A);
  return list.find((p) => p.id === t.profileId)?.current_version ?? 0;
}

function storedBytes(t) {
  // The newest file the server wrote for this profile.
  let best = null;
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (p.includes(t.profileId)) {
        const st = fs.statSync(p);
        if (!best || st.mtimeMs > best.mtimeMs) best = st;
      }
    }
  };
  // Not readable when the server runs on another machine than the agents.
  try { walk(BUNDLES); } catch { return NaN; }
  return best ? best.size : 0;
}

// The three ways a profile gets closed.
//
//   agent    profile.stop over IPC: pack, push, unlock. What a local profile's
//            Stop button does, and what the API does.
//   window   the person closes the browser window; the agent's reaper notices
//            within two seconds and does the same teardown.
//   button   the desktop's Stop button on a TEAM profile, as commands.rs::stop
//            does it: profile.stop through the agent, and the lock released
//            by hand only when the agent was not running the browser. Until
//            01.10.2026 it released the lock and did nothing else; the browser
//            stayed open, and closing it later was refused "not locked by
//            you". FURY_E2E_OLD_STOP=1 does it the old way, to see that again.
async function close(t, run, how) {
  const before = logSize(run.machine);
  const v0 = await serverVersion(t);
  const started = now();
  const oldStop = how === "button" && process.env.FURY_E2E_OLD_STOP === "1";
  if (how === "agent") {
    await ipc(run.machine, "profile.stop", { id: t.profileId });
  } else if (how === "button" && !oldStop) {
    const out = await ipc(run.machine, "profile.stop", { id: t.profileId });
    if (!out.stopped) {
      await api("POST", `/v1/profiles/${t.profileId}/unlock`, t.tokens[run.machine], { lock_token: run.grant.lock_token });
    }
  } else {
    if (oldStop) {
      await api("POST", `/v1/profiles/${t.profileId}/unlock`, t.tokens[run.machine], { lock_token: run.grant.lock_token });
      await sleep(1000);
    }
    run.cdp.fire("Browser.close");
    // The reaper's teardown ends in either a new version or a logged failure.
    for (let i = 0; i < 600; i++) {
      await sleep(500);
      if ((await serverVersion(t)) !== v0) break;
      if (/could not tidy up/.test(logSince(run.machine, before))) break;
    }
  }
  run.cdp.close();
  const took = now() - started;
  const v1 = await serverVersion(t);
  const log = logSince(run.machine, before).replace(/\x1b\[[0-9;]*m/g, "");
  const problem = log.split("\n").find((l) => /WARN|ERROR/.test(l)) || "";
  return { took, uploaded: v1 !== v0, version: v1, problem: problem.trim() };
}

function profileOnDisk(machine, t) {
  const dir = path.join(MACHINES[machine].home, "profiles", t.profileId);
  let total = 0;
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.isFile()) total += fs.statSync(p).size;
    }
  };
  try { walk(dir); } catch {}
  return total;
}

// --- the run -----------------------------------------------------------------

async function setup() {
  const stamp = crypto.randomBytes(3).toString("hex");
  const email = `e2e-${stamp}@example.com`;
  const password = "correct-horse-battery-staple";
  const filler = Buffer.alloc(32, 1).toString("base64");
  const hex = Buffer.alloc(64, 1).toString("hex");
  const signup = await api("POST", "/v1/auth/signup", null, {
    email, password, org_name: `e2e ${stamp}`,
    public_key: filler, wrapped_private_key: filler, kdf_salt: filler, wrapped_ork: filler,
  });
  // Two sessions, one per machine, as two sign-ins would make.
  const second = await api("POST", "/v1/auth/login", null, { email, password, machine_name: "e2e machine B" });
  const project = await api("POST", "/v1/projects", signup.token, { name: "e2e" });
  const proxy = await api("POST", "/v1/proxies", signup.token, {
    name: "e2e", kind: "socks5", host: "127.0.0.1", port: 1080, credentials_enc: hex, wrapped_dek: hex,
  });
  const profile = await api("POST", `/v1/projects/${project.id}/profiles`, signup.token, {
    name: "e2e team profile", persona_id: PERSONA, fp_seed: "0123456789abcdef",
    timezone: "Europe/Berlin", languages: ["en-US", "en"], proxy_id: proxy.id,
  });
  return {
    tokens: { A: signup.token, B: second.token },
    projectId: project.id,
    profileId: profile.id,
    // Any 32 bytes: the server never sees it, and both machines must agree.
    profileKey: crypto.randomBytes(32).toString("hex"),
  };
}

const report = [];
const step = (row) => {
  report.push(row);
  console.log(`  -- ${Object.entries(row).map(([k, v]) => `${k}: ${v}`).join(", ")}`);
};

async function main() {
  const t = await setup();
  const proxy = await startProxy();
  t.proxyPort = proxy.address().port;
  console.log(`profile ${t.profileId}, persona ${PERSONA}\n`);

  console.log("== 1. A opens it for the first time, does some work, closes it with the agent");
  let a = await launch(t, "A");
  step({ step: "A launch", took: secs(a.took), pull: a.pulled.replace(/.*INFO /, "") });
  await work(a.cdp, "one");
  await setPref(a.cdp, "bookmark_bar.show_on_all_tabs", true);
  // A person does not close the window the instant they star a page.
  await sleep(4000);
  let c = await close(t, a, "agent");
  step({ step: "A close (agent)", took: secs(c.took), uploaded: c.uploaded, stored: mb(storedBytes(t)), on_disk: mb(profileOnDisk("A", t)) });
  claim(c.uploaded, "A's session reached the server");

  console.log("\n== 2. B opens it: is A's work there?");
  let b = await launch(t, "B");
  step({ step: "B launch", took: secs(b.took), pull: b.pulled.replace(/.*INFO /, "") });
  let s = await seen(b.cdp, "one");
  claim(s.bookmark, "B sees A's bookmark");
  claim(s.cookie, "B sees A's cookie");
  claim(s.storage, "B sees A's localStorage");
  claim((await readPref(b.cdp, "bookmark_bar.show_on_all_tabs")) === true, "B sees A's bookmark-bar setting");
  await work(b.cdp, "two");
  await sleep(4000);
  c = await close(t, b, "window");
  step({ step: "B close (window)", took: secs(c.took), uploaded: c.uploaded, problem: c.problem || "-" });
  claim(c.uploaded, "B's session reached the server after closing the window");

  console.log("\n== 3. A again: B's work, and how long a launch with a copy already here takes");
  a = await launch(t, "A");
  step({ step: "A launch", took: secs(a.took), pull: a.pulled.replace(/.*INFO /, "") });
  s = await seen(a.cdp, "two");
  claim(s.bookmark && s.cookie && s.storage, "A sees B's bookmark, cookie and storage");
  await work(a.cdp, "three");
  // Starred one second before closing: what survives a shutdown, not a timer.
  await sleep(1000);
  c = await close(t, a, "button");
  step({ step: "A close (Stop button, then window)", took: secs(c.took), uploaded: c.uploaded, problem: c.problem || "-" });
  claim(c.uploaded, "A's session reached the server after the desktop's Stop button");

  console.log("\n== 4. B again: is what A did before pressing Stop there?");
  b = await launch(t, "B");
  step({ step: "B launch", took: secs(b.took), pull: b.pulled.replace(/.*INFO /, "") });
  s = await seen(b.cdp, "three");
  claim(s.bookmark, "B sees the bookmark A made before pressing Stop");
  claim(s.cookie, "B sees the cookie A set before pressing Stop");
  c = await close(t, b, "agent");
  step({ step: "B close (agent)", took: secs(c.took), uploaded: c.uploaded });

  console.log("\n== 5. B once more, nothing changed in between");
  b = await launch(t, "B");
  step({ step: "B launch, same version", took: secs(b.took), pull: b.pulled.replace(/.*INFO /, "") });
  claim(/already holds/.test(b.pulled), "B did not unpack the server's copy over the version it had just uploaded");
  c = await close(t, b, "agent");
  step({ step: "B close (agent)", took: secs(c.took), uploaded: c.uploaded, stored: mb(storedBytes(t)) });

  console.log(`\n${failed === 0 ? "PASS" : "FAIL"} -- ${passed} ok, ${failed} false`);
  process.exitCode = failed === 0 ? 0 : 1;
}

main()
  .catch((e) => {
    console.error(e);
    process.exitCode = 2;
  })
  // The proxy and any socket left open would keep node alive.
  .finally(() => process.exit());
