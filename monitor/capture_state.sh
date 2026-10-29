#!/bin/bash

# ============================================================
# capture_state.sh — 각 run 전후 실행 상태 보존 (C07/C08/C09/C10)
#
# 사용법: bash monitor/capture_state.sh eth0 results/run/system-before.txt
# 출력 파일은 새로 만들며 기존 결과를 덮어쓰지 않는다.
# ace-run이 모든 패킷 캡처 전/후에만 호출한다. 측정 loop에서 실행하지 않는다.
# 지원하지 않는 센서·ethtool 기능은 unavailable/status로 남기고 0으로 채우지 않는다.
# ============================================================

set -Eeuo pipefail
export LC_ALL=C
export PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"

if [[ ${1:-} == --help ]]; then
    echo 'Usage: capture_state.sh INTERFACE NEW_OUTPUT_FILE'
    exit 0
fi
[[ $# == 2 && $1 =~ ^[A-Za-z0-9_.:-]{1,15}$ && -n $2 ]] || exit 2
interface=$1 output=$2
[[ -d /sys/class/net/$interface ]] || { echo "Error: interface not found: $interface" >&2; exit 1; }

# noclobber는 기존 파일과 symlink를 통한 덮어쓰기도 거부한다.
set -o noclobber
exec 3> "$output"
set +o noclobber

capture_command()
{
    local status
    printf '\n[command]'; printf ' %q' "$@"; printf '\n'
    if command -v "$1" >/dev/null 2>&1; then
        if timeout 5s "$@"; then status=0; else status=$?; fi
        printf 'command_status=%s\n' "$status"
    else
        printf 'unavailable=command_not_installed\n'
    fi
}

capture_file()
{
    local path=$1
    printf '\n[file] %s\n' "$path"
    if [[ -r $path ]]; then cat -- "$path"; printf '\n'; else echo 'unavailable=not_readable'; fi
}

{
    echo 'capture_schema=1'
    date -Is
    uname -a
    capture_file /proc/cmdline
    capture_file /sys/kernel/realtime
    capture_file /sys/devices/system/cpu/online
    capture_file /sys/devices/system/cpu/isolated
    capture_file /sys/devices/system/cpu/nohz_full
    for policy in /sys/devices/system/cpu/cpufreq/policy*; do
        [[ -d $policy ]] || continue
        for name in related_cpus scaling_governor scaling_cur_freq scaling_min_freq scaling_max_freq; do
            capture_file "$policy/$name"
        done
    done
    for zone in /sys/class/thermal/thermal_zone*; do
        [[ -d $zone ]] || continue
        capture_file "$zone/type"; capture_file "$zone/temp"
    done
    capture_command vcgencmd get_throttled
    capture_command vcgencmd measure_clock arm
    capture_file /proc/interrupts
    while IFS= read -r irq; do
        [[ -n $irq ]] || continue
        capture_file "/proc/irq/$irq/smp_affinity_list"
        capture_file "/proc/irq/$irq/effective_affinity_list"
    done < <(awk -v interface="$interface" '$1 ~ /^[0-9]+:$/ {for (i=2;i<=NF;i++) if ($i==interface || index($i,interface "-")==1 || index($i,interface ":")==1) {sub(/:$/,"",$1);print $1;break}}' /proc/interrupts)
    capture_file /proc/net/snmp
    capture_file /proc/net/softnet_stat
    capture_command ip -details link show dev "$interface"
    capture_command ethtool -i "$interface"
    capture_command ethtool -c "$interface"
    capture_command ethtool -g "$interface"
    capture_command ethtool -S "$interface"
    capture_command iperf3 --version
    capture_command stress-ng --version
    capture_command systemctl is-active irqbalance
    echo 'capture_complete=1'
} >&3
exec 3>&-
sync -f "$output"
