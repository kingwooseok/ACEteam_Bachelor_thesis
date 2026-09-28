#!/bin/bash

set -euo pipefail

# ============================================================
# verify_cyclictest.sh
#
# 목적:
#   Case별 scheduling latency를 cyclictest로 검증한다.
#
# 중요:
#   실험 부하(CPU / network)는 별도로 실행 중인 controller.sh가 담당한다.
#   이 스크립트는 stress-ng, iperf3 등 어떤 부하도 생성하지 않는다.
#   현재 실험 환경에서 cyclictest를 한 번 실행하고 결과만 저장한다.
#
# cyclictest 설정:
#   - 모든 online CPU 측정 (--smp)
#   - SCHED_FIFO priority 80
#   - interval 200 us
#   - memory locking
#   - latency histogram 저장
#
# ------------------------------------------------------------
# [중요: 최종 Case 비교 전 반드시 확인]
#
# Case 1과 Case 2는 동일한 CPU governor 상태에서 측정해야 한다.
#
# 예:
#   Case 1 = ondemand    -> Case 2도 ondemand
#   또는
#   Case 1 = performance -> Case 2도 performance
#
# 이 스크립트는 CPU governor를 변경하지 않는다.
# 현재 governor는 system_info.txt에 자동 기록된다.
#
# 따라서 최종 실험 전 두 Case의 governor가 동일한지 반드시 확인한다.
#
# ------------------------------------------------------------
# Case 정의:
#
#   case1 = Vanilla Kernel
#   case2 = PREEMPT_RT Kernel
#
# 주의:
#   case1 / case2 인자는 단순 결과 라벨이다.
#   커널을 자동으로 변경하거나 검증해 주는 기능은 아니다.
#
# ------------------------------------------------------------
# 사용법:
#
#   sudo ./verify_cyclictest.sh <case> [duration_sec]
#
# 예:
#   sudo ./verify_cyclictest.sh case1 300
#   sudo ./verify_cyclictest.sh case2 300
#
# 기본값:
#   duration = 60 sec
#
# ============================================================


# ------------------------------------------------------------
# 기본 설정
# ------------------------------------------------------------

CASE_NAME="${1:-}"
DURATION="${2:-60}"

PRIORITY=80
INTERVAL_US=200
HIST_MAX_US=1000

CPU_COUNT="$(nproc)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

RESULT_ROOT="./results"
OUTDIR="${RESULT_ROOT}/cyclictest_${CASE_NAME}_${TIMESTAMP}"


# ------------------------------------------------------------
# 사용법 확인
# ------------------------------------------------------------

if [[ -z "${CASE_NAME}" ]]; then
    echo "Usage:"
    echo "  sudo $0 <case_name> [duration_sec]"
    echo
    echo "Example:"
    echo "  sudo $0 case1 300"
    echo "  sudo $0 case2 300"
    exit 1
fi


# ------------------------------------------------------------
# root 권한 확인
# ------------------------------------------------------------

if [[ "${EUID}" -ne 0 ]]; then
    echo "[ERROR] cyclictest RT priority 설정을 위해 root 권한이 필요합니다."
    echo
    echo "Run:"
    echo "  sudo $0 ${CASE_NAME} ${DURATION}"
    exit 1
fi


# ------------------------------------------------------------
# 필요한 프로그램 확인
# ------------------------------------------------------------

for cmd in cyclictest nproc lscpu; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        echo "[ERROR] '${cmd}' command not found."
        echo
        if [[ "${cmd}" == "cyclictest" ]]; then
            echo "Install:"
            echo "  sudo apt install rt-tests"
        fi
        exit 1
    fi
done


# ------------------------------------------------------------
# 입력값 확인
# ------------------------------------------------------------

if ! [[ "${DURATION}" =~ ^[0-9]+$ ]] ||
   [[ "${DURATION}" -le 0 ]]; then

    echo "[ERROR] duration은 양의 정수여야 합니다."
    exit 1
fi


# ------------------------------------------------------------
# 결과 디렉터리 생성
# ------------------------------------------------------------

mkdir -p "${OUTDIR}"

META_FILE="${OUTDIR}/system_info.txt"
RESULT_FILE="${OUTDIR}/cyclictest.txt"
SUMMARY_FILE="${OUTDIR}/summary.txt"


# ------------------------------------------------------------
# Histogram 옵션 확인
#
# --histofall 지원:
#   CPU별 histogram + 전체 summary column 저장
#
# 미지원:
#   일반 --histogram 사용
# ------------------------------------------------------------

if cyclictest --help 2>&1 | grep -q -- "--histofall"; then
    HIST_OPTION="--histofall=${HIST_MAX_US}"
else
    HIST_OPTION="--histogram=${HIST_MAX_US}"
fi


# ------------------------------------------------------------
# cyclictest 공통 옵션
#
# histogram 모드에서는 cyclictest가 thread distance를 0으로
# 처리하므로 --distance 옵션은 따로 지정하지 않는다.
# ------------------------------------------------------------

CYCLIC_ARGS=(
    --mlockall
    --smp
    --priority="${PRIORITY}"
    --interval="${INTERVAL_US}"
    --duration="${DURATION}s"
    "${HIST_OPTION}"
    --quiet
)


# ------------------------------------------------------------
# 현재 CPU governor 출력 함수
# ------------------------------------------------------------

print_governors()
{
    local found=0

    for governor in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        if [[ -f "${governor}" ]]; then
            echo "${governor}: $(cat "${governor}")"
            found=1
        fi
    done

    if [[ "${found}" -eq 0 ]]; then
        echo "CPU governor information not available."
    fi
}


# ------------------------------------------------------------
# 핵심 summary 추출 함수
# ------------------------------------------------------------

extract_summary()
{
    local INPUT_FILE="$1"
    local OUTPUT_FILE="$2"

    grep -E \
        "^# (Min Latencies|Avg Latencies|Max Latencies|Histogram Overflows)" \
        "${INPUT_FILE}" \
        > "${OUTPUT_FILE}" || true
}


# ------------------------------------------------------------
# 시스템 / 실험 조건 저장
# ------------------------------------------------------------

{
    echo "============================================================"
    echo "Cyclictest Verification"
    echo "============================================================"
    echo

    echo "CASE_NAME    : ${CASE_NAME}"
    echo "TIMESTAMP    : ${TIMESTAMP}"
    echo "CPU_COUNT    : ${CPU_COUNT}"
    echo "DURATION_SEC : ${DURATION}"
    echo "PRIORITY     : ${PRIORITY}"
    echo "INTERVAL_US  : ${INTERVAL_US}"
    echo "HIST_MAX_US  : ${HIST_MAX_US}"

    echo
    echo "IMPORTANT:"
    echo "CPU/network load is controlled externally by controller.sh."
    echo "This script does NOT start stress-ng or iperf3."

    echo
    echo "============================================================"
    echo "Kernel"
    echo "============================================================"

    uname -a

    echo
    echo "Kernel release:"
    uname -r

    echo
    echo "/sys/kernel/realtime:"

    if [[ -f /sys/kernel/realtime ]]; then
        cat /sys/kernel/realtime
    else
        echo "not present"
    fi

    echo
    echo "/proc/cmdline:"
    cat /proc/cmdline

    echo
    echo "============================================================"
    echo "CPU"
    echo "============================================================"

    lscpu

    echo
    echo "Online CPUs:"

    if [[ -f /sys/devices/system/cpu/online ]]; then
        cat /sys/devices/system/cpu/online
    else
        echo "not available"
    fi

    echo
    echo "============================================================"
    echo "CPU Governors"
    echo "============================================================"

    print_governors

    echo
    echo "IMPORTANT:"
    echo "Case 1 and Case 2 must use the same CPU governor."
    echo "This script does NOT change CPU governor settings."

    echo
    echo "============================================================"
    echo "Tool Version"
    echo "============================================================"

    cyclictest --help 2>&1 |
        grep -m1 "^cyclictest V " || echo "cyclictest version unavailable"

    echo
    echo "============================================================"
    echo "cyclictest command"
    echo "============================================================"

    printf "cyclictest"

    for arg in "${CYCLIC_ARGS[@]}"; do
        printf " %q" "${arg}"
    done

    printf "\n"

} > "${META_FILE}"


# ------------------------------------------------------------
# 시작 정보 출력
# ------------------------------------------------------------

echo
echo "============================================================"
echo " cyclictest verification"
echo "============================================================"
echo "Case       : ${CASE_NAME}"
echo "CPU count  : ${CPU_COUNT}"
echo "Duration   : ${DURATION} sec"
echo "Interval   : ${INTERVAL_US} us"
echo "Priority   : ${PRIORITY}"
echo "Result dir : ${OUTDIR}"
echo "============================================================"

echo
echo "[IMPORTANT]"
echo "CPU / network 부하는 별도로 실행 중인 controller.sh가 담당합니다."
echo "이 스크립트는 추가 부하를 생성하지 않습니다."
echo "Case 1 / Case 2 최종 비교 시 CPU governor를 동일하게 맞춰야 합니다."
echo
echo "Current governors:"
print_governors
echo


# ============================================================
# cyclictest 측정
# ============================================================

echo "[START] cyclictest started..."

cyclictest \
    "${CYCLIC_ARGS[@]}" \
    > "${RESULT_FILE}" 2>&1

extract_summary \
    "${RESULT_FILE}" \
    "${SUMMARY_FILE}"

echo "[DONE] cyclictest finished."


# ============================================================
# 결과 출력
# ============================================================

echo
echo "============================================================"
echo " Result"
echo "============================================================"
echo

if [[ -s "${SUMMARY_FILE}" ]]; then
    cat "${SUMMARY_FILE}"
else
    echo "Summary pattern not found."
    echo "Check:"
    echo "  ${RESULT_FILE}"
fi


echo
echo "============================================================"
echo " Saved files"
echo "============================================================"

echo "${META_FILE}"
echo "${RESULT_FILE}"
echo "${SUMMARY_FILE}"

echo "============================================================"


# ------------------------------------------------------------
# sudo로 실행했을 경우 결과 파일 소유자를
# 원래 사용자로 돌려놓는다.
# ------------------------------------------------------------

if [[ -n "${SUDO_USER:-}" ]] &&
   [[ "${SUDO_USER}" != "root" ]]; then

    USER_GROUP="$(id -gn "${SUDO_USER}")"

    chown -R \
        "${SUDO_USER}:${USER_GROUP}" \
        "${OUTDIR}"
fi


echo
echo "[DONE] cyclictest verification completed."
