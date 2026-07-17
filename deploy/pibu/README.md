# pibu — Career Caddy scrape runner

Permanent shape for running `agents/runners/scrape_runner.py` on **pibu**
(Raspberry Pi, aarch64 Debian trixie, ~900 MB RAM, wayvnc/Xwayland on
`:0`). cc_auto owns this surface because it's operator-side (one user,
one box) — even though the runner *code* lives in the sibling
`career_caddy_agents` submodule.

## Why Camoufox (not Chromium)

Pibu's RAM budget is too tight to launch headed Chromium under
Playwright (`BrowserType.launch: Timeout 180000ms exceeded` in a prior
session). Camoufox's Firefox engine fits, and `--headed` mode keeps
one resident window with ephemeral tabs per scrape so cookies persist
across scrapes — exactly what Doug wants to watch over VNC.

## Components

- `caddy-runner.service` — systemd **user** unit. Sources the env file,
  exports `DISPLAY=:0`, runs the agents-repo runner with
  `--engine camoufox --headed`. Memory-capped (`MemoryHigh=600M`,
  `MemoryMax=750M`).
- `install.sh` — idempotent installer. Preflight-checks the env file,
  agents repo, and `uv`, then drops the unit into
  `~/.config/systemd/user/` and `daemon-reload`s.

## Required environment file

`~/.config/environment.d/career-caddy.conf`:

```
CC_API_BASE_URL=https://careercaddy.online
CC_API_TOKEN=<api-key from /api/v1/api-keys/>
CC_RUNNER_NAME=pibu
BROWSER_ENGINE=camoufox
```

Do **not** set `BROWSER_HEADLESS=true` — `--headed` requires a display.

## One-time setup on pibu

```bash
# 1. Clone the agents repo (already done as of 2026-05-31)
git clone git@github.com:overcast-software/career_caddy_agents.git \
  ~/Projects/career_caddy_agents

# 2. Install deps
cd ~/Projects/career_caddy_agents && uv sync

# 3. Fetch the Camoufox browser binary (~150 MB, slow on Pi)
uv run camoufox fetch

# 4. Install the systemd user unit
bash ~/Projects/career_caddy_automation/deploy/pibu/install.sh

# 5. Start it
systemctl --user start caddy-runner
journalctl --user -u caddy-runner -f
```

> **Path note (pibu vs laptop).** On pibu the cc_auto checkout lives at
> `~/Projects/career_caddy_automation/` (standalone clone). On the
> laptop the canonical copy is the `automation/` submodule of the
> Career Caddy parent at
> `~/Network/syncthing/Projects/career_caddy/automation/`. Pibu does
> NOT need the parent worktree — it only uses this `deploy/pibu/`
> subtree plus the agents repo. If you change these files on the
> laptop, sync them to pibu by either pulling the cc_auto repo on
> pibu or `scp`-ing this directory across.

## Refreshing after agents-repo updates

```bash
cd ~/Projects/career_caddy_agents
git pull
uv sync
systemctl --user restart caddy-runner
```

## Refreshing the unit itself

After editing `caddy-runner.service` in this directory and syncing it
to pibu:

```bash
bash ~/Projects/career_caddy_automation/deploy/pibu/install.sh
systemctl --user restart caddy-runner
```

## Watching it work

- VNC into pibu (wayvnc). The resident Camoufox window appears on
  `:0` and stays put; each claimed scrape opens an ephemeral tab.
- `journalctl --user -u caddy-runner -f` for the runner's log.
- Career Caddy UI: a scrape transitions `hold → running` with
  `runner_name=pibu` when pibu claims it.

## Limits

- Pibu has 905 MB RAM total; the unit caps at 750 MB. A Camoufox
  process that drifts above that gets killed and systemd restarts
  (with `RestartSec=30`). If this happens frequently, drop
  `--headed` and run headless (one ephemeral browser per scrape,
  lower steady-state RAM but no warm cookies).
- No swap headroom — pibu already runs close to the limit. Don't
  run other heavy workloads alongside the runner.
