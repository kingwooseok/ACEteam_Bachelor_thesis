#!/bin/bash

# ============================================================
# Receiver Experiment Controller
#
# Usage:
#   ./controller.sh <CPU_LOAD> <INTERVAL> <DURATION> <LOAD_CPUS>
#
# Example:
#   ./controller.sh 50 1 60 0,1,2
#
# 의미:
#   CPU_LOAD : 선택한 각 CPU에 줄 부하율 (%)
#   INTERVAL : 측정 주기 (초)
#   DURATION : 전체 실험 시간 (초)
#   LOAD_CPUS: stress-ng 부하를 줄 CPU 목록
#
# Example:
#   0       -> CPU0
#   0,1,2   -> CPU0, CPU1, CPU2 각각 부하
#   0,1,2,3 -> CPU0~CPU3 전체 부하
# ============================================================

set -u

RESULT_ROOT="./results"

RESULT_DIR=""

IPERF_PID=""
MPSTAT_PID=""
SAR_PID=""
STRESS_PIDS=()

STOPPING=0


# ============================================================
# Cleanup
# ============================================================

cleanup()
{
    if [ "$STOPPING" -eq 1 ]; then
        return
    fi

    STOPPING=1

    echo
    echo "[STOP] Finishing experiment..."

    # stress-ng 종료
    for pid in "${STRESS_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
        fi
    done

    # mpstat 종료
    if [ -n "$MPSTAT_PID" ] &&
       kill -0 "$MPSTAT_PID" 2>/dev/null; then

        kill "$MPSTAT_PID" 2>/dev/null
    fi

    # sar 종료
    if [ -n "$SAR_PID" ] &&
       kill -0 "$SAR_PID" 2>/dev/null; then

        kill "$SAR_PID" 2>/dev/null
    fi

    # iperf3 종료
    if [ -n "$IPERF_PID" ] &&
       kill -0 "$IPERF_PID" 2>/dev/null; then

        kill "$IPERF_PID" 2>/dev/null
    fi


    # 프로세스 종료 대기
    for pid in "${STRESS_PIDS[@]}"; do
        wait "$pid" 2>/dev/null
    done

    [ -n "$MPSTAT_PID" ] && wait "$MPSTAT_PID" 2>/dev/null
    [ -n "$SAR_PID" ]    && wait "$SAR_PID" 2>/dev/null
    [ -n "$IPERF_PID" ]  && wait "$IPERF_PID" 2>/dev/null


    if [ -n "$RESULT_DIR" ]; then
        echo "[DONE] Results saved: $RESULT_DIR"
    fi
}


trap cleanup EXIT
trap 'exit 130' INT TERM


# ============================================================
# Arguments
# ============================================================

if [ "$#" -ne 4 ]; then

    echo "Usage:"
    echo "  $0 <CPU_LOAD> <INTERVAL> <DURATION> <LOAD_CPUS>"
    echo
    echo "Example:"
    echo "  $0 50 1 60 0,1,2"

    exit 1
fi


CPU_LOAD="$1"
INTERVAL="$2"
DURATION="$3"
LOAD_CPUS="$4"


# ============================================================
# Validate arguments
# ============================================================

# CPU load: 0 ~ 100
if ! [[ "$CPU_LOAD" =~ ^[0-9]+$ ]] ||
   [ "$CPU_LOAD" -lt 0 ] ||
   [ "$CPU_LOAD" -gt 100 ]; then

    echo "Error: CPU_LOAD must be 0~100."
    exit 1
fi


# Measurement interval
if ! [[ "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then

    echo "Error: INTERVAL must be a positive integer."
    exit 1
fi


# Duration
if ! [[ "$DURATION" =~ ^[1-9][0-9]*$ ]]; then

    echo "Error: DURATION must be a positive integer."
    exit 1
fi


# CPU list 형식 확인
if ! [[ "$LOAD_CPUS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then

    echo "Error: LOAD_CPUS format is invalid."
    echo "Example: 0,1,2"

    exit 1
fi


# ============================================================
# Check required commands
# ============================================================

for cmd in stress-ng mpstat sar iperf3 taskset; do

    if ! command -v "$cmd" >/dev/null 2>&1; then

        echo "Error: '$cmd' is not installed."
        exit 1
    fi

done


# ============================================================
# Validate CPU numbers
# ============================================================

CPU_COUNT=$(nproc --all)

IFS=',' read -ra CPU_ARRAY <<< "$LOAD_CPUS"

for cpu in "${CPU_ARRAY[@]}"; do

    if [ "$cpu" -ge "$CPU_COUNT" ]; then

        echo "Error: CPU${cpu} does not exist."
        echo "Available CPUs: 0 ~ $((CPU_COUNT - 1))"

        exit 1
    fi

done


# ============================================================
# Result directory
# ============================================================

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

RESULT_DIR="${RESULT_ROOT}/${TIMESTAMP}"

mkdir -p "$RESULT_DIR"


# ============================================================
# Save configuration
# ============================================================

cat > "${RESULT_DIR}/config.txt" <<EOF
cpu_load=${CPU_LOAD}
load_cpus=${LOAD_CPUS}
measurement_interval=${INTERVAL}
duration=${DURATION}
EOF


# ============================================================
# Experiment information
# ============================================================

echo "========================================"
echo " Receiver Experiment Controller"
echo "========================================"
echo "CPU load      : ${CPU_LOAD}%"
echo "Load CPUs     : ${LOAD_CPUS}"
echo "Interval      : ${INTERVAL} sec"
echo "Duration      : ${DURATION} sec"
echo "Result        : ${RESULT_DIR}"
echo "========================================"


# ============================================================
# 1. Start iperf3 server (주기별 중간 출력은 끄고 최종 결과만 기록)
# ============================================================

iperf3 \
    -s \
    -i 0 \
    > "${RESULT_DIR}/iperf3.log" 2>&1 &

IPERF_PID=$!


# ============================================================
# 2. Start CPU monitoring
# ============================================================

stdbuf -oL \
    mpstat \
    -P ALL \
    "$INTERVAL" \
    > "${RESULT_DIR}/mpstat.log" 2>&1 &

MPSTAT_PID=$!


# ============================================================
# 3. Start network monitoring (eth0만 측정)
# ============================================================

stdbuf -oL \
    sar \
    -n DEV \
    --iface=eth0 \
    "$INTERVAL" \
    > "${RESULT_DIR}/network.log" 2>&1 &

SAR_PID=$!


# ============================================================
# 4. Start CPU load
# ============================================================

if [ "$CPU_LOAD" -gt 0 ]; then

    for cpu in "${CPU_ARRAY[@]}"; do

        echo "[CPU LOAD] CPU${cpu} -> ${CPU_LOAD}%"

        taskset -c "$cpu" \
            stress-ng \
            --cpu 1 \
            --cpu-load "$CPU_LOAD" \
            --timeout "${DURATION}s" \
            --metrics-brief \
            > "${RESULT_DIR}/stress_cpu${cpu}.log" 2>&1 &

        STRESS_PIDS+=("$!")

    done

else

    echo "[CPU LOAD] 0% - stress-ng skipped."

fi


# ============================================================
# Run experiment
# ============================================================

echo
echo "[RUNNING] Experiment started"
echo

sleep "$DURATION"


# EXIT trap -> cleanup()
exit 0
