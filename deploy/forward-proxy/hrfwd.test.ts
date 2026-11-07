// Tests for hrfwd, the forward proxy that gives a session headroom savings without
// setting ANTHROPIC_BASE_URL (so /remote-control stays enabled). Claim IDs refer to ISA.md.
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ensureCa } from "./certs";
import { isInterceptTarget, startForwardProxy } from "./hrfwd";

const TOKEN = "a".repeat(64);
const SECRET = "s3cret-per-session";
const dir = mkdtempSync(join(tmpdir(), "hrfwd-test-"));

type Seen = { method: string; url: string; headers: http.IncomingHttpHeaders; body: string };
const headroomSeen: Seen[] = [];
const upstreamSeen: Seen[] = [];

function record(into: Seen[], req: http.IncomingMessage): Promise<Seen> {
  return new Promise((resolve) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const s = { method: req.method!, url: req.url!, headers: req.headers, body };
      into.push(s);
      resolve(s);
    });
  });
}

// A throwaway CA for the fake upstream, deliberately NOT the proxy's CA.
function upstreamCert() {
  const key = join(dir, "up.key");
  const crt = join(dir, "up.pem");
  execFileSync("openssl", [
    "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
    "-keyout", key, "-out", crt, "-days", "1", "-subj", "/CN=localhost",
    "-addext", "subjectAltName=DNS:localhost",
  ], { stdio: "ignore" });
  return { key: readFileSync(key, "utf8"), cert: readFileSync(crt, "utf8") };
}

let ca: Awaited<ReturnType<typeof ensureCa>>;
let up: { key: string; cert: string };
let headroom: http.Server;
let upstream: https.Server;
let headroomPort = 0;
let upstreamPort = 0;
let proxy: Awaited<ReturnType<typeof startForwardProxy>>;
let releaseStream: () => void = () => {};
let headroomClosedAt = 0;

function listen(s: net.Server): Promise<number> {
  return new Promise((r) => s.listen(0, "127.0.0.1", () => r((s.address() as net.AddressInfo).port)));
}

beforeAll(async () => {
  ca = await ensureCa(join(dir, "ca"));
  up = upstreamCert();

  headroom = http.createServer(async (req, res) => {
    await record(headroomSeen, req);
    if (req.url?.includes("stream")) {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.on("close", () => (headroomClosedAt = Date.now()));
      res.write("event: first\ndata: 1\n\n");
      await new Promise<void>((r) => (releaseStream = r));
      res.end("event: last\ndata: 2\n\n");
      return;
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ via: "headroom" }));
  });
  headroomPort = await listen(headroom);

  upstream = https.createServer({ key: up.key, cert: up.cert }, async (req, res) => {
    await record(upstreamSeen, req);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ via: "upstream" }));
  });
  upstreamPort = await listen(upstream);

  proxy = await startForwardProxy({
    port: 0,
    runDir: join(dir, "run"),
    proxySecret: SECRET,
    leafKeyPem: ca.leafKeyPem,
    leafCertPem: ca.leafCertPem,
    headroomUrl: `http://127.0.0.1:${headroomPort}`,
    headroomToken: TOKEN,
    upstreamOrigin: `https://localhost:${upstreamPort}`,
    upstreamCa: up.cert,
  });
});

afterAll(async () => {
  releaseStream();
  await proxy?.close();
  headroom?.close();
  upstream?.close();
  rmSync(dir, { recursive: true, force: true });
});

const viaProxy = (url: string, init: RequestInit = {}, trust = ca?.caCertPem) =>
  fetch(url, { ...init, proxy: `http://hrfwd:${SECRET}@127.0.0.1:${proxy.port}`, tls: { ca: trust } } as RequestInit);

describe("hrfwd", () => {
  test("C1: POST /v1/messages goes to headroom with the token and an intact body", async () => {
    const body = JSON.stringify({ model: "x", messages: [{ role: "user", content: "hi" }] });
    const res = await viaProxy("https://api.anthropic.com/v1/messages?beta=true", {
      method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer oauth-abc" },
      body,
    });
    expect(await res.json()).toEqual({ via: "headroom" });
    const seen = headroomSeen.at(-1)!;
    expect(seen.url).toBe("/v1/messages?beta=true");
    expect(seen.body).toBe(body);
    expect(seen.headers["x-headroom-proxy-token"]).toBe(TOKEN);
    expect(seen.headers["authorization"]).toBe("Bearer oauth-abc");
  });

  test("C2: other api.anthropic.com paths go to the real upstream, not headroom", async () => {
    const before = headroomSeen.length;
    const res = await viaProxy("https://api.anthropic.com/v1/sessions");
    expect(await res.json()).toEqual({ via: "upstream" });
    expect(upstreamSeen.at(-1)!.url).toBe("/v1/sessions");
    expect(headroomSeen.length).toBe(before);
    // GET on the messages path is not a model call either.
    await viaProxy("https://api.anthropic.com/v1/messages/count_tokens", { method: "POST", body: "{}" });
    expect(upstreamSeen.at(-1)!.url).toBe("/v1/messages/count_tokens");
  });

  test("C3: the headroom token never reaches the upstream, even if the client sends one", async () => {
    await viaProxy("https://api.anthropic.com/v1/environments", {
      headers: { "x-headroom-proxy-token": "leaked" },
    });
    for (const s of upstreamSeen) expect(s.headers["x-headroom-proxy-token"]).toBeUndefined();
    // A client-sent token on the headroom path is replaced, not duplicated.
    await viaProxy("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "x-headroom-proxy-token": "leaked" },
      body: "{}",
    });
    expect(headroomSeen.at(-1)!.headers["x-headroom-proxy-token"]).toBe(TOKEN);
  });

  test("C4: CONNECT to any other host is a blind tunnel", async () => {
    const before = upstreamSeen.length;
    // Trusting only the upstream's own CA proves the proxy did not re-sign the connection.
    const res = await viaProxy(`https://localhost:${upstreamPort}/anything`, {}, up.cert);
    expect(await res.json()).toEqual({ via: "upstream" });
    expect(upstreamSeen.length).toBe(before + 1);
  });

  test("C5: headroom down means 502, never a fallback to the upstream", async () => {
    const dead = await startForwardProxy({
      port: 0,
      leafKeyPem: ca.leafKeyPem,
      leafCertPem: ca.leafCertPem,
      runDir: join(dir, "run-dead"),
      headroomUrl: "http://127.0.0.1:1",
      headroomToken: TOKEN,
      upstreamOrigin: `https://localhost:${upstreamPort}`,
      upstreamCa: up.cert,
    });
    const before = upstreamSeen.length;
    try {
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        body: "{}",
        proxy: `http://127.0.0.1:${dead.port}`,
        tls: { ca: ca.caCertPem },
      } as RequestInit);
      expect(res.status).toBe(502);
      expect(upstreamSeen.length).toBe(before);
    } finally {
      await dead.close();
    }
  });

  test("C6: SSE streams incrementally", async () => {
    const res = await viaProxy("https://api.anthropic.com/v1/messages?stream", {
      method: "POST",
      body: "{}",
    });
    const reader = res.body!.getReader();
    const first = await Promise.race([
      reader.read(),
      new Promise<never>((_, rej) => setTimeout(() => rej(new Error("first event did not stream")), 2000)),
    ]);
    expect(new TextDecoder().decode(first.value)).toContain("event: first");
    releaseStream();
    let rest = "";
    for (let r = await reader.read(); !r.done; r = await reader.read()) rest += new TextDecoder().decode(r.value);
    expect(rest).toContain("event: last");
  });

  test("C7: the CA is name-constrained to api.anthropic.com", () => {
    const caFile = join(dir, "ca-only.pem");
    writeFileSync(caFile, ca.caCertPem);
    const verify = (name: string) => {
      const f = name.replace(/[^a-z0-9.]/gi, "_");
      const key = join(dir, `${f}.key`);
      const csr = join(dir, `${f}.csr`);
      const crt = join(dir, `${f}.pem`);
      execFileSync("openssl", ["req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", key, "-out", csr, "-subj", name.startsWith("IP:") ? "/O=ip-probe" : `/CN=${f}`], { stdio: "ignore" });
      const ext = join(dir, `${f}.ext`);
      writeFileSync(ext, `subjectAltName=${name.startsWith("IP:") ? name : `DNS:${name}`}\n`);
      execFileSync("openssl", ["x509", "-req", "-in", csr, "-CA", caFile, "-CAkey", ca.caKeyPath,
        "-CAcreateserial", "-out", crt, "-days", "1", "-extfile", ext], { stdio: "ignore" });
      try {
        execFileSync("openssl", ["verify", "-CAfile", caFile, crt], { stdio: "ignore" });
        return true;
      } catch {
        return false;
      }
    };
    expect(verify("api.anthropic.com")).toBe(true);
    expect(verify("evil.example.com")).toBe(false);
    expect(verify("IP:127.0.0.1")).toBe(false);
  });

  test("C7b: the CA key is mode 0600", () => {
    const mode = execFileSync("stat", ["-c", "%a", ca.caKeyPath]).toString().trim();
    expect(mode).toBe("600");
  });

  test("C12: a client abort tears down the headroom request", async () => {
    headroomClosedAt = 0;
    const ac = new AbortController();
    const res = await viaProxy("https://api.anthropic.com/v1/messages?stream-abort", {
      method: "POST",
      body: "{}",
      signal: ac.signal,
    });
    const reader = res.body!.getReader();
    await reader.read();
    const abortedAt = Date.now();
    ac.abort();
    await new Promise((r) => setTimeout(r, 500));
    expect(headroomClosedAt).toBeGreaterThan(0);
    expect(headroomClosedAt - abortedAt).toBeLessThan(500);
    releaseStream();
  });

  test("C13: CONNECT without the session secret gets 407", async () => {
    const reply = await new Promise<string>((resolve) => {
      const s = net.connect(proxy.port, "127.0.0.1", () =>
        s.write("CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n"));
      s.once("data", (d) => { resolve(d.toString()); s.destroy(); });
    });
    expect(reply).toStartWith("HTTP/1.1 407");
    const before = headroomSeen.length;
    let status = 0;
    try {
      status = (await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST", body: "{}", proxy: `http://hrfwd:wrong@127.0.0.1:${proxy.port}`, tls: { ca: ca.caCertPem },
      } as RequestInit)).status;
    } catch { status = -1; }
    expect([407, -1]).toContain(status);
    expect(headroomSeen.length).toBe(before);
  });

  test("C14: the TLS terminator is a unix socket, not a TCP port", () => {
    expect(statSync(proxy.innerPath).isSocket()).toBe(true);
    expect(statSync(proxy.innerPath).mode & 0o077).toBe(0);
  });

  test("C15: intercept matching ignores case and a trailing dot, and requires port 443", () => {
    expect(isInterceptTarget("API.Anthropic.COM", "443")).toBe(true);
    expect(isInterceptTarget("api.anthropic.com.", "443")).toBe(true);
    expect(isInterceptTarget("api.anthropic.com", "0443")).toBe(true);
    expect(isInterceptTarget("api.anthropic.com", "8443")).toBe(false);
    expect(isInterceptTarget("evil.api.anthropic.com", "443")).toBe(false);
  });

  test("C8: hrfwd listens on 127.0.0.1 only", () => {
    expect(proxy.host).toBe("127.0.0.1");
  });
});
