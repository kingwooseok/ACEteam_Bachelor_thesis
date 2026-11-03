#!/usr/bin/env bash
# The native runner holds the global experiment lock and stops its children
# before release. This helper only changes its own /ace-rx-PID partition.
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/check_cpu_partition.sh"

partition_error() { printf '%s\n' "$*" >&2; return 1; }

cg_write() { printf '%s' "$2" > "$1"; }
cg_mkdir() { mkdir -- "$1"; }
cg_rmdir() { rmdir -- "$1"; }

validate_owner() {
    [[ $1 =~ ^[1-9][0-9]{0,9}$ ]] && ((10#$1 > 1)) || return 2
    [[ -d $2 && ! -L $2 ]] || return 2
}

controller_in_use() {
    local path value
    for path in "$CGROUP_ROOT"/*; do
        [[ -d $path ]] || continue
        if [[ -r "$path/cgroup.subtree_control" ]] && grep -qw cpuset "$path/cgroup.subtree_control"; then return 0; fi
        for value in cpuset.cpus cpuset.mems cpuset.cpus.exclusive; do
            if [[ -r "$path/$value" && -n $(< "$path/$value") ]]; then return 0; fi
        done
        if [[ -r "$path/cpuset.cpus.partition" && $(< "$path/cpuset.cpus.partition") != member ]]; then return 0; fi
    done
    return 1
}

release_partition() {
    local owner=$1 state_dir=$2
    validate_owner "$owner" "$state_dir" || return
    local group="$CGROUP_ROOT/ace-rx-$owner" state="$state_dir/cpu-partition.state"
    local values=() available dedicated added root enabled=false
    if [[ ! -e $state ]]; then
        [[ ! -e $group ]] || { partition_error 'Refusing to remove a partition without ownership state'; return 1; }
        return 0
    fi
    [[ -f $state && ! -L $state && ! -L $group ]] || return 1
    mapfile -t values < "$state"
    [[ ${#values[@]} == 4 && ${values[0]} == "$owner" && ${values[2]} =~ ^[0-9]{1,4}$ && ${values[3]} =~ ^[01]$ ]] || {
        partition_error 'Invalid CPU partition ownership state'; return 1;
    }
    available=$(canonical_cpus "${values[1]}") || return 1
    dedicated=$((10#${values[2]})); added=${values[3]}
    parse_cpu_list "$available" && contains_cpu "$dedicated" "${PARSED_CPUS[@]}" || return 1
    if [[ -d $group ]]; then
        [[ -r "$group/cgroup.procs" && -z $(< "$group/cgroup.procs") ]] || {
            partition_error 'CPU partition still has live processes; stop receiver children before release'; return 1;
        }
        cg_write "$group/cpuset.cpus.partition" member || return 1
        cg_rmdir "$group" || return 1
    fi
    root=$(canonical_cpus "$(< "$CGROUP_ROOT/cpuset.cpus.effective")") || return 1
    [[ $root == "$available" ]] || { partition_error 'Full CPU availability was not restored'; return 1; }
    if ((added)) && grep -qw cpuset "$CGROUP_ROOT/cgroup.subtree_control"; then
        # Preserve another administrator's new controller use after CPU3 returns.
        if ! controller_in_use; then cg_write "$CGROUP_ROOT/cgroup.subtree_control" -cpuset || return 1; fi
    fi
    if grep -qw cpuset "$CGROUP_ROOT/cgroup.subtree_control"; then enabled=true; fi
    if [[ ! -e "$state_dir/cpu-partition-restored.json" ]]; then
        (set -C; printf '{"owner_pid":%s,"available_cpus":"%s","reserved_cpu":%s,"partition":"released","root_cpus":"%s","root_cpuset_enabled":%s,"verified":true}\n' \
            "$owner" "$available" "$dedicated" "$root" "$enabled" > "$state_dir/cpu-partition-restored.json") || return 1
    elif [[ ! -f "$state_dir/cpu-partition-restored.json" || -L "$state_dir/cpu-partition-restored.json" ]]; then
        return 1
    fi
    printf 'CPU partition released: shared=%s\n' "$root"
}

acquire_partition() (
    local owner=$1 available=$2 dedicated=$3 state_dir=$4
    validate_owner "$owner" "$state_dir" || return
    check_cpu_availability "$available" "$dedicated" || return
    available=$(canonical_cpus "$available") || return
    dedicated=$((10#$dedicated))
    local group="$CGROUP_ROOT/ace-rx-$owner" state="$state_dir/cpu-partition.state"
    local general='' cpu added=0 committed=0 created=0 enabled_added=0
    local root partition actual
    [[ ! -e $group && ! -L $group ]] || { partition_error 'CPU partition path already exists'; return 1; }
    [[ ! -e $state && ! -L $state ]] || { partition_error 'CPU partition ownership state already exists'; return 1; }
    parse_cpu_list "$available" || return
    for cpu in "${PARSED_CPUS[@]}"; do
        [[ $cpu == "$dedicated" ]] || general+="${general:+,}$cpu"
    done
    grep -qw cpuset "$CGROUP_ROOT/cgroup.subtree_control" || added=1
    rollback() {
        local status=$?
        trap - EXIT INT TERM
        if (( ! committed )); then
            if (( created )); then
                if ! release_partition "$owner" "$state_dir"; then
                    printf 'CPU partition rollback failed; ownership state retained in %s\n' "$state_dir" >&2
                    status=1
                fi
            elif (( enabled_added )); then
                if ! controller_in_use; then cg_write "$CGROUP_ROOT/cgroup.subtree_control" -cpuset || status=1; fi
            fi
        fi
        exit "$status"
    }
    trap rollback EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # Record ownership before the first mutation. Noclobber protects old runs.
    (set -C; printf '%s\n' "$owner" "$available" "$dedicated" "$added" > "$state") || return 1
    if ((added)); then
        cg_write "$CGROUP_ROOT/cgroup.subtree_control" +cpuset || return 1
        enabled_added=1
    fi
    cg_mkdir "$group" || return 1
    created=1
    cg_write "$group/cpuset.cpus" "$dedicated" || return 1
    cg_write "$group/cpuset.cpus.partition" isolated || return 1
    partition=$(< "$group/cpuset.cpus.partition")
    actual=$(canonical_cpus "$(< "$group/cpuset.cpus.effective")") || return 1
    root=$(canonical_cpus "$(< "$CGROUP_ROOT/cpuset.cpus.effective")") || return 1
    [[ $partition == isolated && $actual == "$dedicated" && $root == "$general" ]] || {
        partition_error 'Dynamic CPU partition did not grant the requested exclusive CPU'; return 1;
    }
    (set -C; printf '{"owner_pid":%s,"available_cpus":"%s","reserved_cpu":%s,"general_cpus":"%s","partition":"%s","partition_cpus":"%s","root_cpus":"%s","root_cpuset_added":%s,"verified":true}\n' \
        "$owner" "$available" "$dedicated" "$general" "$partition" "$actual" "$root" \
        "$([[ $added == 1 ]] && echo true || echo false)" > "$state_dir/cpu-partition-active.json") || return 1
    committed=1
    printf 'CPU partition acquired: shared=%s reserved=%s group=%s\n' "$root" "$dedicated" "$group"
)

partition_main() {
    case ${1:-} in
        acquire) [[ $# == 5 ]] || return 2; acquire_partition "$2" "$3" "$4" "$5" ;;
        release) [[ $# == 3 ]] || return 2; release_partition "$2" "$3" ;;
        --help) echo 'Usage: cpu_partition.sh acquire PID AVAILABLE_CPUS CPUMAP_CPU STATE_DIR | release PID STATE_DIR' ;;
        *) return 2 ;;
    esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then partition_main "$@"; fi
