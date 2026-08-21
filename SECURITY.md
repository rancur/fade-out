# Security

## Reporting a vulnerability

Please report security issues privately through
[GitHub's private vulnerability reporting](https://github.com/rancur/fade-out/security/advisories/new)
rather than opening a public issue.

Expect an initial response within a few days. This is a hobby project maintained
by one person — please size your expectations accordingly.

## The single most important thing to know

**fade-out has no authentication. Do not expose it directly to the internet.**

There is no login, no API key, no session — every endpoint is reachable by
anyone who can reach the port. That includes endpoints that read and write
stored credentials:

- `GET /api/auth/credentials` lists which credentials are configured and returns
  masked values (first 6 and last 4 characters).
- `PUT /api/auth/credentials` overwrites stored credentials.
- `POST /api/mixes/{id}/upload` and the pipeline endpoints publish to your real
  SoundCloud and YouTube accounts.

This is a deliberate design point, not an oversight: fade-out is built as a
single-user tool on a trusted home network. But it means the deployment model is
your only security boundary.

### Deploy it behind something

Safe:

- Bound to a LAN the app can trust, reachable only from inside it.
- Behind a reverse proxy that enforces authentication (Authelia, oauth2-proxy,
  Cloudflare Access, basic auth).
- On a private overlay network such as Tailscale or WireGuard.

Not safe:

- Port-forwarded straight from your router.
- A Cloudflare Tunnel with no Access policy in front of it.
- Any public hostname that resolves to the container without an auth layer.

### The OAuth callback exception

YouTube's OAuth flow requires a real, publicly resolvable domain, which is why
`PUBLIC_URL` exists. Publishing a hostname for the OAuth callback does **not**
mean publishing the dashboard: put the auth layer in front of everything, and if
your proxy needs an exception for the callback, scope it to the
`/api/auth/*/callback` paths only.

### CORS

`CORS_ALLOW_ORIGINS` defaults to the local Vite dev-server origins, with
`PUBLIC_URL` folded in automatically. The dashboard is served from the same
origin as the API, so most deployments need no additional entry.

Setting it to `*` is strongly discouraged. With no authentication in front of
the API, a wildcard lets any website you happen to visit read your stored
credential metadata and drive your pipeline from your own browser. If `*` is
set, credentialed CORS is disabled automatically, but the request-forgery
exposure remains.

## Secrets

- Secrets come from the environment or the settings database. Nothing is
  committed — `.env` is gitignored, and `gitleaks` runs as a pre-commit hook
  (`.pre-commit-config.yaml`).
- Credential values are masked on read, never returned in full.
- `GITHUB_TOKEN` is only ever used to read release and tag metadata for the
  deployment-freshness check. Use a fine-grained token scoped to this one
  repository with *Contents: Read-only*, and nothing else.

## Container

The image runs as root so that bind-mounted NAS shares keep working out of the
box across the many UID/GID setups people deploy on. If your mounts permit it,
running unprivileged is better — uncomment the `user:` line in
`docker-compose.yml` and set it to a UID/GID that can read the watch folders and
write the output folders.

## Automation

The workflows under `.github/workflows/` run Claude with write access to this
repository. `claude.yml` is gated on `author_association`, so only the owner,
org members, and invited collaborators can trigger it — pull requests from forks
do not receive repository secrets. Please keep that gate in place in any PR that
touches those files.
