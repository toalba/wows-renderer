# Security Policy

## Supported Versions

While the project is pre-1.0, only the `master` branch receives security
fixes. Once tagged releases begin, the most recent minor line will be
supported.

## Reporting a Vulnerability

**Do not file public GitHub issues for security problems.**

Prefer the private channel:

1. GitHub → this repo → **Security → Report a vulnerability** (GitHub
   Security Advisories, maintainer-only visibility).
2. Fallback email: **tb@kleinundpartner.at**.

Please include:
- A description of the issue and its impact.
- Steps to reproduce, ideally with a minimal proof-of-concept.
- Affected version(s) / commit SHA / Docker image tag.
- Any mitigations you're aware of.

The bot ingests user-uploaded `.wowsreplay` files and parses them via
`wows-replay-parser`. Parser bugs that allow arbitrary code execution,
path traversal, or resource exhaustion via a crafted replay are
in-scope and should be reported privately.

## Response

- Acknowledgement within **72 hours**.
- Triage and severity assessment within **7 days**.
- Fix or mitigation for confirmed vulnerabilities within **30 days**,
  faster for high-severity issues.

Coordinated disclosure is expected: please allow a reasonable window for
a fix to ship before going public. Credit is given in release notes
unless the reporter prefers anonymity.

## Out of Scope

- End-user compliance with Wargaming's EULA for the World of Warships
  client or for the gamedata you extract from your local install.
- Integrity of the `wows-gamedata` repository (separate project).
- Discord-platform-level abuse (use Discord's in-app reporting).
