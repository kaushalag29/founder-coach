# Security and privacy

## Reporting a problem

The repository is private during the beta. Report a vulnerability or a leak (a secret in the
history, Founder data where it shouldn't be) directly to the maintainer, Kaushal, through GitHub
(`kaushalag29`), not in a shared channel. Include what you found and where; don't include the
secret itself.

## Secrets

- API keys live only in `.env`, which is git-ignored; `.env.example` is the template.
- `scripts/check_secrets.py` scans every tracked file for API keys, tokens, private keys and
  env files. CI runs it on every push, `scripts/release.py` runs it on the repo and on the
  built plugin, and `docs/release.md` shows the pre-commit hook.
- If a key is ever committed: revoke it at the provider first, then remove it from the history.
  Deleting it in a later commit is not enough.

## Founder data

- The coach keeps everything about a Founder on their own machine, in `~/.founder-coach/` (or
  `FOUNDER_COACH_HOME`): the store, FOUNDER.md, backups, Feedback, the usage log and any exports in
  `exports/`. There is no server and nothing is sent automatically; the only network call is the
  one-time download of the ONNX query models. The usage log (docs/usage-log.md) stays local, holds
  no Founder text unless `FOUNDER_COACH_USAGE_TEXT=1` (search text only), and is turned off with
  `FOUNDER_COACH_USAGE=0`.
- Only public corpus text is sent to LLM judges while building and evaluating; Founder data never is (ADR-0006, ADR-0008).
- Feedback and the usage log leave the machine only when the Founder runs `founder-coach feedback export`
  or `founder-coach usage export` and sends the file. `founder-coach forget` empties the store and FOUNDER.md,
  Feedback and the usage log included; it keeps one final backup unless run with `--no-backup`, and
  does not delete earlier exports in `exports/`.
- The runtime reads only `FOUNDER_COACH_*` lines from any `.env` file, so a shared `.env` never
  hands it an API key.
- Quoted talk text reaches the host model wrapped as untrusted reference material, never as
  instructions. Write tools save only what the Founder approved (ADR-0011).

## Supply chain

- The runtime's dependencies are declared in `plugin/pyproject.toml` (a test checks every
  import is covered) and installed by `uv` on the Founder's machine.
- The Knowledge pack ships with a sha256 manifest; the runtime refuses a pack that fails it.
