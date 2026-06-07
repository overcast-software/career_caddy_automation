#!/usr/bin/env bash
# Install / refresh the Career Caddy scrape-runner systemd user unit on pibu.
#
# Idempotent. Safe to re-run after editing caddy-runner.service.
#
# Prereqs (verified by this script):
#   - ~/.config/environment.d/career-caddy.conf exists with
#     CC_API_BASE_URL, CC_API_TOKEN, CC_RUNNER_NAME, BROWSER_ENGINE=camoufox
#   - ~/Projects/career_caddy_agents is a checkout of the agents repo
#   - ~/.local/bin/uv is installed
#   - Camoufox browser binary fetched (run `uv run camoufox fetch` once
#     inside ~/Projects/career_caddy_agents if missing)
#
# Usage on pibu:
#   bash ~/Projects/career_caddy_automation/deploy/pibu/install.sh
#
# Or via ssh from another box:
#   scp -r deploy/pibu pibu:/tmp/cc-pibu-deploy
#   ssh pibu 'bash /tmp/cc-pibu-deploy/install.sh'

set -euo pipefail

ENV_FILE="$HOME/.config/environment.d/career-caddy.conf"
AGENTS_DIR="$HOME/Projects/career_caddy_agents"
UNIT_SRC="$(dirname "$(readlink -f "$0")")/caddy-runner.service"
UNIT_DST="$HOME/.config/systemd/user/caddy-runner.service"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "OK:   $*"; }

# --- Preflight ---------------------------------------------------------------
[ -f "$ENV_FILE" ] || fail "env file missing: $ENV_FILE"
grep -q '^CC_API_TOKEN=' "$ENV_FILE" || fail "CC_API_TOKEN missing in $ENV_FILE"
grep -q '^CC_API_BASE_URL=' "$ENV_FILE" || fail "CC_API_BASE_URL missing in $ENV_FILE"
grep -q '^BROWSER_ENGINE=camoufox' "$ENV_FILE" \
  || fail "BROWSER_ENGINE must be 'camoufox' in $ENV_FILE (got: $(grep ^BROWSER_ENGINE "$ENV_FILE" || echo none))"
ok "env file looks sane"

[ -d "$AGENTS_DIR/runners" ] || fail "agents repo not at $AGENTS_DIR"
ok "agents repo present"

[ -x "$HOME/.local/bin/uv" ] || fail "uv not at ~/.local/bin/uv"
ok "uv present"

# Camoufox browser presence — informational; install.sh does not fetch
# (the download is slow, do it interactively the first time).
if [ ! -f "$HOME/.cache/camoufox/version.json" ]; then
  echo "WARN: Camoufox browser not fetched. Run once before starting the unit:"
  echo "        cd $AGENTS_DIR && uv run camoufox fetch"
fi

# --- Install -----------------------------------------------------------------
mkdir -p "$(dirname "$UNIT_DST")"
cp "$UNIT_SRC" "$UNIT_DST"
ok "wrote $UNIT_DST"

systemctl --user daemon-reload
ok "reloaded systemd user manager"

# Enable so it starts at next graphical-session, but don't auto-start
# right now — the caller decides via `systemctl --user start caddy-runner`.
systemctl --user enable caddy-runner.service
ok "enabled caddy-runner.service"

cat <<EOF

Next steps:
  systemctl --user start caddy-runner
  journalctl --user -u caddy-runner -f

Status:
  systemctl --user status caddy-runner
EOF
