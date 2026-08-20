#!/bin/zsh
# Install (or reinstall) the launchd agent that supervises a library runner.
#
# Usage: ./scripts/install_health_check.sh RUN_DIRECTORY
#        ./scripts/install_health_check.sh --uninstall
#
# The agent runs scripts/health_check.py every 5 minutes. That script decides
# whether a restart is warranted; see its docstring for what it refuses to
# override (STOP files, completed runs, repeated fruitless restarts).
set -eu

label="com.zachbeaudoin.phototagger.healthcheck"
plist="$HOME/Library/LaunchAgents/$label.plist"
script_dir="${0:A:h}"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$plist"
  echo "Uninstalled $label"
  exit 0
fi

if [[ $# -lt 1 ]]; then
  echo "usage: install_health_check.sh RUN_DIRECTORY | --uninstall" >&2
  exit 2
fi
run_dir="${1:A}"
if [[ ! -f "$run_dir/run.json" ]]; then
  echo "not a run directory (no run.json): $run_dir" >&2
  exit 1
fi

log_dir="$run_dir"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/bin/python3</string>
		<string>$script_dir/health_check.py</string>
		<string>$run_dir</string>
	</array>
	<key>StartInterval</key>
	<integer>300</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardOutPath</key>
	<string>$log_dir/healthcheck.log</string>
	<key>StandardErrorPath</key>
	<string>$log_dir/healthcheck.log</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>PATH</key>
		<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
	</dict>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "Installed $label (every 5 min, supervising $run_dir)"
echo "Log:       $log_dir/healthcheck.log"
echo "Status:    launchctl list | grep phototagger"
echo "Uninstall: $script_dir/install_health_check.sh --uninstall"
