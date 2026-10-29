#!/bin/bash

# ============================================================
# prepare_env.sh — 본시험 준비 단계의 C08/C10 적용
#
# 사용법: sudo bash monitor/prepare_env.sh receiver|sender eth0 performance 2
#   양측: CPUFreq policy마다 동일 governor 적용·확인
#   수신측만: 현재 NIC IRQ를 찾아 지정 CPU로 적용·확인
#
# 설정은 현재 부팅 세션에 유지한다. 실행 중 주기적으로 설정을 바꾸지 않는다.
# 중간 실패는 이 스크립트가 바꾼 governor를 복원하며 IRQ helper도 자체 복원한다.
# 커널 교체, NIC MTU/링/시간 병합, 스케줄링 우선순위는 변경하지 않는다.
# ============================================================

set -Eeuo pipefail
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SYS_ROOT=/sys
GOVERNOR_PATHS=()
GOVERNOR_ORIGINAL=()
GOVERNOR_CHANGED=0

prepare_exit()
{
    local status=$? i
    trap - EXIT
    if ((status && GOVERNOR_CHANGED)); then
        for ((i=0;i<${#GOVERNOR_PATHS[@]};i++)); do
            printf '%s\n' "${GOVERNOR_ORIGINAL[$i]}" > "${GOVERNOR_PATHS[$i]}" ||
                printf 'Error: cannot restore %s\n' "${GOVERNOR_PATHS[$i]}" >&2
        done
    fi
    exit "$status"
}

main()
{
    local role=${1:-} interface=${2:-} governor=${3:-} irq_cpu=${4:-} path available item found
    if [[ ${1:-} == --help ]]; then
        echo 'Usage: prepare_env.sh receiver|sender INTERFACE GOVERNOR RX_IRQ_CPU'
        return 0
    fi
    [[ $# == 4 && ($role == receiver || $role == sender) && $interface =~ ^[A-Za-z0-9_.:-]{1,15}$ &&
       $governor =~ ^[A-Za-z0-9_-]{1,31}$ && $irq_cpu =~ ^[0-9]{1,4}$ ]] || return 2
    [[ -d "$SYS_ROOT/class/net/$interface" ]] || { echo "Error: interface not found: $interface" >&2; return 1; }
    GOVERNOR_PATHS=(); GOVERNOR_ORIGINAL=(); GOVERNOR_CHANGED=0
    for path in "$SYS_ROOT/devices/system/cpu/cpufreq/"policy*/scaling_governor; do
        [[ -f $path ]] || continue
        available=$(< "${path%/*}/scaling_available_governors")
        found=0
        for item in $available; do [[ $item != "$governor" ]] || found=1; done
        ((found)) || { echo "Error: governor $governor is unsupported at $path" >&2; return 1; }
        GOVERNOR_PATHS+=("$path"); GOVERNOR_ORIGINAL+=("$(< "$path")")
    done
    ((${#GOVERNOR_PATHS[@]})) || { echo 'Error: CPUFreq policies unavailable; cannot apply the experimental governor' >&2; return 1; }
    trap prepare_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    for path in "${GOVERNOR_PATHS[@]}"; do
        if [[ $(< "$path") != "$governor" ]]; then
            GOVERNOR_CHANGED=1
            printf '%s\n' "$governor" > "$path" || return 1
        fi
        [[ $(< "$path") == "$governor" ]] || { echo "Error: governor read-back differs: $path" >&2; return 1; }
        printf '%s=%s\n' "$path" "$governor"
    done
    if [[ $role == receiver ]]; then
        bash "$SCRIPT_DIR/set_irq_affinity.sh" "$interface" "$irq_cpu" || return 1
    fi
    GOVERNOR_CHANGED=0
    printf 'Prepared role=%s interface=%s governor=%s\n' "$role" "$interface" "$governor"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
