#!/bin/zsh
set -eu

if [[ $# -lt 1 ]]; then
  echo "usage: start_library_runner.sh RUN_DIRECTORY [BATCH_SIZE] [--stop-after=N] [--only-extensions=...] [--only-filenames=...]" >&2
  exit 2
fi

run_dir="${1:A}"
shift
batch_size=""
extra_args=()
for arg in "$@"; do
  case "$arg" in
    --*) extra_args+=("$arg") ;;
    *)   batch_size="$arg" ;;
  esac
done

script_dir="${0:A:h}"
log_file="$run_dir/runner.log"
if [[ -e "$run_dir/STOP" ]]; then
  echo "STOP file exists; move or delete it before restarting the runner" >&2
  exit 1
fi
nohup python3 "$script_dir/run_library_batches.py" "$run_dir" \
  ${batch_size:+"$batch_size"} ${extra_args:+"${extra_args[@]}"} >> "$log_file" 2>&1 &
runner_pid=$!
# The child exits within a second or so when another runner already holds
# runner.lock (or the Photos-lifecycle lock). Confirm it survived that check
# BEFORE recording the pid: writing it unconditionally used to overwrite a
# healthy runner's pid file with a dead one, so every later health check
# reported the live run as dead.
sleep 3
if ! kill -0 "$runner_pid" 2>/dev/null; then
  echo "Runner exited immediately; runner.pid left untouched. Last log lines:" >&2
  tail -3 "$log_file" >&2
  exit 1
fi
echo "$runner_pid" > "$run_dir/runner.pid"
# Record how this runner was invoked so an unattended restart can reproduce
# its scope. Without it the health check restarts a filtered or target-limited
# run as an unrestricted whole-library run, silently changing what it does.
: > "$run_dir/runner.args"
[[ -n "$batch_size" ]] && print -r -- "$batch_size" >> "$run_dir/runner.args"
for arg in ${extra_args[@]:-}; do print -r -- "$arg" >> "$run_dir/runner.args"; done
[[ -n "${PHOTOTAGGER_MIN_FREE_GB:-}" ]] && \
  print -r -- "PHOTOTAGGER_MIN_FREE_GB=$PHOTOTAGGER_MIN_FREE_GB" > "$run_dir/runner.env"
echo "Started PhotoTagger runner PID $runner_pid"
echo "Log: $log_file"
echo "Pause safely: touch '$run_dir/STOP'"
