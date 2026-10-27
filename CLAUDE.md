# quota-canvas

TURZX 9.2" USB secondary screen (1920x462, USB 1cbe:0092) driver for this Mac. See README.md.

- To put anything on the TURZX screen, use this repo's `turzx` package / `.venv/bin/turzx` CLI. Do not re-clone phstudy/turing-smart-screen-cli or patch it; it is a pinned dependency and `src/turzx/screen.py` supplies the PID and rotation.
- Draw content as landscape 1920x462 and call `Screen().show(img)`; it rotates to the portrait 462x1920 frame the device needs.
- If `.venv` is missing, run `./scripts/setup.sh`.
- Only touch USB VID 0x1cbe. Other devices on the same hubs (Elgato HD60 S, audio interface, keyboards) must not be reset or claimed.
- Claude can't see the panel; ask the user to confirm what is displayed.

## Public repository rules

This repository is public on GitHub. Everything committed (including history) is visible to anyone.

- Never commit personal information: real names, user names (e.g. in `/Users/<name>/...`), email addresses, home or scratchpad paths, hostnames, IP addresses, serial numbers, or account IDs. Use `~`, `Path.home()`, `$HOME`, or placeholders like `example.test`.
- Never commit secrets: API keys, OAuth tokens, cookies, keychain contents, `auth.json` / credentials files, or real `config.toml`. Tests use obvious dummy values only.
- Never commit real usage data (`status.json`, `history.jsonl`, screenshots of the live panel). Preview images must use dummy data.
- Commit author email must be the GitHub noreply address (`<id>+<user>@users.noreply.github.com`).
- Keep machine-specific notes in `CLAUDE.local.md` / `AGENTS.local.md` (git-ignored), not in tracked files.
- Before committing, check the diff for the items above (e.g. `git diff --cached | grep -nE '/Users/|@[a-z0-9-]+\.[a-z]|sk-|token'`).
