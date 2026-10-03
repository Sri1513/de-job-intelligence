# Browser Profiles (git-ignored)

This directory holds Playwright **persistent browser profiles**, one per
platform:

- `linkedin/` — LinkedIn session (cookies, localStorage, Cloudflare clearance)
- `indeed/` — Indeed session

The profile is the **source of truth** for sessions. The apply worker reuses
it on every run; sites refresh the cookies themselves, so sessions stay alive
without re-injecting anything.

**Do not commit these directories.** They contain live session cookies —
anyone with them can act as you. They are excluded via `.gitignore`.

## Creating a session (run on the machine that will do the applying)

```bash
python scripts/manual_login.py linkedin
python scripts/manual_login.py indeed
```

Log in once in the headed browser window. On a headless Linux server:

```bash
xvfb-run -a python scripts/manual_login.py linkedin
```

## Legacy seed files

`config/auth_states/*.json` cookie exports are kept only as a **first-boot
seed**: they are injected exactly once, and only when the profile shows
logged-out. They are never re-injected over a healthy session.
