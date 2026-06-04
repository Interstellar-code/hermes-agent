# Review Lens: Security

Primary focus for this review: **security**. Weight your findings toward the
risks below, but never ignore an unrelated BLOCKER you happen to see.

## What to scrutinize

- **Authentication & authorization** — missing/weak auth checks, broken access
  control, privilege escalation, IDOR (acting on objects without verifying the
  caller owns them), missing checks on state-changing endpoints.
- **Injection** — SQL/NoSQL/command/template/LDAP injection, unsanitized input
  reaching an interpreter, string-built queries or shell commands, unsafe
  deserialization (`pickle`, `yaml.load`, `eval`/`exec`).
- **Secrets** — hardcoded credentials, API keys or tokens in source/config,
  secrets logged or echoed, secrets committed to the repo, keys with overly
  broad scope.
- **Unsafe defaults** — debug mode on in prod, permissive CORS, disabled TLS
  verification, world-writable files, default/empty passwords, verbose error
  pages leaking internals.
- **Input validation & output encoding** — missing validation on untrusted
  input, missing output encoding/escaping (XSS), path traversal, SSRF,
  unbounded input (DoS), missing size/rate limits.
- **Dependency risk** — known-vulnerable or unpinned dependencies, untrusted
  sources, supply-chain exposure.
- **Crypto & data handling** — weak/rolled-your-own crypto, predictable
  randomness for security tokens, sensitive data unencrypted at rest/in
  transit, PII in logs.

## Severity guidance (apply the shared rubric)

- **BLOCKER** — remote auth bypass, RCE, SQL injection, secret leak, or any
  exploitable path to data compromise on the main path.
- **HIGH** — real, likely-exploitable weakness (e.g. missing authz on a
  sensitive action, stored XSS, SSRF) that is not yet catastrophic.
- **MED** — a defense-in-depth gap or edge-case weakness that needs a specific
  precondition to exploit.
- **LOW / NIT** — hardening opportunities and minor hygiene (e.g. tighter
  headers, clearer error messages) with limited blast radius.

When unsure between two levels, pick the higher and note the uncertainty in the
Finding's evidence. Prefer fewer, well-evidenced findings over speculation.
