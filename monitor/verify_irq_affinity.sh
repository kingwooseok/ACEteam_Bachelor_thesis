#!/bin/bash

set -euo pipefail

# ============================================================
# verify_irq_affinity.sh
#
# 목적:
#   특정 네트워크 인터페이스의 IRQ affinity 설정과
#   실제 IRQ 처리 CPU가 일치하는지 검증한다.
#
# ------------------------------------------------------------
# 이 스크립트가 하는 일:
#
#   1) 대상 NIC의 IRQ 탐색
#   2) smp_affinity_list 확인
#   3) effective_affinity_list 확인
#   4) /proc/interrupts BEFORE 저장
#   5) 측정 시간 동안 mpstat -I CPU 기록
#   6) /proc/interrupts AFTER 저장
#   7) CPU별 IRQ 증가량(delta) 계산
#   8) 기대 CPU와 실제 IRQ 처리 CPU 비교
#
# ------------------------------------------------------------
# [중요: IRQ affinity 설정]
#
# 이 스크립트는 IRQ affinity를 설정하거나 변경하지 않는다.
#
# 사용자가 실험 전에 직접 원하는 CPU에 affinity를
# 설정해야 한다.
#
# 예: IRQ 106을 CPU2에 고정
#
#   echo 2 | sudo tee /proc/irq/106/smp_affinity_list
#
# 설정 확인:
#
#   cat /proc/irq/106/smp_affinity_list
#   cat /proc/irq/106/effective_affinity_list
#
# ------------------------------------------------------------
# [IRQ affinity 고정 해제 / 원상복구]
#
# 테스트가 끝난 뒤 특정 CPU 고정을 해제하려면
# 사용자가 직접 원래 affinity 범위로 복구한다.
#
# 현재 4코어 환경에서 원래 값이 0-3이었다면:
#
#   echo 0-3 | sudo tee /proc/irq/106/smp_affinity_list
#
# 복구 확인:
#
#   cat /proc/irq/106/smp_affinity_list
#   cat /proc/irq/106/effective_affinity_list
#
# 두 값이 0-3이면 CPU0~CPU3 전체 허용 상태이다.
#
# 주의:
#   시스템마다 원래 affinity 값이 다를 수 있으므로
#   변경 전에 기존 값을 반드시 확인해둔다.
#
# ------------------------------------------------------------
# [네트워크 트래픽]
#
# 이 스크립트는 네트워크 트래픽을 생성하지 않는다.
#
# IRQ affinity의 실제 동작을 검증하려면 측정 구간 동안
# 대상 NIC에 실제 네트워크 트래픽이 존재해야 한다.
#
# 트래픽 생성 방식은 이 스크립트와 무관하다.
#
#   - controller.sh가 생성하는 network load
#   - 실제 프로젝트 application 송수신
#   - iperf3
#   - 기타 네트워크 트래픽
#
# controller.sh가 이미 network load를 생성 중이라면
# 별도의 트래픽을 추가로 발생시킬 필요가 없다.
#
# 이 스크립트는 현재 IRQ affinity 설정과
# 실제 CPU별 IRQ 처리 위치만 관찰하고 검증한다.
#
# ------------------------------------------------------------
# [결과 해석]
#
# IRQ affinity 실제 동작 판정의 주 근거:
#
#   /proc/interrupts BEFORE/AFTER의
#   대상 IRQ CPU별 counter delta
#
# 보조 근거:
#
#   mpstat -I CPU
#
# network RX/TX delta:
#
#   측정 구간 동안 대상 NIC에 실제 트래픽이 존재했는지
#   확인하기 위한 sanity check 용도이다.
#
# ------------------------------------------------------------
# 사용법:
#
#   sudo ./verify_irq_affinity.sh \
#       <interface> <expected_cpu> [duration_sec] [irq]
#
# 예:
#
#   sudo ./verify_irq_affinity.sh eth0 2 60
#
# IRQ 자동 탐지가 어려운 경우:
#
#   sudo ./verify_irq_affinity.sh eth0 2 60 106
#
# 기본 측정 시간:
#
#   60 sec
#
# ============================================================


# ------------------------------------------------------------
# 입력값
# ------------------------------------------------------------

IFACE="${1:-}"
EXPECTED_CPU="${2:-}"
DURATION="${3:-60}"
IRQ_OVERRIDE="${4:-}"

COUNTDOWN_SEC=5
MPSTAT_INTERVAL=1

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

RESULT_ROOT="./results"
OUTDIR="${RESULT_ROOT}/irq_affinity_${IFACE}_cpu${EXPECTED_CPU}_${TIMESTAMP}"


# ------------------------------------------------------------
# 사용법 확인
# ------------------------------------------------------------

if [[ -z "${IFACE}" || -z "${EXPECTED_CPU}" ]]; then

    echo "Usage:"
    echo "  sudo $0 <interface> <expected_cpu> [duration_sec] [irq]"
    echo
    echo "Example:"
    echo "  sudo $0 eth0 2 60"
    echo "  sudo $0 eth0 2 60 106"

    exit 1
fi


# ------------------------------------------------------------
# 입력값 검증
# ------------------------------------------------------------

if ! [[ "${EXPECTED_CPU}" =~ ^[0-9]+$ ]]; then
    echo "[ERROR] expected_cpu는 CPU 번호여야 합니다."
    exit 1
fi

if ! [[ "${DURATION}" =~ ^[0-9]+$ ]] ||
   [[ "${DURATION}" -le 0 ]]; then

    echo "[ERROR] duration은 양의 정수여야 합니다."
    exit 1
fi

if [[ -n "${IRQ_OVERRIDE}" ]] &&
   ! [[ "${IRQ_OVERRIDE}" =~ ^[0-9]+$ ]]; then

    echo "[ERROR] IRQ 번호는 정수여야 합니다."
    exit 1
fi


# ------------------------------------------------------------
# 필요한 명령어 확인
# ------------------------------------------------------------

for cmd in mpstat ip awk grep sed sort nproc; do

    if ! command -v "${cmd}" >/dev/null 2>&1; then

        echo "[ERROR] '${cmd}' command not found."

        if [[ "${cmd}" == "mpstat" ]]; then
            echo
            echo "Install:"
            echo "  sudo apt install sysstat"
        fi

        exit 1
    fi
done


# ------------------------------------------------------------
# 인터페이스 확인
# ------------------------------------------------------------

if [[ ! -d "/sys/class/net/${IFACE}" ]]; then
    echo "[ERROR] Interface '${IFACE}' not found."
    exit 1
fi


# ------------------------------------------------------------
# 기대 CPU 존재 여부 확인
# ------------------------------------------------------------

if [[ ! -d "/sys/devices/system/cpu/cpu${EXPECTED_CPU}" ]]; then
    echo "[ERROR] CPU${EXPECTED_CPU} does not exist."
    exit 1
fi

if [[ -f "/sys/devices/system/cpu/cpu${EXPECTED_CPU}/online" ]]; then

    CPU_ONLINE="$(cat "/sys/devices/system/cpu/cpu${EXPECTED_CPU}/online")"

    if [[ "${CPU_ONLINE}" != "1" ]]; then
        echo "[ERROR] CPU${EXPECTED_CPU} is offline."
        exit 1
    fi
fi


# ------------------------------------------------------------
# 결과 디렉터리 / 파일
# ------------------------------------------------------------

mkdir -p "${OUTDIR}"

SYSTEM_INFO="${OUTDIR}/system_info.txt"
AFFINITY_FILE="${OUTDIR}/affinity.txt"

INTERRUPTS_BEFORE="${OUTDIR}/interrupts_before.txt"
INTERRUPTS_AFTER="${OUTDIR}/interrupts_after.txt"

IRQ_DELTA="${OUTDIR}/irq_delta.txt"
MPSTAT_LOG="${OUTDIR}/mpstat_irq.log"


# ------------------------------------------------------------
# IRQ 탐색
# ------------------------------------------------------------

declare -A IRQ_SET=()


add_irq()
{
    local irq="$1"

    if [[ "${irq}" =~ ^[0-9]+$ ]] &&
       [[ -d "/proc/irq/${irq}" ]]; then

        IRQ_SET["${irq}"]=1
    fi
}


if [[ -n "${IRQ_OVERRIDE}" ]]; then

    # 사용자가 직접 IRQ를 지정한 경우 자동 탐색하지 않는다.
    add_irq "${IRQ_OVERRIDE}"

else

    # --------------------------------------------------------
    # 1. MSI IRQ 탐색
    # --------------------------------------------------------

    MSI_DIR="/sys/class/net/${IFACE}/device/msi_irqs"

    if [[ -d "${MSI_DIR}" ]]; then

        for path in "${MSI_DIR}"/*; do

            [[ -e "${path}" ]] || continue

            add_irq "$(basename "${path}")"

        done
    fi


    # --------------------------------------------------------
    # 2. device/irq 확인
    # --------------------------------------------------------

    IRQ_FILE="/sys/class/net/${IFACE}/device/irq"

    if [[ -r "${IRQ_FILE}" ]]; then

        irq="$(cat "${IRQ_FILE}")"

        if [[ "${irq}" != "0" ]]; then
            add_irq "${irq}"
        fi
    fi


    # --------------------------------------------------------
    # 3. /proc/interrupts에서 인터페이스 이름 탐색
    #
    # 백포팅/드라이버 구조에 따라 sysfs 정보가 충분하지
    # 않을 수 있으므로 /proc/interrupts도 함께 확인한다.
    # --------------------------------------------------------

    while IFS= read -r line; do

        irq="$(
            echo "${line}" |
            awk -F: '{gsub(/[[:space:]]/, "", $1); print $1}'
        )"

        add_irq "${irq}"

    done < <(grep -F "${IFACE}" /proc/interrupts || true)

fi


# ------------------------------------------------------------
# IRQ 목록 생성
# ------------------------------------------------------------

mapfile -t IRQS < <(
    printf '%s\n' "${!IRQ_SET[@]}" |
    sort -n
)


if [[ "${#IRQS[@]}" -eq 0 ]]; then

    echo "[ERROR] No IRQ found for interface '${IFACE}'."
    echo
    echo "Backported/custom driver 환경이면 IRQ 번호를 직접 지정하세요:"
    echo
    echo "  sudo $0 ${IFACE} ${EXPECTED_CPU} ${DURATION} <IRQ>"
    echo
    echo "Example:"
    echo "  sudo $0 ${IFACE} ${EXPECTED_CPU} ${DURATION} 106"

    exit 1
fi


# ------------------------------------------------------------
# CPU 개수
#
# /proc/interrupts 첫 줄의 CPU column 수를 기준으로 한다.
# ------------------------------------------------------------

CPU_COUNT="$(
    awk 'NR == 1 { print NF }' /proc/interrupts
)"


# ------------------------------------------------------------
# NIC 통계 읽기
# ------------------------------------------------------------

read_net_stat()
{
    local name="$1"
    local file="/sys/class/net/${IFACE}/statistics/${name}"

    if [[ -r "${file}" ]]; then
        cat "${file}"
    else
        echo 0
    fi
}


# ------------------------------------------------------------
# IRQ별 CPU counter 읽기
#
# 출력 예:
#
#   100 20 0 0
#
# CPU0 CPU1 CPU2 CPU3 순서
# ------------------------------------------------------------

get_irq_counts()
{
    local FILE="$1"
    local IRQ="$2"

    awk \
        -v irq="${IRQ}" \
        -v cpu_count="${CPU_COUNT}" \
        '
        $1 == irq ":" {

            for (i = 2; i <= cpu_count + 1; i++) {

                printf "%s", $i

                if (i < cpu_count + 1)
                    printf " "
            }

            printf "\n"
        }
        ' \
        "${FILE}"
}


# ------------------------------------------------------------
# 시스템 정보 저장
# ------------------------------------------------------------

{
    echo "============================================================"
    echo "IRQ Affinity Verification"
    echo "============================================================"
    echo

    echo "TIMESTAMP    : ${TIMESTAMP}"
    echo "INTERFACE    : ${IFACE}"
    echo "EXPECTED_CPU : ${EXPECTED_CPU}"
    echo "DURATION_SEC : ${DURATION}"
    echo "CPU_COUNT    : ${CPU_COUNT}"

    echo
    echo "Detected IRQs:"
    printf '%s\n' "${IRQS[@]}"

    echo
    echo "============================================================"
    echo "Kernel"
    echo "============================================================"

    uname -a

    echo
    echo "Kernel release:"
    uname -r

    echo
    echo "============================================================"
    echo "Interface"
    echo "============================================================"

    ip -details link show "${IFACE}" || true

    echo
    echo "============================================================"
    echo "Driver"
    echo "============================================================"

    if command -v ethtool >/dev/null 2>&1; then
        ethtool -i "${IFACE}" 2>&1 || true
    else
        echo "ethtool not installed"
    fi

    echo
    echo "============================================================"
    echo "irqbalance"
    echo "============================================================"

    if command -v systemctl >/dev/null 2>&1; then
        systemctl is-active irqbalance 2>&1 || true
    else
        echo "systemctl unavailable"
    fi

} > "${SYSTEM_INFO}"


# ------------------------------------------------------------
# Affinity 설정 읽기
#
# 설정 상태만 확인하며 어떤 affinity 값도 변경하지 않는다.
# ------------------------------------------------------------

CONFIG_PASS=1

{
    echo "============================================================"
    echo "IRQ Affinity"
    echo "============================================================"
    echo

    echo "Interface    : ${IFACE}"
    echo "Expected CPU : ${EXPECTED_CPU}"
    echo

    for irq in "${IRQS[@]}"; do

        echo "------------------------------------------------------------"
        echo "IRQ ${irq}"
        echo "------------------------------------------------------------"

        SMP_FILE="/proc/irq/${irq}/smp_affinity_list"
        EFFECTIVE_FILE="/proc/irq/${irq}/effective_affinity_list"

        if [[ -r "${SMP_FILE}" ]]; then
            SMP="$(tr -d '[:space:]' < "${SMP_FILE}")"
        else
            SMP="not available"
        fi

        if [[ -r "${EFFECTIVE_FILE}" ]]; then
            EFFECTIVE="$(tr -d '[:space:]' < "${EFFECTIVE_FILE}")"
        else
            EFFECTIVE="not available"
        fi

        echo "smp_affinity_list       : ${SMP}"
        echo "effective_affinity_list : ${EFFECTIVE}"

        if [[ "${SMP}" == "${EXPECTED_CPU}" ]]; then
            echo "smp affinity check      : PASS"
        else
            echo "smp affinity check      : FAIL"
            CONFIG_PASS=0
        fi

        if [[ "${EFFECTIVE}" == "${EXPECTED_CPU}" ]]; then
            echo "effective affinity check: PASS"
        else
            echo "effective affinity check: FAIL"
            CONFIG_PASS=0
        fi

        echo

    done

    echo "============================================================"

    if [[ "${CONFIG_PASS}" -eq 1 ]]; then
        echo "CONFIG CHECK: PASS"
    else
        echo "CONFIG CHECK: FAIL"
    fi

    echo "============================================================"

} > "${AFFINITY_FILE}"


# ------------------------------------------------------------
# 현재 정보 출력
# ------------------------------------------------------------

echo
echo "============================================================"
echo " IRQ affinity verification"
echo "============================================================"
echo "Interface    : ${IFACE}"
echo "Expected CPU : CPU${EXPECTED_CPU}"
echo "Duration     : ${DURATION} sec"
echo "IRQ(s)       : ${IRQS[*]}"
echo "Result dir   : ${OUTDIR}"
echo "============================================================"

echo
cat "${AFFINITY_FILE}"


if [[ "${CONFIG_PASS}" -ne 1 ]]; then

    echo
    echo "[WARNING]"
    echo "현재 affinity 설정이 CPU${EXPECTED_CPU} 단일 CPU로"
    echo "설정되어 있지 않습니다."
    echo
    echo "스크립트는 설정을 변경하지 않고 측정을 계속합니다."

fi


# ------------------------------------------------------------
# 네트워크 트래픽 안내
#
# controller.sh가 이미 network load를 생성 중이라면
# 별도의 트래픽을 추가로 실행할 필요가 없다.
# ------------------------------------------------------------

echo
echo "============================================================"
echo " Network traffic"
echo "============================================================"
echo
echo "측정 구간 동안 ${IFACE}에 실제 네트워크 트래픽이 존재해야 합니다."
echo
echo "controller.sh가 이미 network load를 생성 중이라면"
echo "별도의 트래픽을 추가로 실행할 필요가 없습니다."
echo
echo "트래픽 생성 방식은 이 검증 스크립트와 무관합니다."
echo
echo "측정은 ${COUNTDOWN_SEC}초 후 시작합니다."
echo


for ((i=COUNTDOWN_SEC; i>=1; i--)); do
    echo -ne "Starting in ${i} sec...\r"
    sleep 1
done

echo
echo "[START] IRQ measurement"


# ------------------------------------------------------------
# BEFORE
#
# 특정 IRQ의 실제 CPU별 증가량을 확인하기 위한 기준 시점
# ------------------------------------------------------------

cat /proc/interrupts > "${INTERRUPTS_BEFORE}"

RX_PACKETS_BEFORE="$(read_net_stat rx_packets)"
RX_BYTES_BEFORE="$(read_net_stat rx_bytes)"

TX_PACKETS_BEFORE="$(read_net_stat tx_packets)"
TX_BYTES_BEFORE="$(read_net_stat tx_bytes)"


# ------------------------------------------------------------
# mpstat 시작
#
# /proc/interrupts delta가 실제 판정의 주 근거이며
# mpstat -I CPU는 시간축 확인을 위한 보조 자료이다.
# ------------------------------------------------------------

mpstat \
    -I CPU \
    "${MPSTAT_INTERVAL}" \
    "${DURATION}" \
    > "${MPSTAT_LOG}" 2>&1 &

MPSTAT_PID=$!


# ------------------------------------------------------------
# 측정 시간 대기
# ------------------------------------------------------------

sleep "${DURATION}"


# ------------------------------------------------------------
# AFTER
# ------------------------------------------------------------

cat /proc/interrupts > "${INTERRUPTS_AFTER}"

RX_PACKETS_AFTER="$(read_net_stat rx_packets)"
RX_BYTES_AFTER="$(read_net_stat rx_bytes)"

TX_PACKETS_AFTER="$(read_net_stat tx_packets)"
TX_BYTES_AFTER="$(read_net_stat tx_bytes)"


# mpstat 종료 대기
wait "${MPSTAT_PID}" 2>/dev/null || true


# ------------------------------------------------------------
# 네트워크 통계 delta
#
# affinity 판정 자체가 아니라 측정 중 실제 NIC activity가
# 있었는지 확인하기 위한 sanity check 용도
# ------------------------------------------------------------

RX_PACKETS_DELTA=$((RX_PACKETS_AFTER - RX_PACKETS_BEFORE))
RX_BYTES_DELTA=$((RX_BYTES_AFTER - RX_BYTES_BEFORE))

TX_PACKETS_DELTA=$((TX_PACKETS_AFTER - TX_PACKETS_BEFORE))
TX_BYTES_DELTA=$((TX_BYTES_AFTER - TX_BYTES_BEFORE))


# ------------------------------------------------------------
# IRQ Delta 계산
# ------------------------------------------------------------

declare -a TOTAL_DELTA

for ((cpu=0; cpu<CPU_COUNT; cpu++)); do
    TOTAL_DELTA[$cpu]=0
done


RUNTIME_DATA_VALID=1


{
    echo "============================================================"
    echo "IRQ Delta"
    echo "============================================================"

    echo
    echo "Interface    : ${IFACE}"
    echo "Expected CPU : CPU${EXPECTED_CPU}"
    echo "Duration     : ${DURATION} sec"

    echo
    echo "============================================================"
    echo "Network interface activity"
    echo "============================================================"

    echo
    echo "RX packets delta : ${RX_PACKETS_DELTA}"
    echo "RX bytes delta   : ${RX_BYTES_DELTA}"
    echo "TX packets delta : ${TX_PACKETS_DELTA}"
    echo "TX bytes delta   : ${TX_BYTES_DELTA}"

    echo


    for irq in "${IRQS[@]}"; do

        BEFORE_LINE="$(get_irq_counts "${INTERRUPTS_BEFORE}" "${irq}")"
        AFTER_LINE="$(get_irq_counts "${INTERRUPTS_AFTER}" "${irq}")"

        echo "------------------------------------------------------------"
        echo "IRQ ${irq}"
        echo "------------------------------------------------------------"


        if [[ -z "${BEFORE_LINE}" || -z "${AFTER_LINE}" ]]; then

            echo "Unable to read IRQ counters."
            RUNTIME_DATA_VALID=0
            echo
            continue
        fi


        read -ra BEFORE_COUNTS <<< "${BEFORE_LINE}"
        read -ra AFTER_COUNTS <<< "${AFTER_LINE}"


        for ((cpu=0; cpu<CPU_COUNT; cpu++)); do

            before="${BEFORE_COUNTS[$cpu]}"
            after="${AFTER_COUNTS[$cpu]}"

            delta=$((after - before))

            TOTAL_DELTA[$cpu]=$((TOTAL_DELTA[$cpu] + delta))

            printf "CPU%-2d BEFORE=%-12d AFTER=%-12d DELTA=%d\n" \
                "${cpu}" \
                "${before}" \
                "${after}" \
                "${delta}"
        done

        echo

    done


    echo "============================================================"
    echo "Aggregated IRQ delta"
    echo "============================================================"
    echo


    TOTAL_IRQ_DELTA=0

    for ((cpu=0; cpu<CPU_COUNT; cpu++)); do

        printf "CPU%-2d : %d\n" \
            "${cpu}" \
            "${TOTAL_DELTA[$cpu]}"

        TOTAL_IRQ_DELTA=$((TOTAL_IRQ_DELTA + TOTAL_DELTA[$cpu]))

    done


    echo
    echo "Total IRQ delta: ${TOTAL_IRQ_DELTA}"

    echo
    echo "============================================================"
    echo "Runtime verification"
    echo "============================================================"
    echo


    # --------------------------------------------------------
    # 실제 IRQ 동작 판정
    #
    # PASS:
    #   기대 CPU에서만 대상 IRQ가 증가
    #
    # FAIL:
    #   기대 CPU에서 증가하지 않거나
    #   다른 CPU에서도 대상 IRQ가 증가
    #
    # INCONCLUSIVE:
    #   측정 중 대상 IRQ activity가 관찰되지 않음
    # --------------------------------------------------------

    RUNTIME_RESULT="PASS"

    if [[ "${RUNTIME_DATA_VALID}" -ne 1 ]]; then

        RUNTIME_RESULT="INCONCLUSIVE"

    elif [[ "${TOTAL_IRQ_DELTA}" -eq 0 ]]; then

        RUNTIME_RESULT="INCONCLUSIVE"

    else

        EXPECTED_DELTA="${TOTAL_DELTA[$EXPECTED_CPU]}"

        if [[ "${EXPECTED_DELTA}" -eq 0 ]]; then
            RUNTIME_RESULT="FAIL"
        fi


        for ((cpu=0; cpu<CPU_COUNT; cpu++)); do

            if [[ "${cpu}" -ne "${EXPECTED_CPU}" ]] &&
               [[ "${TOTAL_DELTA[$cpu]}" -ne 0 ]]; then

                RUNTIME_RESULT="FAIL"

            fi
        done

    fi


    echo "Expected CPU : CPU${EXPECTED_CPU}"

    echo
    echo "Runtime result: ${RUNTIME_RESULT}"


    if [[ "${RUNTIME_RESULT}" == "PASS" ]]; then

        echo
        echo "IRQ activity was observed only on CPU${EXPECTED_CPU}."

    elif [[ "${RUNTIME_RESULT}" == "FAIL" ]]; then

        echo
        echo "IRQ activity does not match the expected CPU."

    else

        echo
        echo "No usable IRQ activity was observed."
        echo "Check whether network traffic reached ${IFACE} during measurement."

    fi


    echo
    echo "============================================================"
    echo "Overall"
    echo "============================================================"
    echo


    if [[ "${CONFIG_PASS}" -eq 1 ]] &&
       [[ "${RUNTIME_RESULT}" == "PASS" ]]; then

        OVERALL="PASS"

    elif [[ "${RUNTIME_RESULT}" == "INCONCLUSIVE" ]]; then

        OVERALL="INCONCLUSIVE"

    else

        OVERALL="FAIL"

    fi


    echo "CONFIG CHECK : $([[ "${CONFIG_PASS}" -eq 1 ]] && echo PASS || echo FAIL)"
    echo "RUNTIME CHECK: ${RUNTIME_RESULT}"
    echo "OVERALL      : ${OVERALL}"


} > "${IRQ_DELTA}"


# ------------------------------------------------------------
# 결과 출력
# ------------------------------------------------------------

echo
echo "[DONE] IRQ measurement completed."
echo

cat "${IRQ_DELTA}"


# ------------------------------------------------------------
# 저장 파일 출력
# ------------------------------------------------------------

echo
echo "============================================================"
echo " Saved files"
echo "============================================================"

echo "${SYSTEM_INFO}"
echo "${AFFINITY_FILE}"
echo "${INTERRUPTS_BEFORE}"
echo "${INTERRUPTS_AFTER}"
echo "${IRQ_DELTA}"
echo "${MPSTAT_LOG}"

echo "============================================================"


# ------------------------------------------------------------
# sudo 실행 시 결과 파일 소유권을 원 사용자에게 반환
#
# 시스템의 IRQ affinity 설정은 변경하지 않는다.
# ------------------------------------------------------------

if [[ -n "${SUDO_USER:-}" ]] &&
   [[ "${SUDO_USER}" != "root" ]]; then

    USER_GROUP="$(id -gn "${SUDO_USER}")"

    chown -R \
        "${SUDO_USER}:${USER_GROUP}" \
        "${OUTDIR}"
fi


echo
echo "[DONE] IRQ affinity verification completed."