# Security policy

## Reporting a vulnerability
Please report security problems **privately**, using "Report a vulnerability" on the Security tab of this repository
(GitHub private vulnerability reporting), not in a public issue. Include what you found, how to reproduce it and the
version (the image tag `sha-<8>` or the commit). You will get an answer as soon as the maintainer can; this is a hobby
project without a service-level promise.

## Scope and assumptions
* The Web UI is meant for a **trusted home LAN only**: one password, plain HTTP unless you configure TLS. Do not expose it
  to the internet, and do not forward its port. Reports that depend on exposing it to the internet are out of scope.
* Supported: the latest commit on `main`.
* The service holds your INNR account password (as a Docker secret file) and talks to the INNR cloud. Never put secrets
  into an issue, a pull request or a log excerpt; the project's logs scrub known secrets, but check before you share them.

## Hardening already in place
Non-root read-only container, dropped capabilities, hashed server-side sessions, CSRF protection on every change, a
strict content security policy, deny-by-default routes verified by tests, and secrets never rendered or logged. Details:
`.kiro/specs/innr-auto-off/design.md`, section 17.
