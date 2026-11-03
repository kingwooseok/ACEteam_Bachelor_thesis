#!/usr/bin/env bash
# Read-only preflight for mode-dependent CPU reservation.
set -Eeuo pipefail
SYS_ROOT=/sys
CGROUP_ROOT=/sys/fs/cgroup
PARSED_CPUS=()

parse_cpu_list() {
    local list=$1 part first last cpu
    local parts=() seen=()
    PARSED_CPUS=()
    [[ $list =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] || return 1
    IFS=',' read -ra parts <<< "$list"
    for part in "${parts[@]}"; do
        first=${part%-*}; last=${part#*-}
        ((${#first} <= 4 && ${#last} <= 4)) || return 1
        first=$((10#$first)); last=$((10#$last))
        ((first <= last && last < 4096)) || return 1
        for ((cpu=first;cpu<=last;cpu++)); do
            [[ -z ${seen[cpu]:-} ]] || return 1
            seen[cpu]=1
        done
    done
    for ((cpu=0;cpu<4096;cpu++)); do
        [[ -z ${seen[cpu]:-} ]] || PARSED_CPUS+=("$cpu")
    done
}

contains_cpu() {
    local wanted=$1 cpu
    shift
    for cpu in "$@"; do [[ $cpu != "$wanted" ]] || return 0; done
    return 1
}

canonical_cpus() {
    parse_cpu_list "$1" || return 1
    local IFS=,
    printf '%s' "${PARSED_CPUS[*]}"
}

check_cpu_availability() {
    local available=$1 dedicated=$2 cpu path
    local requested=() online=() isolated=()
    [[ $dedicated =~ ^[0-9]{1,4}$ ]] || return 2
    dedicated=$((10#$dedicated))
    parse_cpu_list "$available" || { echo 'Invalid available CPU list' >&2; return 2; }
    requested=("${PARSED_CPUS[@]}")
    ((${#requested[@]} > 1)) && contains_cpu "$dedicated" "${requested[@]}" || {
        echo 'CPUMAP CPU must belong to the available CPU set with at least one other CPU' >&2; return 1;
    }
    path="$SYS_ROOT/devices/system/cpu/online"
    [[ -r $path ]] && parse_cpu_list "$(< "$path")" || { echo 'Cannot read online CPUs' >&2; return 1; }
    online=("${PARSED_CPUS[@]}")
    for cpu in "${requested[@]}"; do
        contains_cpu "$cpu" "${online[@]}" || { echo "CPU$cpu is offline" >&2; return 1; }
    done
    path="$SYS_ROOT/devices/system/cpu/isolated"
    if [[ -r $path && -n $(< "$path") ]]; then
        parse_cpu_list "$(< "$path")" || return 1
        isolated=("${PARSED_CPUS[@]}")
    fi
    for cpu in "${requested[@]}"; do
        if contains_cpu "$cpu" "${isolated[@]}"; then
            echo "CPU$cpu has fixed boot isolation; remove its isolcpus=domain setting and reboot before mode-dependent experiments" >&2
            return 1
        fi
    done
    [[ -r "$CGROUP_ROOT/cgroup.controllers" ]] &&
        grep -qw cpuset "$CGROUP_ROOT/cgroup.controllers" || {
        echo 'cgroup v2 cpuset controller is unavailable' >&2; return 1;
    }
    [[ -r "$CGROUP_ROOT/cpuset.cpus.effective" ]] &&
        [[ $(canonical_cpus "$(< "$CGROUP_ROOT/cpuset.cpus.effective")") == $(canonical_cpus "$available") ]] || {
        echo 'Root cgroup does not expose the complete available CPU set' >&2; return 1;
    }
}

main() {
    if [[ ${1:-} == --help ]]; then
        echo 'Usage: check_cpu_partition.sh INTERFACE AVAILABLE_CPUS CPUMAP_CPU'
        return 0
    fi
    [[ $# == 3 ]] || return 2
    local interface=$1 available=$2 dedicated=$3 path value
    [[ $interface =~ ^[A-Za-z0-9_.:-]{1,15}$ ]] || return 2
    check_cpu_availability "$available" "$dedicated" || return
    [[ -d "$SYS_ROOT/class/net/$interface" ]] || { echo 'Network interface not found' >&2; return 1; }
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
    printf 'CPU availability verified: shared=%s, CPUMAP CPU%s reserved only during active mixed reception; RPS=off, separate threaded NAPI=off\n' \
        "$(canonical_cpus "$available")" "$dedicated"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
