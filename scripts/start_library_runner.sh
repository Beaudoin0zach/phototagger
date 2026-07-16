#!/bin/zsh
set -eu

if [[ $# -ne 1 ]]; then
  echo "usage: start_library_runner.sh RUN_DIRECTORY" >&2
  exit 2
fi

run_dir="${1:A}"
script_dir="${0:A:h}"
log_file="$run_dir/runner.log"
if [[ -e "$run_dir/STOP" ]]; then
  echo "STOP file exists; move or delete it before restarting the runner" >&2
  exit 1
fi
nohup python3 "$script_dir/run_library_batches.py" "$run_dir" >> "$log_file" 2>&1 &
runner_pid=$!
echo "$runner_pid" > "$run_dir/runner.pid"
echo "Started PhotoTagger runner PID $runner_pid"
echo "Log: $log_file"
echo "Pause safely: touch '$run_dir/STOP'"
