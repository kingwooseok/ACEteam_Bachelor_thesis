# 졸업논문 성능 실험 및 분석 도구

이 디렉터리는 졸업논문 성능 실험에서 사용하는 **실험 실행 자동화**, **실험 환경 검증**, **이상 결과 원인 분석** 도구를 정리한다.

핵심 자동화 스크립트는 `controller.sh`이며, 수신기에서 다음 작업을 한 번에 수행한다.

- `iperf3` 서버 실행
- `mpstat` 기반 CPU 사용률 측정
- `sar` 기반 `eth0` 네트워크 사용량 측정
- `stress-ng` 기반 지정 CPU 부하 생성
- 실험 설정 및 로그 자동 저장
- 실험 종료 또는 `Ctrl+C` 입력 시 실행한 프로세스 정리

`controller.sh`는 시스템 부하와 상태를 관리·관찰하기 위한 스크립트이며, **애플리케이션의 OWD/latency 자체를 측정하는 도구는 아니다.** 핵심 latency 결과와 함께 시스템 로그를 비교하여 Case별 성능 차이와 이상 결과의 원인을 분석하는 용도로 사용한다.

---

## 1. 실험 Case

현재 실험은 다음 Case를 기준으로 비교한다.

| Case | 구성 |
|---|---|
| Case 1 | Vanilla |
| Case 2 | PREEMPT_RT + IRQ affinity |
| Case 3 | PREEMPT_RT + XDP + CPUMAP |
| Case 4 | AF_XDP |

`controller.sh`는 특정 Case에 종속되지 않으며 각 Case에서 동일한 방식으로 기본 부하와 시스템 모니터링을 수행할 수 있다.

---

## 2. 디렉터리 구조

실험을 실행하면 `results/` 아래에 실행 시각을 기준으로 결과 디렉터리가 자동 생성된다.

```text
.
├── controller.sh
├── README.md
└── results/
    └── YYYYMMDD_HHMMSS/
        ├── config.txt
        ├── iperf3.log
        ├── mpstat.log
        ├── network.log
        └── stress_cpuN.log
```

CPU 부하를 여러 코어에 적용하면 `stress_cpu0.log`, `stress_cpu1.log`처럼 CPU별 로그가 각각 생성된다.

> `RESULT_ROOT`가 `./results`이므로 가급적 이 디렉터리에서 `controller.sh`를 실행한다. 다른 작업 디렉터리에서 실행하면 `results/`도 현재 작업 디렉터리를 기준으로 생성된다.

---

## 3. controller.sh

### 3.1 역할

`controller.sh`는 수신기에서 다음 순서로 실험 환경을 구성한다.

```text
controller.sh 실행
      ↓
인자 및 CPU 번호 검증
      ↓
필수 명령어 설치 여부 확인
      ↓
results/<timestamp>/ 생성
      ↓
config.txt 저장
      ↓
iperf3 server 시작
      ↓
mpstat 시작
      ↓
sar 시작
      ↓
stress-ng CPU 부하 시작
      ↓
지정 시간 동안 실험
      ↓
모든 프로세스 종료 및 로그 저장
```

### 3.2 실행 형식

```bash
./controller.sh <CPU_LOAD> <INTERVAL> <DURATION> <LOAD_CPUS>
```

예시:

```bash
./controller.sh 50 1 60 0,1,2
```

위 명령은 CPU0, CPU1, CPU2 각각에 목표 50% 부하를 주고, CPU/네트워크 상태를 1초 간격으로 60초 동안 측정한다.

### 3.3 인자

| 인자 | 의미 | 허용 값 / 형식 |
|---|---|---|
| `CPU_LOAD` | 선택 CPU 각각에 줄 목표 부하율 | `0~100` 정수 |
| `INTERVAL` | `mpstat`, `sar` 측정 주기 | 1 이상의 정수, 단위: 초 |
| `DURATION` | 전체 controller 실행 시간 | 1 이상의 정수, 단위: 초 |
| `LOAD_CPUS` | `stress-ng`를 실행할 CPU 목록 | 예: `0`, `0,1,2`, `0,1,2,3` |

현재 스크립트는 `INTERVAL`과 `DURATION`에 **정수 초만 허용**한다. 예를 들어 `0.001`과 같은 소수 단위 주기는 사용할 수 없다.

`CPU_LOAD=0`이면 `stress-ng`는 실행하지 않는다. 다만 스크립트 형식상 네 번째 인자 `LOAD_CPUS`는 여전히 필요하므로 baseline은 다음처럼 실행할 수 있다.

```bash
./controller.sh 0 1 60 0
```

이 경우 `LOAD_CPUS=0`은 형식 검증에만 사용되며 실제 CPU 부하는 생성되지 않는다.

### 3.4 실행 권한

최초 한 번 실행 권한을 부여한다.

```bash
chmod +x controller.sh
```

---

## 4. controller.sh에서 사용하는 도구

스크립트 시작 시 아래 명령어의 존재 여부를 확인하며, 하나라도 없으면 실험을 시작하지 않는다.

| 도구 | 용도 | 생성 로그 |
|---|---|---|
| `iperf3` | 수신기 네트워크 부하용 서버 | `iperf3.log` |
| `mpstat` | 전체 및 CPU별 사용률 측정 | `mpstat.log` |
| `sar` | `eth0` 송수신 트래픽 측정 | `network.log` |
| `stress-ng` | 지정 CPU에 목표 부하 생성 | `stress_cpuN.log` |
| `taskset` | `stress-ng`를 지정 CPU에 고정 | 별도 로그 없음 |

`mpstat`와 `sar`는 일반적으로 `sysstat` 패키지에 포함된다.

---

## 5. 결과 파일

### 5.1 config.txt

실행 인자를 그대로 기록한다.

```text
cpu_load=50
load_cpus=0,1,2
measurement_interval=1
duration=60
```

실험 결과를 비교할 때 먼저 이 파일로 해당 run의 조건을 확인한다.

### 5.2 iperf3.log

수신기에서 실행된 `iperf3` server 로그이다.

```bash
iperf3 -s -i 0
```

`-i 0`을 사용하므로 주기별 중간 출력은 끄고 **최종 결과 중심으로 기록**한다.

> `controller.sh` 자체는 송신 트래픽을 만들지 않는다. 송신기에서 별도로 `iperf3` client 또는 실제 실험 송신 애플리케이션을 실행해야 한다.

### 5.3 mpstat.log

```bash
mpstat -P ALL <INTERVAL>
```

전체 CPU와 각 CPU의 사용률을 기록한다. `stress-ng`가 의도한 CPU에 실제 부하를 만들었는지 확인하거나 Case별 CPU 사용 패턴을 비교할 때 사용한다.

### 5.4 network.log

```bash
sar -n DEV --iface=eth0 <INTERVAL>
```

`eth0`의 패킷 수와 송수신량을 기록한다. 설정한 송신 부하와 별개로 **수신기 인터페이스에서 실제로 관측된 트래픽**을 확인하는 용도이다.

### 5.5 stress_cpuN.log

각 CPU에서 실행한 `stress-ng --metrics-brief` 결과를 저장한다.

예를 들어 `LOAD_CPUS=0,1,2`이면 다음 파일이 생성된다.

```text
stress_cpu0.log
stress_cpu1.log
stress_cpu2.log
```

---

## 6. 로그 확인

최근 실행 결과 확인:

```bash
ls -lt results/
```

특정 run의 설정 확인:

```bash
cat results/<timestamp>/config.txt
```

로그 확인:

```bash
less results/<timestamp>/iperf3.log
less results/<timestamp>/mpstat.log
less results/<timestamp>/network.log
less results/<timestamp>/stress_cpu0.log
```

실험 중 실시간으로 확인하려면 다음처럼 사용할 수 있다.

```bash
tail -f results/<timestamp>/mpstat.log
tail -f results/<timestamp>/network.log
```

---

## 7. 실험 환경 검증

각 Case의 실험 환경이 의도한 대로 실제 적용되었는지 확인하기 위해 다음 검증을 수행한다. 이 단계는 이상 결과가 발생했을 때만 수행하는 선택적 분석이 아니라, **각 Case의 구성을 검증하기 위한 필수 절차**이다.

| 검증 대상 | 확인하려는 것 | 도구 / 방법 | 의미 |
|---|---|---|---|
| Case 1 ↔ Case 2 | PREEMPT_RT 적용 효과 | `cyclictest` | scheduling latency가 실제로 감소했는지 확인 |
| Case 2 | 실제 IRQ 처리 CPU | `mpstat -I CPU` | 실제 트래픽에서 어느 CPU가 IRQ를 처리했는지 확인 |
| Case 3 | XDP CPUMAP steering | `xdp_cpumap_enqueue`, `xdp_cpumap_kthread` tracepoint + `trace-cmd` | 패킷이 실제 목표 CPU로 redirect됐는지 확인 |

### Case 1 ↔ Case 2: cyclictest

목적은 Vanilla와 PREEMPT_RT 환경의 **scheduling latency 차이**를 확인하는 것이다.

논문의 네트워크 latency 결과와 별도로 PREEMPT_RT가 시스템 스케줄링 지연에 실제 영향을 주었는지 확인하는 보조 근거로 사용한다.

### Case 2: mpstat -I CPU

```bash
mpstat -I CPU <INTERVAL>
```

IRQ affinity를 설정한 뒤, 실제 실험 트래픽에서도 IRQ가 기대한 CPU에서 처리되는지 확인한다.

단순히 affinity 설정값만 확인하는 것보다 **실제 동작 결과를 함께 확인**하는 용도이다.

### Case 3: XDP CPUMAP tracepoint + trace-cmd

확인 대상:

```text
xdp_cpumap_enqueue
xdp_cpumap_kthread
```

CPUMAP redirect가 실제로 발생하고 목표 CPU 쪽 kthread에서 처리되는지 tracepoint를 통해 확인한다.

커널에 따라 사용 가능한 tracepoint가 다를 수 있으므로 실제 측정 전에 현재 커널에서 해당 이벤트가 존재하는지 확인한다.

```bash
trace-cmd list -e | grep xdp_cpumap
```

※ 이 항목은 CPUMAP 구현 담당 파트에서 별도로 검증한다.

---

## 8. 이상 결과 원인 분석

실험 결과가 예상과 다를 경우 처음부터 모든 tracing 도구를 켜지 않고, **증상에 따라 가벼운 도구부터 원인을 좁힌 뒤 필요한 경우 더 깊은 tracing으로 넘어간다.**

| 이상 증상 | 먼저 의심할 것 | 1차 분석 | 더 깊은 분석 |
|---|---|---|---|
| latency / tail만 튐 | scheduling, IRQ/softirq, OS noise | `perf sched`, `rtla osnoise` | `ftrace`, `trace-cmd` |
| 전체 latency가 예상보다 높음 | CPU processing overhead, scheduling, 잘못된 CPU 배치 | `perf stat`, `perf sched` | `ftrace` |
| Packet Loss 발생 | NIC/driver drop, kernel drop, RX 처리 부족 | `ethtool -S`, `ip -s link` | `dropwatch`, tracepoint |
| CPU 사용량이 이상함 / Case가 오히려 느림 | cycles/cache/context switch/migration 증가 | `perf stat` | `perf sched`, `perf record` |
| 결과가 들쭉날쭉하고 재현되지 않음 | PTP, OS noise, background task/IRQ | PTP log + `rtla osnoise` | `ftrace` |

### Packet Loss 1차 확인 예시

NIC/driver 통계:

```bash
sudo ethtool -S eth0
```

인터페이스 통계:

```bash
ip -s link show eth0
```

Packet Loss가 애플리케이션 성능 한계 때문인지, NIC/driver/kernel 단계에서 이미 drop된 것인지 구분할 때 사용한다.

---

## 9. 권장 실험 순서

```text
1. 실험 Case 구성
   ↓
2. 실험 환경 검증
   - Case 1 ↔ Case 2: cyclictest
   - Case 2: mpstat -I CPU
   - Case 3: XDP CPUMAP tracepoint + trace-cmd
   ↓
3. controller.sh 실행
   ↓
4. 송신기 트래픽 / 실험 애플리케이션 실행
   ↓
5. latency, loss, goodput 등 핵심 결과 수집
   ↓
6. controller 결과와 함께 CPU / network 상태 확인
   ↓
7. 결과가 이상하면 '이상 결과 원인 분석' 표에 따라 추가 분석
```

**실험 환경 검증은 각 Case가 의도한 설정대로 실제 동작하는지 확인하기 위해 반드시 수행한다.** 반면 `perf`, `ftrace`, `rtla`, `dropwatch` 등 이상 결과 원인 분석용 도구는 기본적으로 항상 실행하지 않고, 결과에 이상이 있을 때 필요한 도구를 선택적으로 추가한다. 이렇게 하면 상세 tracing에 따른 observer effect를 줄일 수 있다.

---

## 10. 실험 시 주의사항

### controller와 송신기는 자동 동기화되지 않음

`controller.sh`는 `iperf3` client의 접속을 기다린 뒤 타이머를 시작하지 않는다. 모니터링과 CPU 부하를 시작한 뒤 바로 `DURATION` 카운트가 진행된다.

따라서 송신기 시작이 늦으면 실제 트래픽이 흐른 시간과 controller의 측정 시간이 달라질 수 있다. 반복 실험에서는 **송신기와 수신기의 시작 순서를 동일하게 유지하고 실험 구간을 맞추는 것**이 중요하다.

### network.log는 eth0만 측정

현재 controller의 `sar` 명령은 다음과 같이 고정되어 있다.

```bash
--iface=eth0
```

실험 인터페이스가 변경되면 `controller.sh`도 함께 수정해야 한다.

### Ctrl+C로 종료 가능

실험 도중 `Ctrl+C` 또는 종료 신호를 받으면 cleanup 로직이 실행되어 controller가 시작한 `stress-ng`, `mpstat`, `sar`, `iperf3` 프로세스를 종료하고 결과 경로를 출력한다.

### 추가 분석 도구는 필요할 때만 사용

`controller.sh`에 포함되지 않은 도구는 목적에 따라 별도로 실행한다. `cyclictest`, `mpstat -I CPU`, XDP CPUMAP tracepoint + `trace-cmd`는 **Case 구성을 확인하기 위한 필수 실험 환경 검증**에 사용하고, `perf`, `rtla`, `ethtool`, `dropwatch`, `ftrace` 등은 **이상 결과의 원인을 분석할 때 선택적으로 사용**한다.

---

## 11. 요약

이 디렉터리의 도구는 **필수 실험 절차와 필요 시 수행하는 추가 분석 절차**로 구분한다.

```text
[기본 실험]
controller.sh
    └─ CPU load + CPU/network monitoring + iperf3 server

[환경 검증 - 필수]
cyclictest / mpstat -I CPU / XDP CPUMAP tracepoint
    └─ 각 Case가 실제로 의도한 구성대로 동작하는지 확인

[이상 결과 분석 - 필요 시]
perf / rtla / ethtool / ip / dropwatch / ftrace / trace-cmd
    └─ latency, loss, CPU 이상, 재현성 문제의 원인 추적
```

각 Case에서는 동일한 controller 조건을 유지하면서 실험 환경 검증을 반드시 수행한다. 이후 상세 원인 분석 도구는 이상 결과가 발생한 경우에만 추가하여 실험의 재현성과 observer effect를 함께 관리한다.
