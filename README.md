````markdown
# Deadline-Aware Dynamic Packet Classification and Scheduling using TC-eBPF on PREEMPT_RT Linux

PREEMPT_RT Linux 환경에서 TC-eBPF를 이용한 Deadline-Aware 동적 패킷 분류 및 네트워크 스케줄링 실험 프로젝트.

본 프로젝트는 Raspberry Pi 5 기반 Linux 환경에서 **CPU 실시간성(PREEMPT_RT)** 과 **네트워크 실시간성(TC-eBPF + MQPRIO + ETF)** 을 결합하여, 혼잡 상황에서 제어 트래픽의 Tail Latency 및 Deadline Miss Ratio를 줄이는 것을 목표로 한다.

---

## 1. System Architecture

전체 시스템은 다음과 같은 구조를 사용한다.

```text
                    User Space
                        │
                ┌───────▼────────┐
                │ User Controller │
                └───────┬────────┘
                        │
                    BPF Map
                        │
                        ▼
Application ──► IP Layer ──► TC-eBPF
                              │
                              │ skb priority / mark
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
````

### Deadline Policy

애플리케이션 패킷 자체에 Deadline 정보를 추가하는 In-band 방식 대신,
User Space Controller와 eBPF 프로그램 사이의 **BPF Map을 이용한 Out-of-band 정책 관리**를 사용한다.

```text
User Controller
      │
      │ flow / deadline / priority
      ▼
   BPF Map
      │
      ▼
  TC-eBPF
      │
      ├── skb->priority
      └── skb->mark
```

eBPF 프로그램은 패킷의 Flow 정보를 기준으로 BPF Map을 조회하고, 현재 Deadline 또는 Flow 중요도에 따라 패킷의 우선순위를 동적으로 결정한다.



---

# 2. Hardware / Software Environment

* Raspberry Pi 5
* Gigabit Ethernet
* Linux Kernel 6.12.x
* PREEMPT_RT
* eBPF / TC
* MQPRIO
* ETF
* PTP / PHC
* `linuxptp`
* `iproute2`
* `libbpf`
* `bpftool`
* `cyclictest`
* `iperf3` / `pktgen`

---

# 3. Kernel Configuration

## 3.1 CPU Isolation and Real-Time

### Required

```text
CONFIG_PREEMPT_RT=y
CONFIG_HIGH_RES_TIMERS=y
CONFIG_HZ_1000=y
CONFIG_NO_HZ_FULL=y
CONFIG_RCU_NOCB_CPU=y
CONFIG_CPUSETS=y
```

| Configuration            | Purpose                                         |
| ------------------------ | ----------------------------------------------- |
| `CONFIG_PREEMPT_RT`      | PREEMPT_RT 기반 실시간 preemption 활성화                |
| `CONFIG_HIGH_RES_TIMERS` | High Resolution Timer 지원                        |
| `CONFIG_HZ_1000`         | 1000 Hz timer tick 설정                           |
| `CONFIG_NO_HZ_FULL`      | RT 전용 CPU의 periodic tick 최소화                    |
| `CONFIG_RCU_NOCB_CPU`    | RT CPU의 RCU callback을 housekeeping CPU로 offload |
| `CONFIG_CPUSETS`         | CPU affinity / cpuset 기반 CPU 할당 지원              |

> `CONFIG_HZ_1000`은 High Resolution Timer의 실제 해상도와 동일한 개념은 아니다.
> `CONFIG_HIGH_RES_TIMERS`와 함께 사용하여 고해상도 타이머 환경을 구성한다.

---

## 3.2 CPU Frequency / Idle / Memory

다음 기능은 **Kconfig에서 제거하는 것이 아니라 커널에 포함한 뒤 runtime에서 정책을 제어**한다.

```text
CONFIG_CPU_FREQ=y
CONFIG_CPU_IDLE=y
CONFIG_TRANSPARENT_HUGEPAGE=y
```

| Configuration                 | Runtime policy                |
| ----------------------------- | ----------------------------- |
| `CONFIG_CPU_FREQ`             | `performance` governor 사용     |
| `CONFIG_CPU_IDLE`             | RT CPU의 idle state를 필요에 따라 제한 |
| `CONFIG_TRANSPARENT_HUGEPAGE` | 실험 조건에 따라 THP를 `never`로 설정    |

예:

```bash
sudo cpupower frequency-set -g performance
```

THP 상태 확인:

```bash
cat /sys/kernel/mm/transparent_hugepage/enabled
```

필요한 경우:

```bash
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
```

THP 비활성화는 PREEMPT_RT의 필수 설정이 아니라, 메모리 관리에 의한 latency 변수를 통제하기 위한 실험 조건이다.

---

# 4. eBPF / TC Configuration

## Required

```text
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y

CONFIG_NET_CLS_BPF=y
CONFIG_NET_CLS_ACT=y

CONFIG_NET_SCH_MQPRIO=y
CONFIG_NET_SCH_ETF=y
```

## Recommended

```text
CONFIG_DEBUG_INFO_BTF=y
```

| Configuration           | Purpose                                 |
| ----------------------- | --------------------------------------- |
| `CONFIG_BPF`            | BPF subsystem                           |
| `CONFIG_BPF_SYSCALL`    | Userspace BPF syscall / program loading |
| `CONFIG_BPF_JIT`        | BPF JIT compiler                        |
| `CONFIG_DEBUG_INFO_BTF` | BTF 기반 개발 및 CO-RE 지원                    |
| `CONFIG_NET_CLS_BPF`    | TC-BPF classifier                       |
| `CONFIG_NET_CLS_ACT`    | TC action framework                     |
| `CONFIG_NET_SCH_MQPRIO` | Multi-Queue Priority scheduling         |
| `CONFIG_NET_SCH_ETF`    | Earliest TxTime First                   |

---

# 5. PTP / Hardware Timestamping

PTP 기반 clock synchronization 및 hardware timestamping을 사용하는 경우 다음 옵션을 활성화한다.

```text
CONFIG_PTP_1588_CLOCK=y
CONFIG_NETWORK_PHY_TIMESTAMPING=y
```

단, hardware timestamping은 Kconfig만으로 보장되지 않는다.

실제 NIC와 Ethernet driver가 PTP/하드웨어 timestamping을 지원해야 한다.

확인:

```bash
ethtool -T eth0
ls -l /dev/ptp*
```

---

# 6. Kernel Boot Parameters

RT 전용 CPU를 CPU 3으로 사용하는 예시:

```text
isolcpus=3
nohz_full=3
rcu_nocbs=3
```

구성:

```text
CPU 0-2
├── 일반 userspace task
├── IRQ
├── RCU callback
├── kworker / housekeeping work
└── Background workload

CPU 3
└── RT control task
```

실제 isolation 확인:

```bash
cat /sys/devices/system/cpu/isolated
```

---

# 7. Runtime Initialization

커널 부팅 후 실험 환경은 다음 순서로 초기화한다.

```text
Boot
 │
 ├── CPU Isolation
 │     ├── isolcpus
 │     ├── nohz_full
 │     └── rcu_nocbs
 │
 ├── CPU Frequency
 │     └── performance governor
 │
 ├── CPU Idle Policy
 │
 ├── IRQ Affinity
 │
 ├── IRQ Thread Priority
 │
 ├── Workqueue Affinity
 │
 ├── PTP
 │     ├── ptp4l
 │     └── phc2sys
 │
 ├── MQPRIO
 │
 ├── ETF
 │
 ├── TC-eBPF
 │
 ├── BPF Map
 │
 └── RT Application
       ├── CPU Affinity
       └── SCHED_FIFO
```

---

# 8. CPU Frequency

`CONFIG_CPU_FREQ=y` 상태에서 CPU frequency scaling을 실험 조건에 맞게 고정한다.

```bash
cpupower frequency-info
```

가능한 경우:

```bash
sudo cpupower frequency-set -g performance
```

목표:

```text
CPU Frequency
      │
      ▼
Fixed / Performance
      │
      ▼
Frequency transition에 의한 latency variation 최소화
```

---

# 9. CPU Idle

RT 전용 CPU에서는 깊은 idle state 진입/복귀에 따른 latency를 고려한다.

지원되는 idle state 확인:

```bash
cat /sys/devices/system/cpu/cpu*/cpuidle/state*/disable
```

실제 Raspberry Pi 5에서 제공되는 idle state를 확인한 후 필요한 범위에서 제한한다.

모든 idle state를 무조건 비활성화하는 것은 기본 설정으로 가정하지 않는다.

---

# 10. RT Task Affinity and Scheduling

RT application을 전용 CPU에 고정한다.

예:

```bash
taskset -c 3 ./control_app
```

또는:

```bash
chrt -f 90 taskset -c 3 ./control_app
```

확인:

```bash
ps -eLo pid,tid,cls,rtprio,pri,psr,comm
```

목표:

```text
CPU 3
└── control_app
      ├── CPU affinity = 3
      └── SCHED_FIFO
```

실제 IRQ thread와 application의 priority 관계는 실험 환경에 맞춰 고정하고 기록한다.

---

# 11. IRQ Affinity

RT CPU에 불필요한 NIC IRQ가 들어오지 않도록 IRQ affinity를 조정한다.

IRQ 확인:

```bash
cat /proc/interrupts
```

Affinity 확인:

```bash
cat /proc/irq/<IRQ>/smp_affinity_list
```

예를 들어 CPU 0-2에서 NIC IRQ를 처리:

```bash
echo 0-2 | sudo tee /proc/irq/<IRQ>/smp_affinity_list
```

목표:

```text
CPU 0-2
├── NIC IRQ
├── 일반 작업
└── housekeeping

CPU 3
└── RT task
```

---

# 12. IRQ Thread Priority

PREEMPT_RT에서는 많은 IRQ가 threaded IRQ 형태로 처리될 수 있다.

IRQ thread 확인:

```bash
ps -eLo pid,tid,cls,rtprio,pri,psr,comm | grep -E 'irq/|thread'
```

필요한 경우 priority를 조정한다.

```bash
sudo chrt -f <PRIORITY> -p <IRQ_THREAD_PID>
```

IRQ priority는 RT application보다 무조건 높게 설정하지 않는다.

실험에서 사용하는 우선순위 정책을 명시적으로 고정하고 모든 Case에서 동일하게 유지한다.

---

# 13. Workqueue Affinity

Linux kernel 내부의 deferred work는 workqueue를 통해 `kworker`에서 실행될 수 있다.

RT 전용 CPU의 isolation을 유지하기 위해 unbound workqueue의 CPU affinity를 확인한다.

```bash
cat /sys/devices/virtual/workqueue/unbound_cpumask
```

workqueue 구조 확인:

```bash
sudo tools/workqueue/wq_dump.py
```

worker 확인:

```bash
ps -eLo pid,tid,psr,comm | grep kworker
```

목표:

```text
CPU 0-2
└── housekeeping / unbound workqueue

CPU 3
└── RT application
```

필요한 경우 unbound workqueue의 CPU mask를 housekeeping CPU로 제한한다.

예:

```bash
echo 0-2 | sudo tee /sys/devices/virtual/workqueue/unbound_cpumask
```

> 모든 workqueue를 무조건 RT CPU에서 제거하는 것이 아니라, 실제 kernel/workqueue 구성을 확인한 후 실험 조건에 맞게 제한한다.

---

# 14. Background Service

RT 전용 CPU에서 불필요한 userspace task가 실행되지 않도록 한다.

확인:

```bash
ps -eLo pid,psr,comm
```

필요한 경우 service affinity를 housekeeping CPU로 이동하거나 불필요한 서비스를 비활성화한다.

모든 실험 Case에서 동일한 background service 조건을 유지한다.

---

# 15. PTP Clock Synchronization

Deadline 및 TxTime 계산과 timestamp 비교의 일관성을 위해 PTP를 사용한다.

전체 구조:

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
Deadline / TxTime
```

## 15.1 PTP capability 확인

```bash
ethtool -T eth0
ls -l /dev/ptp*
```

## 15.2 ptp4l

NIC의 PHC를 PTP 기준 시간에 동기화한다.

```bash
sudo ptp4l -i eth0 -m -H
```

## 15.3 phc2sys

NIC PHC와 system clock을 동기화한다.

```bash
sudo phc2sys -s <PHC> -c CLOCK_REALTIME -m
```

실제 PHC device는 Pi의 NIC driver가 제공하는 `/dev/ptpX`를 확인하여 사용한다.

PTP는 ETF 자체를 대신하는 기능이 아니다.

PTP의 역할은 Deadline / TxTime 계산 및 송수신 timestamp 비교에 사용하는 clock의 기준을 일관되게 만드는 것이다.

---

# 16. MQPRIO

MQPRIO는 TC-eBPF가 결정한 packet priority를 NIC Traffic Class 및 TX queue와 연결한다.

```text
TC-eBPF
   │
   │ skb->priority
   ▼
 MQPRIO
   │
   ├── TC 0 → TX Queue 0
   ├── TC 1 → TX Queue 1
   └── ...
```

Kconfig:

```text
CONFIG_NET_SCH_MQPRIO=y
```

하지만 Kconfig를 활성화하는 것만으로 MQPRIO가 동작하는 것은 아니다.

실험 시작 시 `tc`를 이용하여 실제 qdisc를 설정해야 한다.

---

# 17. ETF

ETF(Earliest TxTime First)는 packet에 설정된 transmit time을 기준으로 송신 시점을 제어한다.

```text
Deadline
    │
    ▼
TxTime calculation
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

Kconfig:

```text
CONFIG_NET_SCH_ETF=y
```

ETF 역시 실제 TX queue에 qdisc를 구성해야 한다.

> TC-eBPF의 `skb->priority` 설정과 ETF의 TxTime scheduling은 서로 다른 기능이다.
> Priority는 MQPRIO를 통한 queue/class 선택에 사용되고, TxTime은 ETF의 transmission scheduling에 사용된다.

---

# 18. TC-eBPF Loading

eBPF 프로그램은 커널 모듈처럼 `insmod`로 올리는 방식이 아니다.

빌드된 BPF object를 userspace에서 로드하고 TC hook에 attach한다.

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

예상 구조:

```text
Application
     │
     ▼
  IP Layer
     │
     ▼
  TC-eBPF
     │
     ├── flow lookup
     ├── BPF Map lookup
     ├── deadline evaluation
     └── skb priority / mark
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

---

# 19. BPF Map Policy

User Space Controller가 각 Flow의 실시간 정책을 BPF Map에 기록한다.

예:

```text
Flow
 ├── flow identifier
 ├── deadline
 ├── priority
 └── policy state
```

Runtime:

```text
User Controller
      │
      ▼
   BPF Map
      │
      ▼
  TC-eBPF
      │
      ▼
 skb->priority / skb->mark
```

이를 통해 애플리케이션 packet format을 변경하지 않고도 runtime에 packet classification policy를 변경할 수 있다.

---

# 20. Experimental Traffic

실시간 제어 트래픽:

```text
Protocol : UDP
Period   : 1 ms
Deadline : 1 ms 또는 상대 Deadline
```

Background traffic:

```text
0%
50%
70%
80%
90%
```

Best-Effort traffic은 `iperf3` 또는 Linux `pktgen` 등을 이용하여 생성한다.

혼잡 환경에서 TC-eBPF + MQPRIO + ETF가 Deadline Miss 및 Tail Latency에 미치는 영향을 측정한다.



---

# 21. Experimental Cases

동일한 하드웨어 및 트래픽 조건에서 다음 네 가지 Case를 비교한다.

| Case   | Kernel     | TC-eBPF | MQPRIO | ETF | 목적                    |
| ------ | ---------- | ------- | ------ | --- | --------------------- |
| Case 1 | Vanilla    | X       | X      | X   | Baseline              |
| Case 2 | PREEMPT_RT | X       | X      | X   | CPU RT 효과             |
| Case 3 | Vanilla    | O       | O      | O   | Network scheduling 효과 |
| Case 4 | PREEMPT_RT | O       | O      | O   | CPU + Network 통합      |

### Case 1 — Vanilla

```text
Non-RT Linux
+ 기본 network stack
```

Baseline으로 사용한다.

### Case 2 — RT-Only

```text
PREEMPT_RT
+ SCHED_FIFO
+ CPU isolation
+ IRQ affinity
+ IRQ priority
```

CPU scheduling의 실시간성 개선 효과를 측정한다.

### Case 3 — TC-eBPF Only

```text
Non-RT Linux
+ TC-eBPF
+ BPF Map
+ MQPRIO
+ ETF
```

Network scheduling 자체의 효과를 측정한다.

### Case 4 — RT + TC-eBPF

```text
PREEMPT_RT
+ CPU RT tuning
+ TC-eBPF
+ BPF Map
+ MQPRIO
+ ETF
```

CPU scheduling과 network scheduling을 통합했을 때의 효과를 측정한다.



---

# 22. Common Experimental Conditions

실험 결과에 영향을 줄 수 있는 외부 변수를 통제하기 위해 다음 조건을 일정하게 유지한다.

## RT Cases

Case 2 / Case 4:

* `SCHED_FIFO`
* CPU Isolation
* CPU Affinity
* IRQ Affinity
* IRQ Thread Priority
* Workqueue Affinity
* CPU Frequency Policy
* PTP synchronization

## All Cases

* 동일한 hardware
* 동일한 kernel base
* 동일한 CPU frequency policy
* 동일한 background service 조건
* 동일한 traffic generation
* 동일한 packet size
* 동일한 test duration
* 동일한 measurement method

---

# 23. Runtime Setup Summary

전체 초기화 순서:

```text
1. Kernel Boot
   ├── isolcpus
   ├── nohz_full
   └── rcu_nocbs

2. CPU Runtime Policy
   ├── performance governor
   └── CPU idle policy

3. CPU / IRQ Isolation
   ├── RT task affinity
   ├── IRQ affinity
   └── IRQ thread priority

4. Kernel Background Work
   ├── workqueue affinity
   └── background service control

5. Clock Synchronization
   ├── PTP / PHC check
   ├── ptp4l
   └── phc2sys

6. Network Scheduling
   ├── MQPRIO
   └── ETF

7. eBPF
   ├── Load BPF program
   ├── TC attach
   └── Initialize BPF Map

8. Experiment
   ├── Generate control traffic
   ├── Generate background traffic
   └── Collect latency metrics
```

---

# 24. Measurement Metrics

주요 평가 지표:

### Average Latency

평균 패킷 전송 지연.

### 99% / 99.9% Tail Latency

상위 1%, 0.1% 구간의 latency distribution.

### Worst-Case Latency

측정된 최대 latency.

### Deadline Miss Ratio

```text
Deadline Miss Ratio
=
Deadline을 초과한 packet 수
-------------------------
전체 packet 수
```

본 연구의 핵심 지표로 사용한다.

특히 Background Load가 증가하는 상황에서 Deadline Miss Ratio가 어떻게 변화하는지 비교한다.



---

# 25. Expected Results

예상되는 비교 방향:

```text
Case 1 → Case 2
```

PREEMPT_RT 적용으로 CPU scheduling 및 IRQ response 측면의 latency variation 감소.

```text
Case 1 → Case 3
```

TC-eBPF + MQPRIO + ETF 적용으로 network congestion 상황에서 긴급 packet의 scheduling 개선.

```text
Case 3 → Case 4
```

PREEMPT_RT를 추가함으로써 CPU scheduling jitter까지 함께 줄여 전체 시스템의 latency distribution을 안정화.

핵심적으로:

```text
                CPU Layer       Network Layer

Case 1             -                 -
Case 2           RT                  -
Case 3             -             eBPF + MQPRIO + ETF
Case 4           RT              eBPF + MQPRIO + ETF
```

를 비교함으로써 각 기술 요소의 기여도를 분리한다.



---

# 26. Project Goal

본 프로젝트의 핵심은 단순히 PREEMPT_RT 또는 TSN Qdisc를 사용하는 것이 아니다.

```text
Runtime Deadline Policy
          │
          ▼
       BPF Map
          │
          ▼
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

          +

      PREEMPT_RT
          │
          ▼
 CPU Scheduling / IRQ / Isolation
```

즉,

**CPU 계층의 실시간성(PREEMPT_RT)** 과
**네트워크 계층의 Deadline-Aware scheduling(TC-eBPF + MQPRIO + ETF)** 을 통합하고,

혼잡 환경에서 다음 지표가 실제로 개선되는지를 정량적으로 검증하는 것을 목표로 한다.

* Average Latency
* Tail Latency
* Worst-Case Latency
* Deadline Miss Ratio

```

이 정도면 GitHub에서는 **README의 프로젝트 설명 + 환경 구축 문서** 역할을 같이 할 수 있어. 특히 네가 지금 실제로 커널 빌드/런타임 세팅을 시작하는 단계라서, 이 문서를 나중에 `docs/setup.md`로 분리해도 구조가 자연스럽다.

```
