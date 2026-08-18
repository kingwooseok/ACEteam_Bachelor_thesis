# Deadline-Aware Dynamic Packet Classification and Scheduling using TC-eBPF on PREEMPT_RT Linux

Raspberry Pi 5 기반 Linux에서 **PREEMPT_RT 기반 CPU 실시간성**과 **TC-eBPF + MQPRIO + ETF 기반 네트워크 스케줄링**을 결합하여, 혼잡한 네트워크 환경에서 Deadline-aware 제어 트래픽의 지연 및 Deadline Miss를 줄이는 것을 목표로 하는 졸업논문 프로젝트이다.

> 연구 제안서의 핵심 구조와 실험 설계를 바탕으로, 실제 커널 빌드와 런타임 초기화에 필요한 설정까지 하나의 흐름으로 정리한 문서이다.

---

## 1. 전체 구조

본 프로젝트는 크게 **CPU/커널 계층**, **네트워크 스케줄링 계층**, **Deadline 정책 계층**으로 구성된다.

```text
                         User Space
                              │
                 ┌────────────▼────────────┐
                 │     User Controller     │
                 │  Runtime Deadline Rule  │
                 └────────────┬────────────┘
                              │
                         BPF Map
                              │
                              ▼
Application ──► IP Layer ──► TC-eBPF
                              │
                    Deadline-aware Classification
                              │
                     skb->priority / mark
                              │
                              ▼
                           MQPRIO
                              │
                    Traffic Class / TX Queue
                              │
                              ▼
                             ETF
                              │
                         TxTime Scheduling
                              │
                              ▼
                             NIC
```

동시에 CPU 측에서는 PREEMPT_RT를 사용하여 RT task, IRQ, CPU isolation 등을 제어한다.

```text
CPU 0~2                         CPU 3
─────────────────────           ───────────────────
Housekeeping                    RT CPU
├─ 일반 Task                    └─ RT Control Task
├─ IRQ
├─ RCU callback
├─ kworker / workqueue
└─ Background Service
```

따라서 최종적으로는 다음 두 계층을 결합한다.

```text
CPU Layer
PREEMPT_RT
  ├─ CPU Isolation
  ├─ CPU Affinity
  ├─ IRQ Affinity
  ├─ IRQ Thread Priority
  └─ Workqueue Affinity

Network Layer
TC-eBPF
  ├─ BPF Map
  ├─ Deadline-aware Classification
  ├─ MQPRIO
  └─ ETF
```

연구의 핵심은 이 두 계층을 함께 적용했을 때 혼잡 상황에서 실시간 네트워크 결정성이 얼마나 개선되는지를 정량적으로 분석하는 것이다.

---

## 2. Deadline-aware Packet Scheduling

기존 TC 분류처럼 IP, Port, DSCP 등의 정적 규칙만 사용하는 대신, User Space Controller가 Flow별 실시간 요구사항을 **BPF Map**에 저장한다.

```text
User Controller
      │
      │ Flow / Deadline / Priority
      ▼
   BPF Map
      │
      ▼
   TC-eBPF
      │
      ├─ Flow lookup
      ├─ Deadline lookup
      ├─ Remaining deadline 계산
      └─ skb->priority / skb->mark 설정
```

예를 들어 패킷 생성 시각을 `t0`, 현재 시각을 `now`, 상대 Deadline을 `D`라고 하면:

```text
remaining_deadline = D - (now - t0)
```

TC-eBPF는 BPF Map의 정책을 조회하여 패킷의 긴급도에 따라 우선순위를 동적으로 결정한다.

패킷 자체에 Deadline 정보를 삽입하는 In-band 방식이 아니라 **BPF Map을 이용한 Out-of-band 정책 관리**를 사용하므로, 애플리케이션 패킷 포맷을 변경하지 않고 런타임에 정책을 변경할 수 있다.

---

## 3. MQPRIO + ETF

TC-eBPF가 설정한 `skb->priority`는 MQPRIO를 통해 Traffic Class와 NIC TX Queue로 연결된다.

```text
TC-eBPF
   │
   │ skb->priority
   ▼
 MQPRIO
   │
   ├─ Traffic Class 0 → TX Queue 0
   ├─ Traffic Class 1 → TX Queue 1
   └─ ...
```

그 다음 ETF는 패킷의 TxTime을 기준으로 실제 송신 시점을 제어한다.

```text
Deadline
   │
   ▼
TxTime 계산
   │
   ▼
Packet TxTime
   │
   ▼
ETF
   │
   ▼
NIC TX
```

따라서 **eBPF의 priority 분류와 ETF의 TxTime scheduling은 서로 다른 역할**을 한다.

```text
eBPF
→ "이 패킷을 어느 Traffic Class로 보낼 것인가?"

MQPRIO
→ "어느 TX Queue로 연결할 것인가?"

ETF
→ "지정된 TxTime을 기준으로 언제 보낼 것인가?"
```

---

## 4. Kernel Configuration

### 4.1 CPU Isolation / Real-Time

다음 옵션을 기본 RT 구성으로 사용한다.

```text
CONFIG_PREEMPT_RT=y
CONFIG_HIGH_RES_TIMERS=y
CONFIG_HZ_1000=y
CONFIG_NO_HZ_FULL=y
CONFIG_RCU_NOCB_CPU=y
CONFIG_CPUSETS=y
```

| Option | Purpose |
|---|---|
| `CONFIG_PREEMPT_RT` | PREEMPT_RT 실시간 커널 기능 |
| `CONFIG_HIGH_RES_TIMERS` | High Resolution Timer |
| `CONFIG_HZ_1000` | 1000 Hz timer tick |
| `CONFIG_NO_HZ_FULL` | RT 전용 CPU의 periodic tick 최소화 |
| `CONFIG_RCU_NOCB_CPU` | RCU callback offload |
| `CONFIG_CPUSETS` | CPU 집합 및 task 배치 제어 |

`CONFIG_HZ_1000`은 1 ms 주기의 timer tick을 의미하며, 실제 고해상도 timer 지원은 `CONFIG_HIGH_RES_TIMERS`가 담당한다.

### 4.2 CPU Frequency / Idle / Memory

다음 기능은 **Kconfig에서 제거하는 것이 아니라 커널에 포함한 뒤 runtime에서 정책을 제어**한다.

```text
CONFIG_CPU_FREQ=y
CONFIG_CPU_IDLE=y
CONFIG_TRANSPARENT_HUGEPAGE=y
```

즉,

```text
CONFIG_CPU_FREQ
→ performance governor

CONFIG_CPU_IDLE
→ RT CPU idle state 정책

CONFIG_TRANSPARENT_HUGEPAGE
→ 필요 시 runtime에서 never
```

로 관리한다.

THP는 PREEMPT_RT의 필수 비활성화 항목이 아니라, 메모리 관리에 의한 latency variation을 통제하고 싶을 때 사용하는 실험 조건이다.

### 4.3 eBPF / TC

```text
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
CONFIG_DEBUG_INFO_BTF=y

CONFIG_NET_CLS_BPF=y
CONFIG_NET_CLS_ACT=y

CONFIG_NET_SCH_MQPRIO=y
CONFIG_NET_SCH_ETF=y
```

| Option | Purpose |
|---|---|
| `CONFIG_BPF` | BPF subsystem |
| `CONFIG_BPF_SYSCALL` | Userspace BPF loading |
| `CONFIG_BPF_JIT` | BPF JIT |
| `CONFIG_DEBUG_INFO_BTF` | BTF / CO-RE 지원 |
| `CONFIG_NET_CLS_BPF` | TC-BPF classifier |
| `CONFIG_NET_CLS_ACT` | TC action framework |
| `CONFIG_NET_SCH_MQPRIO` | MQPRIO qdisc |
| `CONFIG_NET_SCH_ETF` | ETF qdisc |

### 4.4 PTP / Timestamping

```text
CONFIG_PTP_1588_CLOCK=y
CONFIG_NETWORK_PHY_TIMESTAMPING=y
```

이 옵션은 NIC의 PTP Hardware Clock(PHC) 및 hardware timestamping을 사용하는 데 필요하다.

단, 실제 hardware timestamping 지원 여부는 NIC와 해당 driver에 의해 결정되므로 커널 설정만으로 보장되지는 않는다.

---

## 5. Boot-time RT Configuration

예를 들어 CPU 3을 RT 전용 CPU로 사용한다면 kernel command line에 다음을 적용한다.

```text
isolcpus=3
nohz_full=3
rcu_nocbs=3
```

그 결과 목표하는 CPU 배치는 다음과 같다.

```text
CPU 0~2
├─ 일반 userspace task
├─ IRQ
├─ RCU callback
├─ kworker / housekeeping work
└─ Background service

CPU 3
└─ RT control task
```

부팅 후 isolation 상태는 다음으로 확인한다.

```bash
cat /sys/devices/system/cpu/isolated
```

---

## 6. Runtime Initialization

커널 부팅 이후에는 다음 순서로 실험 환경을 구성한다.

```text
┌──────────────────────────────┐
│ 1. CPU / RT Environment      │
│    ├─ CPU frequency          │
│    ├─ CPU idle policy        │
│    └─ Background services    │
├──────────────────────────────┤
│ 2. CPU Isolation             │
│    ├─ RT task affinity       │
│    ├─ IRQ affinity           │
│    ├─ IRQ priority           │
│    └─ Workqueue affinity     │
├──────────────────────────────┤
│ 3. Clock Synchronization     │
│    ├─ NIC PHC 확인           │
│    ├─ ptp4l                   │
│    └─ phc2sys                 │
├──────────────────────────────┤
│ 4. Network Scheduler         │
│    ├─ MQPRIO                  │
│    └─ ETF                     │
├──────────────────────────────┤
│ 5. eBPF                      │
│    ├─ BPF load               │
│    ├─ TC attach               │
│    └─ BPF Map initialization  │
├──────────────────────────────┤
│ 6. Experiment                │
│    ├─ Control traffic         │
│    ├─ Background traffic      │
│    └─ Measurement             │
└──────────────────────────────┘
```

### CPU Frequency

모든 실험에서 CPU frequency variation을 통제한다.

```bash
cpupower frequency-info
sudo cpupower frequency-set -g performance
```

### CPU Idle

RT CPU에서 제공되는 idle state를 확인하고 필요에 따라 제한한다.

```bash
cat /sys/devices/system/cpu/cpu*/cpuidle/state*/disable
```

모든 idle state를 무조건 비활성화하는 것을 기본 조건으로 하지 않고, 실제 Raspberry Pi 5의 idle state를 확인한 뒤 실험 조건을 결정한다.

### RT Task

RT control task는 전용 CPU와 `SCHED_FIFO`로 고정한다.

```bash
taskset -c 3 ./control_app
```

또는:

```bash
chrt -f 90 taskset -c 3 ./control_app
```

실제 priority는 실험에서 사용할 값으로 고정하고 기록한다.

### IRQ Affinity

IRQ가 RT CPU를 침범하지 않도록 확인 및 조정한다.

```bash
cat /proc/interrupts
cat /proc/irq/<IRQ>/smp_affinity_list
```

예:

```bash
echo 0-2 | sudo tee /proc/irq/<IRQ>/smp_affinity_list
```

PREEMPT_RT에서 threaded IRQ가 사용되는 경우 IRQ thread도 확인한다.

```bash
ps -eLo pid,tid,cls,rtprio,pri,psr,comm | grep -E 'irq/|thread'
```

필요한 경우:

```bash
sudo chrt -f <PRIORITY> -p <IRQ_THREAD_PID>
```

IRQ priority는 RT application보다 무조건 높게 설정하지 않고, 실험 목적에 맞는 정책으로 고정한다.

### Workqueue

Linux kernel의 deferred work는 workqueue를 통해 `kworker`에서 실행될 수 있다. 따라서 CPU 3을 RT 전용으로 사용할 경우 unbound workqueue가 RT CPU를 사용하는지 확인한다.

```bash
cat /sys/devices/virtual/workqueue/unbound_cpumask
```

workqueue 구조 확인:

```bash
sudo tools/workqueue/wq_dump.py
```

실제 worker 확인:

```bash
ps -eLo pid,tid,psr,comm | grep kworker
```

필요한 경우 unbound workqueue의 CPU mask를 housekeeping CPU로 제한한다.

```bash
echo 0-2 | sudo tee /sys/devices/virtual/workqueue/unbound_cpumask
```

모든 workqueue를 무조건 제거하는 것이 아니라, 실제 kernel/workqueue 구성을 확인한 뒤 RT CPU isolation을 유지하는 방향으로 설정한다.

### Background Service

불필요한 userspace service가 RT CPU에서 실행되지 않도록 한다.

```bash
ps -eLo pid,psr,comm
```

모든 실험 Case에서 동일한 background service 조건을 유지한다.

### THP

상태 확인:

```bash
cat /sys/kernel/mm/transparent_hugepage/enabled
```

실험에서 THP의 영향을 제거하기로 결정한 경우:

```bash
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
```

THP는 RT 필수 설정이 아니라 실험 조건을 통제하기 위한 선택 항목이다.

---

## 7. PTP Synchronization

Deadline 및 TxTime 계산, 그리고 송수신 timestamp 비교를 위해 PTP clock을 구성한다.

전체 구조는 다음과 같다.

```text
PTP Grandmaster
      │
      │ IEEE 1588
      ▼
   NIC PHC
  /dev/ptpX
      │
      │ phc2sys
      ▼
CLOCK_REALTIME
      │
      ▼
Deadline / TxTime / Timestamp
```

NIC capability 확인:

```bash
ethtool -T eth0
ls -l /dev/ptp*
```

`ptp4l`은 NIC의 PHC를 PTP 기준 시간에 동기화한다.

```bash
sudo ptp4l -i eth0 -m -H
```

그 다음 `phc2sys`를 이용하여 PHC와 system clock을 동기화한다.

```bash
sudo phc2sys -s <PHC> -c CLOCK_REALTIME -m
```

실제 PHC device는 시스템에서 확인한 `/dev/ptpX` 및 linuxptp 환경에 맞춰 지정한다.

PTP는 ETF를 대신하는 기능이 아니다.

```text
PTP
→ Clock 기준 동기화

eBPF
→ Deadline-aware classification

MQPRIO
→ Traffic Class / TX Queue mapping

ETF
→ TxTime scheduling
```

---

## 8. Network Runtime Setup

커널 config만 활성화한다고 MQPRIO와 ETF가 자동으로 구성되는 것은 아니다.

실험 시작 시 실제 qdisc를 설정한다.

```text
TC-eBPF
   │
   ▼
MQPRIO
   │
   ▼
ETF
   │
   ▼
NIC
```

MQPRIO는 `skb->priority`와 NIC Traffic Class / TX Queue의 관계를 구성하고, ETF는 TxTime을 기준으로 송신 시점을 제어한다.

따라서 runtime 초기화에서는:

```text
1. MQPRIO qdisc 구성
2. ETF qdisc 구성
3. TC-eBPF load
4. TC hook attach
5. BPF Map 초기화
6. User Controller 실행
```

순으로 구성한다.

---

## 9. eBPF Runtime Setup

eBPF 프로그램은 일반적인 kernel module처럼 `insmod`로 올리는 것이 아니다.

```text
deadline_classifier.bpf.o
          │
          ▼
        libbpf
          │
          ▼
       BPF Load
          │
          ▼
       TC Attach
```

TC-eBPF는 송신 경로에서 Flow 정보를 확인하고 BPF Map을 조회하여 deadline-aware policy를 적용한다.

```text
Application
    │
    ▼
 IP Layer
    │
    ▼
 TC-eBPF
    │
    ├─ Flow lookup
    ├─ BPF Map lookup
    ├─ Deadline evaluation
    └─ priority / mark
    │
    ▼
 MQPRIO
    │
    ▼
 ETF
    │
    ▼
 NIC
```

User Space Controller는 BPF Map을 통해 runtime policy를 갱신한다.

```text
User Controller
      │
      ▼
   BPF Map
      │
      ▼
  TC-eBPF
```

---

## 10. Experimental Design

실험 플랫폼은 Raspberry Pi 5 + Gigabit Ethernet 기반으로 구성한다.

Control traffic은 UDP 기반 주기성 traffic으로 구성하며, 예시 조건은 다음과 같다.

```text
Control Traffic : Periodic UDP
Period          : 1 ms
Deadline        : 1 ms 또는 control cycle 기반 relative deadline
Background Load : 0%, 50%, 70%, 80%, 90%
```

Best-Effort background traffic은 `iperf3` 또는 Linux `pktgen` 등을 이용하여 생성한다.

모든 Case에서 동일한 traffic generation 조건과 measurement 조건을 유지하여 각 구성 요소의 효과를 비교한다.

---

## 11. Four Experimental Cases

| Case | Kernel | TC-eBPF | MQPRIO | ETF | Purpose |
|---|---|---|---|---|---|
| Case 1 | Vanilla Linux | - | - | - | Baseline |
| Case 2 | PREEMPT_RT | - | - | - | CPU RT 효과 |
| Case 3 | Vanilla Linux | O | O | O | Network scheduling 효과 |
| Case 4 | PREEMPT_RT | O | O | O | CPU + Network 통합 효과 |

### Case 1 — Vanilla

```text
Non-RT Linux
+ 기본 network stack
```

전체 실험의 baseline으로 사용한다.

### Case 2 — RT-Only

```text
PREEMPT_RT
+ SCHED_FIFO
+ CPU Isolation
+ CPU Affinity
+ IRQ Affinity
+ IRQ Thread Priority
```

CPU scheduling의 실시간성 효과를 독립적으로 확인한다.

### Case 3 — TC-eBPF Only

```text
Non-RT Linux
+ TC-eBPF
+ BPF Map
+ MQPRIO
+ ETF
```

네트워크 계층의 동적 classification 및 scheduling 효과를 독립적으로 확인한다.

### Case 4 — RT + TC-eBPF

```text
PREEMPT_RT
+ CPU RT tuning
+ TC-eBPF
+ BPF Map
+ MQPRIO
+ ETF
```

CPU와 network의 실시간성 제어를 통합한 최종 구성이다.

---

## 12. Common Experimental Controls

실험 결과에 영향을 주는 외부 변수를 최소화하기 위해 다음 조건을 고정한다.

### Case 2 / Case 4

```text
SCHED_FIFO
CPU Isolation
CPU Affinity
IRQ Affinity
IRQ Thread Priority
Workqueue Affinity
```

### All Cases

```text
CPU Frequency Policy
Background Service
Hardware
Traffic Pattern
Packet Size
Test Duration
Measurement Method
```

특히 제안서의 비교 실험에서는 CPU Frequency Scaling 및 불필요한 Background Service를 모든 Case에서 동일하게 통제한다.

---

## 13. Measurement

주요 평가 지표는 다음과 같다.

### Average Latency

평균 패킷 전송 지연을 측정한다.

### Tail Latency

```text
99%
99.9%
```

구간의 latency를 측정하여 지연 spike 및 tail behavior를 비교한다.

### Worst-Case Latency

측정된 최대 latency를 비교한다.

### Deadline Miss Ratio

설정된 Deadline을 초과한 packet의 비율을 측정한다.

```text
Deadline Miss Ratio
=
Deadline을 초과한 packet 수
──────────────────────
전체 packet 수
```

특히 background load가 증가할수록 Deadline Miss Ratio와 Tail Latency가 어떻게 변화하는지를 중점적으로 분석한다.

---

## 14. Expected Comparison

연구에서 확인하고자 하는 비교 관계는 다음과 같다.

```text
Case 1 → Case 2
```

PREEMPT_RT 적용으로 CPU scheduling 및 IRQ response 측면의 latency variation 감소 여부를 확인한다.

```text
Case 1 → Case 3
```

TC-eBPF + MQPRIO + ETF 적용으로 network congestion 상황에서 긴급 traffic의 scheduling이 개선되는지 확인한다.

```text
Case 3 → Case 4
```

PREEMPT_RT를 추가하여 CPU scheduling latency까지 함께 제어했을 때 network-only 구성보다 추가적인 개선이 발생하는지 확인한다.

최종적으로 다음과 같은 관계를 검증하는 것이 목표다.

```text
                CPU Layer             Network Layer

Case 1             -                       -

Case 2           RT                      -

Case 3             -                 eBPF + MQPRIO + ETF

Case 4           RT                  eBPF + MQPRIO + ETF
```

---

## 15. Project Workflow

전체 구현 및 실험 흐름은 다음과 같다.

```text
                    ┌──────────────────┐
                    │ Linux 6.12.x     │
                    │ + PREEMPT_RT     │
                    └────────┬─────────┘
                             │
                       Kernel Build
                             │
                             ▼
                    ┌──────────────────┐
                    │ Boot-time RT     │
                    │ Isolation        │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
          CPU Policy      IRQ / WQ       PTP / PHC
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                    ┌──────────────────┐
                    │ Network Setup    │
                    │ MQPRIO + ETF     │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ TC-eBPF          │
                    │ + BPF Map        │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Control Traffic  │
                    │ + Background     │
                    │ Traffic          │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Measurement      │
                    │ Latency / DMR    │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │ Case 1 ~ Case 4  │
                    │ Comparison       │
                    └──────────────────┘
```

---
