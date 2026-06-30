# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Instead, report privately via GitHub's
[Security Advisories](https://github.com/Rocho-EL-Locho/soundpull/security/advisories/new)
(Security → Report a vulnerability), or contact the maintainer directly.

Please include steps to reproduce and the affected version/commit. You'll get an
acknowledgement as soon as possible.

## Scope notes

Soundpull is meant to run **behind authentik (OIDC)** and a TLS-terminating reverse
proxy. Secrets (session key, Fernet key, OIDC client secret, WebDAV passwords) are
configured via environment and stored encrypted at rest where applicable — never
commit real secrets to the repository.
