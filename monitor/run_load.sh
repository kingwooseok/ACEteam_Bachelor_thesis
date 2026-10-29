#!/bin/bash
# Native observation/load tools for ace-run. No Python or packet processing.
set -Eeuo pipefail
export LC_ALL=C
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PIDS=(); LOAD_PIDS=(); LOG_NAMES=(); STOPPING=0; STAGING_DIR=''
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
uint() { [[ $1 =~ ^[0-9]+$ ]] && ((${#1} <= 9)); }
require() { command -v "$1" >/dev/null 2>&1 || die "'$1' is not installed"; }
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
    local status=$1 flush_failed=0 pid alive try log
    ((STOPPING == 0)) || return "$status"
    STOPPING=1
    trap - EXIT
    trap '' INT TERM
    for pid in "${PIDS[@]}"; do
        kill -CONT -- "-$pid" 2>/dev/null || true
        # Cover TERM arriving between fork and setsid's creation of the group.
        if ! kill -TERM -- "-$pid" 2>/dev/null; then
            kill -CONT "$pid" 2>/dev/null || true
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    for ((try=0; try<30; try++)); do
        alive=0
        for pid in "${PIDS[@]}"; do owned_group_alive "$pid" && alive=1 || true; done
        ((alive)) || break
        sleep 0.1
    done
    for pid in "${PIDS[@]}"; do
        if owned_group_alive "$pid"; then
            kill -KILL -- "-$pid" 2>/dev/null || true
            kill -KILL "$pid" 2>/dev/null || true
            status=1
            printf 'Error: load/monitor needed forced termination (pid=%s)\n' "$pid" >&2
        fi
        wait "$pid" 2>/dev/null || true
    done
    # Every writer has stopped. Only now copy observations to persistent storage.
    if [[ -n $STAGING_DIR ]]; then
        for log in "${LOG_NAMES[@]}"; do
            if ! (set -o noclobber; cat -- "$STAGING_DIR/$log" > "$RESULT_DIR/$log"); then
                printf 'Error: cannot save log without overwriting: %s\n' "$RESULT_DIR/$log" >&2
                flush_failed=1
            elif ! sync -f "$RESULT_DIR/$log"; then
                printf 'Error: cannot flush log: %s\n' "$RESULT_DIR/$log" >&2
                flush_failed=1
            fi
        done
        if ((flush_failed)); then
            printf 'Error: staged logs preserved at %s\n' "$STAGING_DIR" >&2
            status=1
        else
            for log in "${LOG_NAMES[@]}"; do rm -f -- "$STAGING_DIR/$log" || status=1; done
            if ! rmdir -- "$STAGING_DIR"; then
                printf 'Error: staging directory remains at %s\n' "$STAGING_DIR" >&2
                status=1
            fi
        fi
    fi
    exit "$status"
}
trap 'cleanup "$?"' EXIT
trap 'exit 0' INT TERM
[[ $# == 4 ]] || die "Usage: $0 CPU_LOAD INTERVAL DURATION LOAD_CPUS"
CPU_LOAD=$1 INTERVAL=$2 DURATION=$3 LOAD_CPUS=$4
uint "$CPU_LOAD" && ((10#$CPU_LOAD <= 100)) || die 'CPU_LOAD must be 0..100'
uint "$INTERVAL" && ((10#$INTERVAL > 0)) || die 'INTERVAL must be a positive integer'
uint "$DURATION" && ((10#$DURATION > 0)) || die 'DURATION must be a positive integer'
CPU_LOAD=$((10#$CPU_LOAD)); INTERVAL=$((10#$INTERVAL)); DURATION=$((10#$DURATION))
RUN_ID=${ACE_RUN_ID:?ACE_RUN_ID required}
RESULT_DIR=${ACE_RUN_DIR:?ACE_RUN_DIR required}
INTERFACE=${ACE_INTERFACE:-eth0}
MONITOR_ENABLED=${ACE_MONITOR_ENABLED:-1}
IPERF_ENABLED=${ACE_IPERF_ENABLED:-0}
IPERF_PORT=${ACE_IPERF_PORT:-5201}
IPERF_CPU=${ACE_IPERF_CPU:-inherit}
CPU_METHOD=${ACE_CPU_METHOD:-}
MEMORY_WORKERS=${ACE_MEMORY_WORKERS:-0}
MEMORY_BYTES=${ACE_MEMORY_BYTES:-}
IO_WORKERS=${ACE_IO_WORKERS:-0}
IO_PATH=${ACE_IO_PATH:-}
ON_SEC=${ACE_LOAD_ON_SEC:-}
OFF_SEC=${ACE_LOAD_OFF_SEC:-}
READY_FILE=${ACE_READY_FILE:-}
[[ $RUN_ID =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || die 'invalid run id'
[[ $INTERFACE =~ ^[A-Za-z0-9_.:-]{1,15}$ ]] || die 'invalid interface'
for flag in "$MONITOR_ENABLED" "$IPERF_ENABLED"; do [[ $flag == 0 || $flag == 1 ]] || die 'boolean options must be 0/1'; done
uint "$MEMORY_WORKERS" && ((10#$MEMORY_WORKERS <= 256)) || die 'invalid memory workers'
uint "$IO_WORKERS" && ((10#$IO_WORKERS <= 256)) || die 'invalid I/O workers'
MEMORY_WORKERS=$((10#$MEMORY_WORKERS)); IO_WORKERS=$((10#$IO_WORKERS))
uint "$IPERF_PORT" && ((10#$IPERF_PORT >= 1024 && 10#$IPERF_PORT <= 65535)) || die 'invalid iperf port'
[[ -z $CPU_METHOD || $CPU_METHOD =~ ^[A-Za-z0-9_-]+$ ]] || die 'invalid CPU method'
((MEMORY_WORKERS == 0)) || [[ $MEMORY_BYTES =~ ^[1-9][0-9]*(%|[KMGkmg])?$ ]] || die 'memory bytes required'
((IO_WORKERS == 0)) || [[ $IO_PATH == /* && -d $IO_PATH && -w $IO_PATH ]] || die 'writable absolute I/O directory required'
if [[ -n $ON_SEC || -n $OFF_SEC ]]; then
    [[ $ON_SEC =~ ^[0-9]+([.][0-9]+)?$ && $OFF_SEC =~ ^[0-9]+([.][0-9]+)?$ ]] || die 'both ON/OFF seconds required'
    awk -v a="$ON_SEC" -v b="$OFF_SEC" 'BEGIN {exit !(a>0 && b>0)}' || die 'ON/OFF seconds must be positive'
fi
CPU_ARRAY=()
if [[ -n $LOAD_CPUS ]]; then
    [[ $LOAD_CPUS =~ ^[0-9]+(,[0-9]+)*$ ]] || die 'LOAD_CPUS must be comma-separated CPU ids'
    IFS=',' read -ra CPU_ARRAY <<< "$LOAD_CPUS"
fi
((CPU_LOAD == 0 || ${#CPU_ARRAY[@]} > 0)) || die 'CPU load needs LOAD_CPUS'
for cpu in "${CPU_ARRAY[@]}"; do uint "$cpu" && ((10#$cpu < $(nproc --all))) || die "invalid CPU $cpu"; done
require setsid
((BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1))) || die 'Bash 5.1 or newer is required'
for command in mktemp stat cat sync; do require "$command"; done
[[ -d /dev/shm && -w /dev/shm && $(stat -f -c %T /dev/shm) == tmpfs ]] || die '/dev/shm must be writable tmpfs'
if ((CPU_LOAD || MEMORY_WORKERS || IO_WORKERS)); then require stress-ng; fi
if ((CPU_LOAD)); then require taskset; fi
if ((MONITOR_ENABLED)); then require mpstat; require sar; require stdbuf; fi
if ((IPERF_ENABLED)); then
    require iperf3
    if [[ $IPERF_CPU != inherit ]]; then
        uint "$IPERF_CPU" && ((10#$IPERF_CPU < $(nproc --all))) || die 'invalid iperf CPU'
        require taskset
    fi
fi
mkdir -p "$RESULT_DIR"
(
    set -o noclobber
    printf 'run_id=%s\ncpu_load=%s\nload_cpus=%s\nmeasurement_interval=%s\nduration=%s\ninterface=%s\nmemory_workers=%s\nmemory_bytes=%s\nio_workers=%s\nload_on_s=%s\nload_off_s=%s\n' "$RUN_ID" "$CPU_LOAD" "$LOAD_CPUS" "$INTERVAL" "$DURATION" "$INTERFACE" "$MEMORY_WORKERS" "$MEMORY_BYTES" "$IO_WORKERS" "$ON_SEC" "$OFF_SEC" > "$RESULT_DIR/config.txt"
) || die 'result directory already has config.txt'
printf 'iperf_cpu=%s\n' "$IPERF_CPU" >> "$RESULT_DIR/config.txt"
STAGING_DIR=$(mktemp -d "/dev/shm/ace-monitor.$RUN_ID.XXXXXX") || die 'cannot create log staging directory'
launch() {
    local type=$1 log=$2
    shift 2
    [[ ! -e "$STAGING_DIR/$log" ]] || die "duplicate log name: $log"
    LOG_NAMES+=("$log")
    (set -o noclobber; exec setsid "$@" > "$STAGING_DIR/$log" 2>&1) &
    local pid=$!
    PIDS+=("$pid")
    if [[ $type == load ]]; then LOAD_PIDS+=("$pid"); fi
}
if ((IPERF_ENABLED)); then
    iperf_args=(iperf3 -s -p "$IPERF_PORT" -i 0)
    [[ $IPERF_CPU == inherit ]] || iperf_args=(taskset -c "$IPERF_CPU" "${iperf_args[@]}")
    launch observe iperf3.log "${iperf_args[@]}"
fi
if ((MONITOR_ENABLED)); then
    launch observe mpstat.log stdbuf -oL mpstat -P ALL "$INTERVAL"
    launch observe network.log stdbuf -oL sar -n DEV --iface="$INTERFACE" "$INTERVAL"
fi
if ((CPU_LOAD)); then
    args=(--cpu 1 --cpu-load "$CPU_LOAD" --metrics-brief)
    [[ -z $CPU_METHOD ]] || args+=(--cpu-method "$CPU_METHOD")
    for cpu in "${CPU_ARRAY[@]}"; do launch load "stress_cpu${cpu}.log" taskset -c "$cpu" stress-ng "${args[@]}"; done
fi
if ((MEMORY_WORKERS)); then launch load stress_memory.log stress-ng --vm "$MEMORY_WORKERS" --vm-bytes "$MEMORY_BYTES" --metrics-brief; fi
if ((IO_WORKERS)); then
    run_io="$IO_PATH/ace-$RUN_ID"
    mkdir "$run_io"
    launch load stress_io.log stress-ng --hdd "$IO_WORKERS" --temp-path "$run_io" --metrics-brief
fi
if [[ -n $ON_SEC && ${#LOAD_PIDS[@]} -gt 0 ]]; then
    launch observe load-phases.log bash "$SCRIPT_DIR/load_phase.sh" "$ON_SEC" "$OFF_SEC" "${LOAD_PIDS[@]}"
fi
sleep 0.1
for pid in "${PIDS[@]}"; do kill -0 "$pid" 2>/dev/null || die 'load/monitor failed during preparation'; done
# Register the one native timer before READY makes us stoppable by the runner.
setsid sleep "$DURATION" & timer_pid=$!
PIDS+=("$timer_pid")
if [[ -n $READY_FILE ]]; then
    ready_tmp="$READY_FILE.tmp.$$"
    (set -o noclobber; printf '%s\n' "$$" > "$ready_tmp") || die 'cannot create readiness marker'
    if ! ln "$ready_tmp" "$READY_FILE"; then rm -f -- "$ready_tmp"; die 'readiness marker exists'; fi
    rm -f -- "$ready_tmp"
fi
printf 'READY run=%s pid=%s\n' "$RUN_ID" "$$"
# One native timer and a blocking builtin wait replace periodic shell polling.
wait_status=0
wait -n -p completed_pid "${PIDS[@]}" || wait_status=$?
[[ ${completed_pid:-} == "$timer_pid" && $wait_status == 0 ]] || die 'load/monitor exited before measurement finished'
printf 'DONE run=%s\n' "$RUN_ID"
