#!/usr/bin/env bash
# Install the SW Local Agent Service station runner on a Linux test station, offline.
#
#   tar xzf slas-station-runner-<version>.tgz && cd slas-station-runner-<version> && ./install.sh
#
# What happens: a virtual environment under ~/.slas-station-runner/venv is filled from the
# wheels in this bundle (no network), the platform CA is copied next to it, and a systemd
# user unit is installed so the runner starts with the operator's graphical session. The
# station still has no identity afterwards: the next step is `slas-station-runner enrol`
# with the one-time code from Admin → Stations, then `doctor`, then the service starts.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
state="${SLAS_RUNNER_STATE:-$HOME/.slas-station-runner}"
venv="$state/venv"

say() { printf '%s\n' "$*"; }

python="$(command -v python3.12 || command -v python3 || true)"
if [ -z "$python" ]; then
  say "Python 3.12 is not installed on this station."
  say "The runner needs it; install the distribution's python3.12 package (offline media) and run this again."
  exit 1
fi
if ! "$python" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  say "Python at $python is not 3.12; the runner needs 3.12."
  say "Install python3.12 and run this again."
  exit 1
fi

for tool in xdotool import x11vnc; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    say "Note: $tool is not installed. The runner needs xdotool for GUI steps, ImageMagick's import for screenshots, and x11vnc so the operator can watch and take over. Install them from offline media; doctor will check again."
  fi
done

mkdir -p "$state"
chmod 700 "$state"
say "Creating the virtual environment under $venv."
"$python" -m venv --clear "$venv"
"$venv/bin/python" -m pip install --quiet --no-index --find-links "$here/wheels" --upgrade pip >/dev/null 2>&1 || true
"$venv/bin/python" -m pip install --quiet --no-index --find-links "$here/wheels" slas-station-runner
cp "$here/slas-ca.pem" "$state/slas-ca.pem"
chmod 600 "$state/slas-ca.pem"

unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
sed "s|@STATE@|$state|g" "$here/slas-station-runner.service" > "$unit_dir/slas-station-runner.service"
if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null; then
  systemctl --user enable slas-station-runner.service >/dev/null 2>&1 || true
  say "The systemd user unit is installed and enabled; it starts once the station is enrolled."
else
  say "systemd --user is not available in this session; start the runner from the graphical session with: $venv/bin/slas-station-runner serve"
fi

say ""
say "Installed $("$venv/bin/slas-station-runner" --help 2>/dev/null | head -1 | sed 's/usage: //') under $venv."
say "Next, on this station, with the code from Admin → Stations:"
say "  $venv/bin/slas-station-runner enrol --platform https://<factory-executor>:8444 \\"
say "      --station <name> --code XXXX-XXXX-XXXX --runner-url https://<this station>:8443 --ca $state/slas-ca.pem"
say "  $venv/bin/slas-station-runner doctor"
say "  systemctl --user start slas-station-runner   # or: $venv/bin/slas-station-runner serve"
