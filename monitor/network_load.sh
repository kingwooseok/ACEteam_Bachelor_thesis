#!/bin/bash
# Native iperf3 background traffic on the sender Pi. Packet length is unchanged.
set -Eeuo pipefail
export LC_ALL=C
[[ $# == 7 ]] || { echo 'Usage: network_load.sh HOST TOTAL_BPS PARALLEL udp|tcp forward|reverse|bidir PORT DURATION' >&2; exit 2; }
host=$1 rate=$2 parallel=$3 protocol=$4 direction=$5 port=$6 duration=$7
[[ $host =~ ^[0-9.]+$ && $rate =~ ^[1-9][0-9]{0,12}$ && $parallel =~ ^[1-9][0-9]{0,3}$ && $port =~ ^[0-9]{1,5}$ && $duration =~ ^[1-9][0-9]{0,5}$ ]] || exit 2
((parallel <= 1024 && port >= 1024 && port <= 65535 && rate >= parallel)) || exit 2
[[ $protocol == udp || $protocol == tcp ]] || exit 2
[[ $direction == forward || $direction == reverse || $direction == bidir ]] || exit 2
command -v iperf3 >/dev/null || { echo 'iperf3 is not installed' >&2; exit 2; }
on=${ACE_LOAD_ON_SEC:-} off=${ACE_LOAD_OFF_SEC:-}
if [[ -n $on || -n $off ]]; then
    [[ $on =~ ^[0-9]+([.][0-9]+)?$ && $off =~ ^[0-9]+([.][0-9]+)?$ ]] || exit 2
    awk -v a="$on" -v b="$off" 'BEGIN {exit !(a>0 && b>0)}' || exit 2
fi
child='' stopping=0 staging_dir=''
# kill -0 also reports zombies. They cannot write logs and must not trigger
# a forced-termination failure; inspect only our PID/process-group ownership.
owned_group_alive() {
    local owned=$1 path line rest state parent group
    for path in /proc/[0-9]*/stat; do
        IFS= read -r line 2>/dev/null < "$path" || continue
        rest=${line##*) }
        read -r state parent group _ <<< "$rest"
        [[ $group == "$owned" || $path == "/proc/$owned/stat" ]] || continue
        [[ $state == Z || $state == X ]] || return 0
    done
    return 1
}
cleanup() {
    local status=$1
    ((stopping == 0)) || return "$status"
    stopping=1
    trap - EXIT
    trap '' INT TERM
    if [[ -n $child ]]; then
        if ! kill -TERM -- "-$child" 2>/dev/null; then
            kill -TERM "$child" 2>/dev/null || true
        fi
        for ((i=0;i<30;i++)); do owned_group_alive "$child" || break; sleep 0.1; done
        if owned_group_alive "$child"; then
            kill -KILL -- "-$child" 2>/dev/null || true
            kill -KILL "$child" 2>/dev/null || true
            status=1
            printf 'Error: network load needed forced termination\n' >&2
        fi
        wait "$child" 2>/dev/null || true
    fi
    if [[ -n $staging_dir ]]; then
        exec 1>&3 2>&4
        if cat -- "$staging_dir/network.log"; then
            rm -f -- "$staging_dir/network.log" || status=1
            if ! rmdir -- "$staging_dir"; then
                printf 'Error: staging directory remains at %s\n' "$staging_dir" >&2
                status=1
            fi
        else
            printf 'Error: cannot save network log; staged logs preserved at %s\n' "$staging_dir" >&2
            status=1
        fi
    fi
    exit "$status"
}
trap 'cleanup "$?"' EXIT
trap 'exit 0' TERM INT
for tool in setsid mktemp stat cat; do command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 2; }; done
[[ -d /dev/shm && -w /dev/shm && $(stat -f -c %T /dev/shm) == tmpfs ]] || { echo '/dev/shm must be writable tmpfs' >&2; exit 2; }
exec 3>&1 4>&2
staging_dir=$(mktemp -d /dev/shm/ace-network.XXXXXX)
# Preserve the caller's destination while all phase/iperf output stays in RAM.
exec > "$staging_dir/network.log" 2>&1
args=(-c "$host" -p "$port" -P "$parallel" -b "$((rate / parallel))" -i 0)
[[ $protocol != udp ]] || args+=(-u)
[[ $direction != reverse ]] || args+=(-R)
[[ $direction != bidir ]] || args+=(--bidir)
end=$((SECONDS + duration))
if [[ -z $on ]]; then
    setsid iperf3 "${args[@]}" -t "$duration" & child=$!
    wait "$child"
    child=''
else
    while ((SECONDS < end)); do
        remaining=$((end - SECONDS))
        span=$(awk -v a="$on" -v b="$remaining" 'BEGIN {print a<b?a:b}')
        printf 'ON %(%s)T\n' -1
        # timeout owns its iperf child; all of them share our fresh process group.
        setsid timeout --signal=TERM --kill-after=2s "${span}s" iperf3 "${args[@]}" -t "$remaining" & child=$!
        rc=0; wait "$child" || rc=$?; child=''
        [[ $rc == 0 || $rc == 124 ]] || exit "$rc"
        printf 'OFF %(%s)T\n' -1
        ((SECONDS < end)) || break
        remaining=$((end - SECONDS))
        span=$(awk -v a="$off" -v b="$remaining" 'BEGIN {print a<b?a:b}')
        setsid sleep "$span" & child=$!
        wait "$child"; child=''
    done
fi
