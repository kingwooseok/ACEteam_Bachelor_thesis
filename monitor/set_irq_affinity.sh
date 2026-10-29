#!/bin/bash

# ============================================================
# set_irq_affinity.sh
#
# 목적:
#   수신 NIC의 현재 IRQ를 찾아 지정 CPU로 배치하고 실효값을 확인한다.
#   IRQ 번호는 부팅·드라이버에 따라 달라지므로 106 등으로 고정하지 않는다.
#
# 사용법:
#   sudo bash monitor/set_irq_affinity.sh eth0 2
#   bash monitor/set_irq_affinity.sh eth0 2 --check
#
# --check는 읽기 전용이다. 실제 패킷 처리 위치는 별도
# verify_irq_affinity.sh의 트래픽 중 counter delta로 검증한다.
# 여러 IRQ 중 하나라도 적용/검증에 실패하면 변경한 요청값을 복원한다.
# ============================================================

set -Eeuo pipefail

# 일반 실행에서는 항상 실제 proc/sys를 사용한다. 함수 단위 회귀 검사는
# source 이후 이 변수들만 임시 fixture 디렉터리로 바꿔 커널 변경을 피한다.
PROC_ROOT=/proc
SYS_ROOT=/sys
IRQ_IDS=()
IRQ_ORIGINAL=()
IRQ_CHANGED=0

cpu_in_list()
{
    local target=$1 list=$2 item first last
    local entries=()
    IFS=',' read -ra entries <<< "$list"
    for item in "${entries[@]}"; do
        [[ $item =~ ^[0-9]+(-[0-9]+)?$ ]] || return 1
        first=${item%-*}; last=${item#*-}
        ((target >= 10#$first && target <= 10#$last)) && return 0
    done
    return 1
}

nic_irqs()
{
    local interface=$1 path number
    local found=()
    # PCI NIC는 MSI 목록을 우선 사용한다. Pi의 platform/macb는 아래의
    # 정확한 interface label 탐색으로 처리한다(eth01/veth0와 구별).
    for path in "$SYS_ROOT/class/net/$interface/device/msi_irqs/"*; do
        [[ -e $path ]] || continue
        number=${path##*/}
        [[ $number =~ ^[0-9]+$ ]] && found+=("$number")
    done
    if ((${#found[@]})); then
        printf '%s\n' "${found[@]}" | sort -nu
    else
        awk -v interface="$interface" '
            $1 ~ /^[0-9]+:$/ {
                for (i=2;i<=NF;i++)
                    if ($i==interface || index($i,interface "-")==1 || index($i,interface ":")==1) {
                        sub(/:$/,"",$1); print $1; break
                    }
            }' "$PROC_ROOT/interrupts" | sort -nu
    fi
}

restore_irq_requests()
{
    local i path
    ((IRQ_CHANGED)) || return 0
    for ((i=0;i<${#IRQ_IDS[@]};i++)); do
        path="$PROC_ROOT/irq/${IRQ_IDS[$i]}/smp_affinity_list"
        if ! printf '%s\n' "${IRQ_ORIGINAL[$i]}" > "$path"; then
            printf 'Error: cannot restore IRQ %s to %s\n' "${IRQ_IDS[$i]}" "${IRQ_ORIGINAL[$i]}" >&2
        fi
    done
    IRQ_CHANGED=0
}

irq_exit()
{
    local status=$?
    trap - EXIT
    if ((status)); then restore_irq_requests; fi
    exit "$status"
}

main()
{
    local interface=${1:-} cpu=${2:-} mode=${3:-} irq requested effective attempt
    IRQ_IDS=(); IRQ_ORIGINAL=(); IRQ_CHANGED=0
    if [[ ${1:-} == --help ]]; then
        printf 'Usage: %s INTERFACE CPU [--check]\n' "$0"
        return 0
    fi
    [[ $# == 2 || ($# == 3 && $mode == --check) ]] || { echo 'Usage: set_irq_affinity.sh INTERFACE CPU [--check]' >&2; return 2; }
    [[ $interface =~ ^[A-Za-z0-9_.:-]{1,15}$ && $cpu =~ ^[0-9]{1,4}$ ]] || return 2
    cpu=$((10#$cpu))
    [[ -d "$SYS_ROOT/class/net/$interface" ]] || { echo "Error: interface not found: $interface" >&2; return 1; }
    cpu_in_list "$cpu" "$(< "$SYS_ROOT/devices/system/cpu/online")" || { echo "Error: CPU$cpu is not online" >&2; return 1; }
    while IFS= read -r irq; do
        [[ -n $irq ]] && IRQ_IDS+=("$irq")
    done < <(nic_irqs "$interface")
    ((${#IRQ_IDS[@]})) || { echo "Error: no IRQ found for $interface; bring the interface up first" >&2; return 1; }
    for irq in "${IRQ_IDS[@]}"; do
        [[ -r "$PROC_ROOT/irq/$irq/smp_affinity_list" && -r "$PROC_ROOT/irq/$irq/effective_affinity_list" ]] || {
            echo "Error: requested/effective affinity unavailable for IRQ $irq" >&2; return 1;
        }
        IRQ_ORIGINAL+=("$(< "$PROC_ROOT/irq/$irq/smp_affinity_list")")
    done
    trap irq_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    for irq in "${IRQ_IDS[@]}"; do
        requested=$(< "$PROC_ROOT/irq/$irq/smp_affinity_list")
        if [[ $mode != --check && $requested != "$cpu" ]]; then
            IRQ_CHANGED=1
            printf '%s\n' "$cpu" > "$PROC_ROOT/irq/$irq/smp_affinity_list" || return 1
        fi
        # 실효 affinity의 갱신을 짧게 기다리되 실패를 성공으로 숨기지 않는다.
        for ((attempt=0;attempt<10;attempt++)); do
            requested=$(< "$PROC_ROOT/irq/$irq/smp_affinity_list")
            effective=$(< "$PROC_ROOT/irq/$irq/effective_affinity_list")
            [[ $requested == "$cpu" && $effective == "$cpu" ]] && break
            [[ $mode == --check ]] && break
            sleep 0.05
        done
        if [[ $requested != "$cpu" || $effective != "$cpu" ]]; then
            printf 'Error: IRQ %s requested=%s effective=%s expected=%s\n' "$irq" "$requested" "$effective" "$cpu" >&2
            return 1
        fi
        printf 'IRQ %s: requested=%s effective=%s interface=%s\n' "$irq" "$requested" "$effective" "$interface"
    done
    IRQ_CHANGED=0
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
