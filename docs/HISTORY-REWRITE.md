# History rewrite, 2026-10-21

Every commit SHA in this repository changed on 2026-10-21. If you hold a clone
from before that date, re-sync it (below). Don't push old commits back: that
puts the removed material back in public history.

## Why

The repo is public and the owner wants it to stay public. Older history
contained three kinds of material that shouldn't be public:

1. Links to private AI coding sessions, carried in `Claude-Session:` trailer
   lines in commit messages.
2. The names of a real password-manager vault and item, which an old
   `scripts/set-github-token.sh` used as defaults. Those defaults were already
   removed going forward. The rewrite removes them from history too.
3. Details of one real copyright claim against one real mix (its title, video
   ID and blocked territories), used in comments and test fixtures for the
   availability watchdog. The fixtures now use neutral placeholders. The
   tests and their behaviour are unchanged.

Everything else was kept. Every commit message keeps its real content (only
the trailer lines above came out), and `Co-Authored-By` lines, docs,
CHANGELOG, handoff notes, authors and dates are untouched. Commits whose
trees changed: the four watchdog/uploader files in `backend/` and
`scripts/set-github-token.sh`, in the commits that touched them.

## How

- Tool: `git filter-repo` (a40bce548d2c).
- `--message-callback`: drops lines that are only a session link, a
  `Claude-Session:` trailer or a "Generated with" trailer, and collapses the
  blank lines that leaves behind.
- `--replace-text` and `--replace-message`: a short list of literal
  replacements for categories 2 and 3 above. The list is kept privately with
  the owner because it names the removed strings.
- Scope: all branches and all tags. Annotated tags were rewritten and their
  releases still attach to the same tag names.
- A full pre-rewrite mirror and bundle are kept privately by the owner, off
  GitHub.
- Before the push, the backend test suite and `ruff` passed on the rewritten
  `main`. After the push, a fresh clone had 0 matches for any removed string
  across `git log --all`, every blob in every commit, and every tag.

## What still holds the old commits

GitHub keeps old commits reachable through the read-only `refs/pull/*` refs
of past pull requests, and through its caches, so an old SHA URL may still
open. A purge request to GitHub Support covers that. The rewrite can't reach
it.

## Re-syncing an old clone

```sh
git fetch origin --prune --force '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
git reset --keep origin/<your-branch>   # per branch that has no local-only work
```

For a branch with local-only commits, rebase those commits onto the new
branch (`git rebase --onto origin/<branch> <old-base> <branch>`) or re-clone.

## Branch and tag map (old -> new)

| ref | old | new |
|---|---|---|
| `heads/chore/npm-audit-backlog` | `f2d4184cc843` | `f6e97f077f1c` |
| `heads/dependabot/npm_and_yarn/frontend/multi-d49ef80a9c` | `c7a99242a438` | `8fb4a699aabb` |
| `heads/dependabot/npm_and_yarn/frontend/source-map-js-1.2.2` | `1206e364c959` | `7233cb70d1b1` |
| `heads/deploy` | `2171b7e4f272` | `068da8778b8a` |
| `heads/feat/acoustid-fallback-tighter-sampling` | `29f3455eb9ca` | `eacee0500108` |
| `heads/feat/activity-log-and-robustness` | `139a7f028b83` | `c49857616a09` |
| `heads/feat/backfill-safe-unattended` | `81568d55b9f7` | `fb0dea5b9ddd` |
| `heads/feat/catalog-tracklist-backfill` | `23d408f83ef9` | `79142c1d9e3a` |
| `heads/feat/catalog-unified-titles` | `218a5ae4e83c` | `ba973b27fb44` |
| `heads/feat/fade-out-metadata-thumbnail` | `3bb57d0c0694` | `bcdc1e707676` |
| `heads/feat/settings-overhaul` | `84cca2766172` | `1363a4588736` |
| `heads/feat/shorts-uploader` | `7103d6800ac0` | `fe968372e513` |
| `heads/feat/sorting-and-title-diversity` | `387049390883` | `7bb58f465637` |
| `heads/feat/source-file-metadata` | `4a87bbfc2bed` | `f286e0518b33` |
| `heads/feat/thumbnail-system-and-playlists` | `377bc214ec8e` | `06dd29dde653` |
| `heads/feat/unified-genre-titles` | `329033265472` | `e2c51f7babd5` |
| `heads/feat/uniqueness-engine` | `efa9177e62ef` | `c3ace5bc7e39` |
| `heads/feat/youtube-publish-modes` | `cd55aad52b04` | `7b125631abef` |
| `heads/fix/alert-on-dead-platform-credentials` | `1df522599d60` | `d544d14df990` |
| `heads/fix/backfill-session-scope` | `b8159cffeec1` | `ee53c8e9e156` |
| `heads/fix/credential-probe-safety` | `f9207455402e` | `fa1b06322a5b` |
| `heads/fix/cue-guards-shazam-sc-refresh` | `17be7d61184e` | `5a8ddeb4b18d` |
| `heads/fix/decouple-platform-uploads` | `eccf99be57c2` | `c3f7118df4f7` |
| `heads/fix/dependency-audit` | `cc2ad33484f1` | `3e82debe4a12` |
| `heads/fix/oauth-redirect-and-browser-fallback` | `bc21ae4d8826` | `7ffd70c6b456` |
| `heads/fix/pipeline-lock-hygiene` | `d131b9f6c286` | `81d2b5381ed1` |
| `heads/fix/playlists-live-hardening` | `3f30163094c0` | `8a45063d2e23` |
| `heads/fix/publish-modes-drop-premiere` | `4ea752fca401` | `5d5f78ae9ce5` |
| `heads/fix/shorts-metadata-gate-and-autoresume` | `e046cfb26132` | `96bac5c78d4a` |
| `heads/fix/shorts-one-to-one-dedupe` | `55fc62fc5488` | `263c0ca3ed57` |
| `heads/fix/title-series-identity` | `b57a3dd39443` | `5d7e84722e76` |
| `heads/fix/wire-file-watcher-autoingest` | `352daf95a8ed` | `e3fe07d5a1f0` |
| `heads/fix/youtube-oauth-public-url` | `0c85dbff004b` | `85143550493a` |
| `heads/fix/youtube-region-block-detection` | `e81728de937c` | `3b31b13f5ca6` |
| `heads/fix/yt-apply-snippet-race` | `64f2303bf45a` | `2e223184f2c9` |
| `heads/main` | `5f0dd67e9ba7` | `355fbb3fdaf7` |
| `heads/v2` | `bbc1f179a8d9` | `02bbea4b3811` |
| `tags/v1.0.0` | `c15b9593cde7` | `d7c7822e34b2` |
| `tags/v2.0.0` | `54f5926203e8` | `eb290764a905` |
| `tags/v2.1.0` | `f18b0371a3f0` | `76470f721f32` |
| `tags/v2.2.0` | `985000e98256` | `0d0ec690dad2` |
| `tags/v2.3.0` | `0e02288b3456` | `55b50821dad8` |
| `tags/v2.4.0` | `7e525f62ea8f` | `a979172dd6a5` |
| `tags/v2.4.1` | `2c2d675d8ac4` | `226a4f54521c` |
| `tags/v2.5.0` | `c3bcf0c63e4d` | `b50e2a8f57be` |
| `tags/v2.6.1` | `bc286c159899` | `935deaf1bcd5` |
