# Repos, CI and beta releases (runbook)

Two private GitHub repos under `kaushalag29` (names in `product.toml` → `[repos]`):

| Repo | Holds | Who uses it |
|---|---|---|
| `founder-coach` | this folder: pipeline, runtime, plugin source, evals, docs | you; CI runs on every push |
| `founder-coach-marketplace` | `marketplace.json`, the built plugin with its pack, a README | beta testers add it in Claude Code |

Both are free: private repos on GitHub Free include 2,000 Actions minutes a month; without a
payment method, runs stop at the quota instead of billing. The marketplace needs no hosting:
Claude Code clones it with the tester's own git credentials.

## One-time setup (on your Mac)

1. On github.com create two **private, empty** repos (no README, no .gitignore):
   `founder-coach` and `founder-coach-marketplace`.
2. This repo:

   ```bash
   cd path/to/ytbrain                          # this repo
   git init -b main
   git add -A
   git status --short | grep -E '\.env$|^.. data/|\.sqlite$' && echo "STOP: secrets or data staged"
   python3 scripts/check_secrets.py            # must print "no secrets in the tracked files"
   git commit -m "Initial import: pipeline, founder-coach runtime and plugin, evals"
   git remote add origin git@github.com:kaushalag29/founder-coach.git
   git push -u origin main
   ```

   `.gitignore` keeps out `.env`, `data/` (1.1 GB, rebuildable), `dist/`, `.venv/`,
   `sources.yaml` and caches. `eval/` (the benchmark, 4 MB) is tracked on purpose.
3. Block secrets before every commit (optional, recommended):

   ```bash
   printf '#!/bin/sh\nexec python3 scripts/check_secrets.py\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
   ```
4. The marketplace clone, next to this folder:

   ```bash
   cd ..                                       # the folder that holds this repo
   git clone git@github.com:kaushalag29/founder-coach-marketplace.git
   ```

   Cloning an empty repo prints a warning; that's fine.

## CI

`.github/workflows/ci.yml` runs on every push and pull request, on Linux with Python 3.11 and
3.13: the secret scan, the six test suites (core, eval, pack, coach, plugin, web; a suite that skips a test for a missing
dependency fails the run), a plugin assembled from a test pack with `--check`, a check that
the generated files (`founder_coach/product.json`, `founder_coach/playbooks/`) are committed,
and `claude plugin validate --strict`. No paid calls, no model downloads, no `.env`.
See runs under the repo's **Actions** tab.

## Release a beta version

```bash
# commit your changes first; the release refuses uncommitted work (--allow-dirty overrides)
python scripts/release.py --pack data/pack \
  --marketplace "../founder-coach-marketplace" --version 0.1.1 --push
git commit -am "Release 0.1.1" && git push      # the version bump in this repo
```

- The first release can omit `--version` (it ships the current `0.1.0`). Every later one needs
  a higher `--version`: Claude Code only updates testers whose installed version differs.
- The script checks, builds with `--check`, scans the build for secrets, checks GitHub's file
  limits (the pack must stay under 100 MB and out of Git LFS), writes `marketplace.json` and the
  README, runs `claude plugin validate`, commits and tags `founder-coach--v<version>`.
  Nothing in the marketplace clone changes until every check passes.
- Without `--push` it prints the push command, so you can look at the commit first.

## What testers do

They need read access to `founder-coach-marketplace` (add them as collaborators on GitHub),
`git` that reaches GitHub without prompting (an SSH key in ssh-agent, or
`gh auth login && gh auth setup-git`), and `uv`. Then, in Claude Code:

```
/plugin marketplace add kaushalag29/founder-coach-marketplace
/plugin install founder-coach@founder-coach-marketplace
/founder-coach:setup
```

Updates: `/plugin` → Marketplaces → founder-coach-marketplace → **Enable auto-update**, or
`/plugin marketplace update founder-coach-marketplace`. Feedback: `/founder-coach:feedback`
saves locally; `founder-coach feedback export` (the skill can run it) writes the file to send, and
the skill offers `founder-coach usage export` (the usage log, never the Founder's words) alongside.
Ask testers for both files at the end of each beta week.

## Renaming the product (ADR-0012)

1. Edit `id` (and `display_name`) in `product.toml`; rename the dev CLI in `pyproject.toml`.
2. `python scripts/assemble_plugin.py --pack data/pack` regenerates `founder_coach/product.json`;
   run the tests.
3. Release with a new `--version`. The script adds the old id to `renames` in
   `marketplace.json`, so installs migrate instead of breaking. Testers move their data folder
   once: `mv ~/.founder-coach ~/.<new-id>`, and rename any `FOUNDER_COACH_*` settings.

## Developing the plugin

`plugin/` is a template (`{{id}}` where the product id goes). Load the assembled build:

```bash
python scripts/assemble_plugin.py --pack data/pack --check
claude plugin validate dist/plugin --strict && ytbrain claude -- --plugin-dir dist/plugin
```
