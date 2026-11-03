#!/usr/bin/env bash
# Read-only preflight for the shared general domain and CPUMAP CPU.
# No process affinity, IRQ, boot file or governor is changed here.
set -Eeuo pipefail
SYS_ROOT=/sys
PARSED_CPUS=()

parse_cpu_list() {
    local list=$1 part first last cpu
    local parts=()
    PARSED_CPUS=()
    [[ $list =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] || return 1
    IFS=',' read -ra parts <<< "$list"
    for part in "${parts[@]}"; do
        first=${part%-*}; last=${part#*-}
        ((${#first} <= 4 && ${#last} <= 4)) || return 1
        first=$((10#$first)); last=$((10#$last))
        ((first <= last && last < 4096)) || return 1
        for ((cpu=first;cpu<=last;cpu++)); do PARSED_CPUS+=("$cpu"); done
    done
}

contains_cpu() {
    local wanted=$1 cpu
    shift
    for cpu in "$@"; do [[ $cpu != "$wanted" ]] || return 0; done
    return 1
}

main() {
    if [[ ${1:-} == --help ]]; then
        echo 'Usage: check_cpu_partition.sh INTERFACE SHARED_CPUS CPUMAP_CPU'
        return 0
    fi
    [[ $# == 3 ]] || return 2
    local interface=$1 shared=$2 dedicated=$3 cpu path value
    local general=() online=() isolated=()
    [[ $interface =~ ^[A-Za-z0-9_.:-]{1,15}$ && $dedicated =~ ^[0-9]{1,4}$ ]] || return 2
    dedicated=$((10#$dedicated))
    parse_cpu_list "$shared" || { echo 'Invalid shared CPU list' >&2; return 2; }
    general=("${PARSED_CPUS[@]}")
    contains_cpu "$dedicated" "${general[@]}" && { echo 'CPUMAP CPU overlaps shared domain' >&2; return 1; }
    [[ -d "$SYS_ROOT/class/net/$interface" ]] || { echo 'Network interface not found' >&2; return 1; }
    path="$SYS_ROOT/devices/system/cpu/online"
    [[ -r $path ]] && parse_cpu_list "$(< "$path")" || { echo 'Cannot read online CPUs' >&2; return 1; }
    online=("${PARSED_CPUS[@]}")
    for cpu in "${general[@]}" "$dedicated"; do
        contains_cpu "$cpu" "${online[@]}" || { echo "CPU$cpu is offline" >&2; return 1; }
    done
    path="$SYS_ROOT/devices/system/cpu/isolated"
    if [[ -r $path && -n $(< "$path") ]]; then
        parse_cpu_list "$(< "$path")" || return 1
        isolated=("${PARSED_CPUS[@]}")
    fi
    contains_cpu "$dedicated" "${isolated[@]}" || {
        echo "CPU$dedicated is not scheduler-domain isolated. Prepare the receiver's dedicated CPU before measurement; see experiment/design/배포전_준비사항.md." >&2
        return 1
    }
    for cpu in "${general[@]}"; do
        if contains_cpu "$cpu" "${isolated[@]}"; then
            echo "Shared CPU$cpu is isolated from normal scheduling" >&2; return 1
        fi
    done
    # Keep PASS on the ordinary IRQ/NAPI stack, without another steering policy.
    for path in "$SYS_ROOT/class/net/$interface/queues/"rx-*/rps_cpus; do
        [[ -e $path ]] || continue
        [[ -r $path ]] || return 1
        value=$(< "$path")
        [[ $value =~ ^0+(,0+)*$ ]] || { echo "RPS must be disabled: $path=$value" >&2; return 1; }
    done
    path="$SYS_ROOT/class/net/$interface/threaded"
    if [[ -e $path ]]; then
        [[ -r $path && $(< "$path") == 0 ]] || {
            echo 'Separate threaded NAPI is enabled; use the common IRQ/NAPI experiment configuration' >&2
            return 1
        }
    fi
    printf 'CPU partition verified: shared=%s cpumap=%s (scheduler-domain isolated), RPS=off, separate threaded NAPI=off\n' "$shared" "$dedicated"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
