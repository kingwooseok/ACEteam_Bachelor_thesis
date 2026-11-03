#!/bin/bash
# Read-only snapshot after the loader/receivers are READY, before measurement.
# /proc stat fields: 39 processor, 40 rt_priority, 41 scheduling policy.
set -Eeuo pipefail
export LC_ALL=C

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/set_irq_affinity.sh"

json_string()
{
    local value=$1 character code i
    printf '"'
    for ((i=0;i<${#value};i++)); do
        character=${value:i:1}
        case $character in
            '"') printf '\\"' ;;
            '\') printf '\\\\' ;;
            *) printf -v code '%d' "'$character"
               if ((code < 32)); then printf '\\u%04x' "$code"; else printf '%s' "$character"; fi ;;
        esac
    done
    printf '"'
}

policy_name()
{
    case $1 in
        0) printf SCHED_OTHER ;; 1) printf SCHED_FIFO ;; 2) printf SCHED_RR ;;
        3) printf SCHED_BATCH ;; 5) printf SCHED_IDLE ;; 6) printf SCHED_DEADLINE ;;
        7) printf SCHED_EXT ;; *) printf UNKNOWN ;;
    esac
}

capture_scheduler()
{
    local interface=${1:-} output=${2:-} path tid name kind irq irq_number stat_line rest affinity key value
    local separator='' thread_status='not_present' count=0 missed=0 flags irq_count=0 cpumap_count=0
    local irq_status='no_matching_thread' cpumap_status='no_matching_thread'
    local -a irqs=() fields=()
    if [[ ${1:-} == --help ]]; then
        echo 'Usage: capture_scheduler.sh INTERFACE NEW_OUTPUT_FILE'
        return 0
    fi
    [[ $# == 2 && $interface =~ ^[A-Za-z0-9_.:-]{1,15}$ && -n $output ]] || return 2
    [[ -d "$SYS_ROOT/class/net/$interface" ]] || { echo "interface not found: $interface" >&2; return 1; }
    while IFS= read -r irq; do [[ -z $irq ]] || irqs+=("$irq"); done < <(nic_irqs "$interface")

    # Never overwrite another run's snapshot, including through a symlink.
    set -o noclobber
    exec 3> "$output"
    set +o noclobber
    {
        printf '{"schema_version":1,"interface":'; json_string "$interface"
        printf ',"phase":"ready","cpumap_scope":"all_visible_cpumap_kernel_threads","irq_ids":['
        for irq in "${irqs[@]}"; do printf '%s%s' "$separator" "$irq"; separator=','; done
        printf '],"threads":['
        separator=''
        # IRQ and CPUMAP kthreads are thread-group leaders. Reading their comm
        # avoids ps truncation/locale differences; PF_KTHREAD excludes user tasks.
        for path in "$PROC_ROOT"/[0-9]*/comm; do
            [[ -r $path ]] || continue
            IFS= read -r name < "$path" || continue
            tid=${path%/comm}; tid=${tid##*/}
            [[ $tid =~ ^[0-9]+$ ]] || continue
            kind='' irq_number='null'
            if [[ $name =~ ^cpumap/[0-9]+(/|$) ]]; then
                kind=cpumap
            else
                for irq in "${irqs[@]}"; do
                    if [[ $name == "irq/$irq-"* ]]; then kind=irq; irq_number=$irq; break; fi
                done
            fi
            [[ -n $kind ]] || continue
            stat_line=''; affinity=''
            if IFS= read -r stat_line < "$PROC_ROOT/$tid/stat" 2>/dev/null; then
                # comm may contain spaces and ')'; the last ') ' precedes state.
                rest=${stat_line##*) }
                read -ra fields <<< "$rest"
            else fields=(); fi
            if ((${#fields[@]} >= 39)) && [[ ${fields[6]} =~ ^[0-9]+$ ]]; then
                flags=${fields[6]}
                ((flags & 2097152)) || continue
            fi
            printf '%s{"tid":%s,"name":' "$separator" "$tid"; json_string "$name"
            printf ',"kind":"%s","irq":%s' "$kind" "$irq_number"
            separator=','
            count=$((count+1))
            if [[ $kind == irq ]]; then irq_count=$((irq_count+1)); else cpumap_count=$((cpumap_count+1)); fi
            if ((${#fields[@]} < 39)) || [[ ! ${fields[36]} =~ ^[0-9]+$ || ! ${fields[37]} =~ ^[0-9]+$ || ! ${fields[38]} =~ ^[0-9]+$ || ! ${fields[19]} =~ ^[0-9]+$ ]]; then
                printf ',"status":"stat_unavailable"}'
                missed=$((missed+1)); continue
            fi
            if [[ -r "$PROC_ROOT/$tid/status" ]]; then
                while IFS=: read -r key value; do
                    if [[ $key == Cpus_allowed_list ]]; then affinity=${value//[[:space:]]/}; break; fi
                done < "$PROC_ROOT/$tid/status"
            fi
            printf ',"policy_id":%s,"policy":"%s","rt_priority":%s,"last_cpu":%s,"start_time_ticks":%s' \
                "${fields[38]}" "$(policy_name "${fields[38]}")" "${fields[37]}" "${fields[36]}" "${fields[19]}"
            if [[ $affinity =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]]; then
                printf ',"cpus_allowed_list":'; json_string "$affinity"
                printf ',"status":"captured"}'
            else
                printf ',"cpus_allowed_list":null,"status":"affinity_unavailable"}'
                missed=$((missed+1))
            fi
        done
        ((count == 0)) || thread_status=captured
        ((missed == 0)) || thread_status=partial
        ((${#irqs[@]})) || irq_status=no_irq_found
        ((irq_count == 0)) || irq_status=present
        ((cpumap_count == 0)) || cpumap_status=present
        printf '],"status":"%s","irq_status":"%s","cpumap_status":"%s","thread_count":%s,"irq_thread_count":%s,"cpumap_thread_count":%s,"unavailable_count":%s,"complete":true}\n' \
            "$thread_status" "$irq_status" "$cpumap_status" "$count" "$irq_count" "$cpumap_count" "$missed"
    } >&3
    exec 3>&-
    sync -f "$output"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then capture_scheduler "$@"; fi
