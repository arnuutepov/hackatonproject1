# Backend security scope · 0.10.0

This is an existing support service, not a new clone. No architecture file,
SSO tenant, game credentials, payments or email accounts were supplied.
Implementation and deployment boundaries: [../docs/security.md](../docs/security.md).

- [x] Personal operator identities; scrypt password hashes; no default credential.
- [x] Revocable HttpOnly/Secure/SameSite sessions; login/logout-all; absolute expiry.
- [x] Server-side agent/lead/admin permissions and actor attribution.
- [x] Authentication required for current and future HTTP routes by default.
- [x] Strict input validation, mandatory Origin on cookie-authenticated writes.
- [x] Persistent login throttling and bounded password-check concurrency.
- [x] HMAC game signatures, timestamp, persistent nonce, idempotent event IDs.
- [x] Synthetic tests for unauthenticated access, cross-role abuse, impersonation,
      replay, body tampering and actual uvicorn transport.
- [x] OIDC JWT/MFA claim verification and pinned Caddy/oauth2-proxy deployment configuration.
- [x] Read-only CI security gate and reviewable branch-protection apply script.
- [ ] Live organizational MFA enrollment, DNS/TLS, GitHub protection: owner/admin prerequisites pending.
- [ ] External delivery, game profile lookup, payments/email: out of current scope.
- [ ] Production backup retention, disaster recovery and independent penetration test.

Secrets are configured by the owner. The agent has not created production
accounts/keys or changed GitHub branch protection. `.env.example` has no secrets.
