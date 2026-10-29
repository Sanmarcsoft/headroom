// hrfwd: a loopback forward proxy that gives Claude Code headroom savings without setting
// ANTHROPIC_BASE_URL, which is what keeps /remote-control enabled (headroomlabs-ai/headroom#1779).
//
// CONNECT api.anthropic.com:443  -> TLS terminated here with the local CA leaf
//   POST /v1/messages            -> headroom sidecar, with x-headroom-proxy-token
//   everything else              -> the real api.anthropic.com, token stripped
// CONNECT anything else          -> blind TCP tunnel
//
// Fails closed: if headroom is unreachable a model call gets 502, never a direct fallback.
import { timingSafeEqual } from "node:crypto";
import { chmodSync, mkdirSync, rmSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import tls from "node:tls";
import { join } from "node:path";
import { INTERCEPT_HOST } from "./certs";

export type ForwardProxyOptions = {
  port?: number;
  /** Private 0700 dir for the TLS terminator's unix socket. */
  runDir: string;
  /** When set, CONNECT must carry Proxy-Authorization for any user with this password. */
  proxySecret?: string;
  leafKeyPem: string;
  leafCertPem: string;
  headroomUrl: string;
  headroomToken: string;
  upstreamOrigin?: string;
  upstreamCa?: string;
};

export type ForwardProxy = { host: string; port: number; innerPath: string; close(): Promise<void> };

const HOST = "127.0.0.1";
const TOKEN_HEADER = "x-headroom-proxy-token";
const HOP_BY_HOP = ["connection", "proxy-connection", "keep-alive", "proxy-authorization", "te", "trailer", "upgrade"];

/** CONNECT targets that get TLS-terminated: api.anthropic.com on 443, case- and trailing-dot-insensitive. */
export function isInterceptTarget(host: string, port: string): boolean {
  return host.toLowerCase().replace(/\.$/, "") === INTERCEPT_HOST && Number(port) === 443;
}

function hasSecret(header: string | undefined, secret: string): boolean {
  const m = /^Basic\s+(\S+)$/i.exec(header ?? "");
  if (!m) return false;
  const decoded = Buffer.from(m[1], "base64").toString("utf8");
  const pass = Buffer.from(decoded.slice(decoded.indexOf(":") + 1));
  const want = Buffer.from(secret);
  return pass.length === want.length && timingSafeEqual(pass, want);
}

function isModelCall(req: http.IncomingMessage): boolean {
  return req.method === "POST" && new URL(req.url ?? "/", "https://x").pathname === "/v1/messages";
}

function listen(server: net.Server, port: number): Promise<number> {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, HOST, () => resolve((server.address() as net.AddressInfo).port));
  });
}

export async function startForwardProxy(o: ForwardProxyOptions): Promise<ForwardProxy> {
  const headroom = new URL(o.headroomUrl);
  const upstream = new URL(o.upstreamOrigin ?? `https://${INTERCEPT_HOST}`);

  const inner = https.createServer({ key: o.leafKeyPem, cert: o.leafCertPem }, (req, res) => {
    const toHeadroom = isModelCall(req);
    const target = toHeadroom ? headroom : upstream;
    const headers = { ...req.headers };
    for (const h of [...HOP_BY_HOP, TOKEN_HEADER]) delete headers[h];
    headers.host = target.host;
    if (toHeadroom) headers[TOKEN_HEADER] = o.headroomToken;

    const send = target.protocol === "https:" ? https.request : http.request;
    const out = send(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port || (target.protocol === "https:" ? 443 : 80),
        method: req.method,
        path: req.url,
        headers,
        ...(target === upstream && o.upstreamCa ? { ca: o.upstreamCa } : {}),
      },
      (upRes) => {
        res.on("close", () => upRes.destroy());
        res.writeHead(upRes.statusCode ?? 502, upRes.headers);
        res.flushHeaders();
        upRes.pipe(res);
      },
    );
    out.on("error", (e) => {
      if (res.headersSent) return res.destroy(e);
      const who = toHeadroom ? "headroom" : "api.anthropic.com";
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ type: "error", error: { type: "api_error", message: `hrfwd: ${who} unreachable: ${e.message}` } }));
    });
    // A client that goes away (Esc in Claude) must cancel the upstream call, not leave it streaming.
    const abort = () => out.destroy();
    req.on("error", abort);
    res.on("close", () => { if (!res.writableFinished) abort(); });
    req.pipe(out);
  });

  // Upgrades (websockets) on the intercepted host go straight to the real upstream.
  inner.on("upgrade", (req, sock, head) => {
    const up = tls.connect({
      host: upstream.hostname,
      port: Number(upstream.port || 443),
      servername: upstream.hostname,
      ...(o.upstreamCa ? { ca: o.upstreamCa } : {}),
    }, () => {
      const lines = [`${req.method} ${req.url} HTTP/1.1`];
      for (let i = 0; i < req.rawHeaders.length; i += 2) {
        const k = req.rawHeaders[i];
        if (k.toLowerCase() === TOKEN_HEADER) continue;
        lines.push(`${k}: ${k.toLowerCase() === "host" ? upstream.host : req.rawHeaders[i + 1]}`);
      }
      up.write(lines.join("\r\n") + "\r\n\r\n");
      if (head.length) up.write(head);
      up.pipe(sock);
      sock.pipe(up);
    });
    up.on("error", () => sock.destroy());
    sock.on("error", () => up.destroy());
  });

  mkdirSync(o.runDir, { recursive: true, mode: 0o700 });
  chmodSync(o.runDir, 0o700);
  const innerPath = join(o.runDir, "inner.sock");
  rmSync(innerPath, { force: true });
  await new Promise<void>((resolve, reject) => {
    inner.once("error", reject);
    inner.listen(innerPath, () => resolve());
  });
  chmodSync(innerPath, 0o600);

  const outer = http.createServer((_req, res) => {
    res.writeHead(405, { "content-type": "text/plain" });
    res.end("hrfwd only speaks CONNECT\n");
  });
  outer.on("connect", (req, client: net.Socket, head: Buffer) => {
    const m = /^\[?([^\]]+?)\]?:(\d+)$/.exec(req.url ?? "");
    if (!m) return client.end("HTTP/1.1 400 Bad Request\r\n\r\n");
    if (o.proxySecret && !hasSecret(req.headers["proxy-authorization"], o.proxySecret)) {
      return client.end('HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="hrfwd"\r\n\r\n');
    }
    const [, host, port] = m;
    const onConnect = () => {
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) dest.write(head);
      dest.pipe(client);
      client.pipe(dest);
    };
    const dest = isInterceptTarget(host, port)
      ? net.connect(innerPath, onConnect)
      : net.connect(Number(port), host, onConnect);
    dest.on("error", () => client.end("HTTP/1.1 502 Bad Gateway\r\n\r\n"));
    client.on("error", () => dest.destroy());
  });

  const port = await listen(outer, o.port ?? 0);
  const close = (s: net.Server) => new Promise<void>((r) => s.close(() => r()));
  return {
    host: HOST,
    port,
    innerPath,
    async close() {
      (outer as any).closeAllConnections?.();
      (inner as any).closeAllConnections?.();
      await Promise.all([close(outer), close(inner)]);
      rmSync(innerPath, { force: true });
    },
  };
}

// CLI: bun hrfwd.ts --run-dir <private dir> [--port N]
// Reads the headroom URL and token the same way ccs does, and the CONNECT secret from
// HRFWD_SECRET (env, never argv). Mints a fresh CA in <run-dir>/ca, puts the TLS terminator's
// unix socket in <run-dir>, and writes the bound port to <run-dir>/port once listening.
// Exits when its parent launcher dies, even by SIGKILL, so it can never outlive the session.
if (import.meta.main) {
  const { readFileSync, writeFileSync, lstatSync } = await import("node:fs");
  const { ensureCa } = await import("./certs");
  const arg = (name: string) => {
    const i = process.argv.indexOf(name);
    return i > 0 ? process.argv[i + 1] : undefined;
  };
  const die = (msg: string): never => {
    console.error(`hrfwd: ${msg}`);
    process.exit(1);
  };
  const home = process.env.HOME ?? die("HOME unset");
  const tokenFile = process.env.HEADROOM_PROXY_TOKEN_FILE ?? `${home}/.config/headroom/proxy-token`;
  const st = lstatSync(tokenFile, { throwIfNoEntry: false }) ?? die(`token file ${tokenFile} missing`);
  if (!st.isFile()) die(`token file ${tokenFile} is not a regular file`);
  if (st.uid !== process.getuid!()) die(`token file ${tokenFile} is not owned by us`);
  if ((st.mode & 0o777) !== 0o600) die(`token file ${tokenFile} must be mode 600`);
  const token = readFileSync(tokenFile, "utf8").trim();
  if (!/^[0-9a-f]{64}$/.test(token)) die(`token file ${tokenFile} is not 64 hex chars`);
  const secret = process.env.HRFWD_SECRET ?? die("HRFWD_SECRET is required");
  if (secret.length < 32) die("HRFWD_SECRET must be at least 32 chars");

  const runDir = arg("--run-dir") ?? die("--run-dir is required");
  const ca = await ensureCa(join(runDir, "ca"));
  const proxy = await startForwardProxy({
    port: Number(arg("--port") ?? 0),
    runDir,
    proxySecret: secret,
    leafKeyPem: ca.leafKeyPem,
    leafCertPem: ca.leafCertPem,
    headroomUrl: process.env.HEADROOM_PROXY_URL ?? "http://headroom:8787",
    headroomToken: token,
  });
  writeFileSync(join(runDir, "port"), `${proxy.port}\n`, { mode: 0o600 });
  console.error(`hrfwd: listening ${proxy.host}:${proxy.port}, CA ${ca.caCertPath}`);

  const parent = process.ppid;
  const shutdown = () => {
    setTimeout(() => process.exit(0), 2000).unref();
    void proxy.close().then(() => process.exit(0));
  };
  for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"] as const) process.on(sig, shutdown);
  // setsid does not reparent, so a dead launcher shows up as a changed ppid.
  setInterval(() => { if (process.ppid !== parent) shutdown(); }, 1000).unref();
}
