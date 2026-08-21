# Contributing to fade-out

Thanks for taking a look. This is a personal project released in the hope it is
useful to other DJs — issues and pull requests are welcome, with the honest
caveat that it is maintained by one person in spare time.

## Before you start

For anything larger than a bug fix, please open an issue first. It saves you
building something that does not fit, and saves me reviewing it.

## Development setup

You need **Python 3.12**, Node 20, and `ffmpeg` on your PATH.

> Python 3.13 and newer will not work. They removed the stdlib `audioop`
> module, which `pydub` — pulled in by `shazamio` — imports at start-up. The
> Docker image pins 3.12 for this reason.

```bash
# Backend
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest            # 993 tests, ~45s

# Frontend
cd frontend
npm ci
npm test
npx tsc --noEmit
npm run build
```

To run the app itself, see the Quick Start in [README.md](README.md).

## Before you open a pull request

Everything below runs in CI, so it is faster to run it locally first:

```bash
cd backend  && python -m pytest -q
cd frontend && npx tsc --noEmit && npm test && npm run build
```

Please also:

- **Add tests** for new behaviour. The backend suite is thorough and it is worth
  keeping it that way.
- **Use conventional commits** — `feat:`, `fix:`, `chore:`, `docs:`,
  `refactor:`, `test:`.
- **Document new environment variables** in both `.env.example` and the README
  configuration table.
- **Never commit secrets.** `gitleaks` runs as a pre-commit hook:
  `pip install pre-commit && pre-commit install`.
- **Keep pull requests focused.** One concern per PR reviews far better than a
  large mixed change.

## Things to be careful with

- **Don't loosen the CORS default or the workflow trust gate.** Both are
  deliberate and both are explained in [SECURITY.md](SECURITY.md).
- **Uploads hit real accounts.** Tests must never make live calls to SoundCloud,
  YouTube, or Mixcloud — mock the client. Every existing test does.
- **Watch folders are other people's archives.** Source-file renaming is off by
  default and gated on a read-write mount; keep changes there conservative.

## Reporting security issues

Please do not open a public issue — see [SECURITY.md](SECURITY.md).
