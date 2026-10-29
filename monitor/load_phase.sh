#!/bin/bash
# Its own process group is owned by run_load.sh; all referenced groups are children.
set -Eeuo pipefail
on=$1 off=$2
shift 2
groups=("$@")
resume() { for pid in "${groups[@]}"; do kill -CONT -- "-$pid" 2>/dev/null || true; done; }
trap resume EXIT
trap 'exit 0' TERM INT
while :; do
    sleep "$on" & wait "$!"
    for pid in "${groups[@]}"; do kill -STOP -- "-$pid"; done
    printf 'OFF %(%s)T\n' -1
    sleep "$off" & wait "$!"
    resume
    printf 'ON %(%s)T\n' -1
done
