// Local CA for hrfwd, minted per session by the launcher. It is name-constrained to
// api.anthropic.com (and its subdomains, inherent to DNS constraints) with every IP address
// excluded, so a stolen key cannot impersonate any other host. Keys are 0600 in a 0700 dir.
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const INTERCEPT_HOST = "api.anthropic.com";

export type CaBundle = {
  caCertPath: string;
  caKeyPath: string;
  caCertPem: string;
  leafKeyPem: string;
  leafCertPem: string;
};

const openssl = (args: string[]) => execFileSync("openssl", args, { stdio: ["ignore", "ignore", "pipe"] });

export async function ensureCa(dir: string): Promise<CaBundle> {
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  const caKey = join(dir, "ca.key");
  const caCert = join(dir, "ca.pem");
  const leafKey = join(dir, "leaf.key");
  const leafCsr = join(dir, "leaf.csr");
  const leafCert = join(dir, "leaf.pem");
  const leafExt = join(dir, "leaf.ext");

  if (!existsSync(caKey) || !existsSync(caCert)) {
    openssl([
      "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
      "-keyout", caKey, "-out", caCert, "-days", "90",
      "-subj", "/CN=hrfwd local CA (api.anthropic.com only)",
      "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
      "-addext", "keyUsage=critical,keyCertSign,cRLSign",
      // DNS constraints alone leave IP SANs unconstrained, so exclude every IPv4 and IPv6 address.
      "-addext", `nameConstraints=critical,permitted;DNS:${INTERCEPT_HOST},excluded;IP:0.0.0.0/0.0.0.0,excluded;IP:0:0:0:0:0:0:0:0/0:0:0:0:0:0:0:0`,
    ]);
  }
  if (!existsSync(leafKey) || !existsSync(leafCert)) {
    openssl([
      "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
      "-keyout", leafKey, "-out", leafCsr, "-subj", `/CN=${INTERCEPT_HOST}`,
    ]);
    writeFileSync(leafExt, [
      `subjectAltName=DNS:${INTERCEPT_HOST}`,
      "basicConstraints=critical,CA:FALSE",
      "keyUsage=critical,digitalSignature",
      "extendedKeyUsage=serverAuth",
      "",
    ].join("\n"));
    openssl([
      "x509", "-req", "-in", leafCsr, "-CA", caCert, "-CAkey", caKey, "-CAcreateserial",
      "-out", leafCert, "-days", "90", "-extfile", leafExt,
    ]);
  }
  for (const f of [caKey, leafKey]) chmodSync(f, 0o600);

  return {
    caCertPath: caCert,
    caKeyPath: caKey,
    caCertPem: readFileSync(caCert, "utf8"),
    leafKeyPem: readFileSync(leafKey, "utf8"),
    leafCertPem: readFileSync(leafCert, "utf8"),
  };
}
