# A/A-급 논문 리딩 중심 설계 검토 v7 — 칠판 Ground Truth 반영


> [KEEP] / [VERIFY] / [PROPOSE] / [OPTIONAL] / [NO CHANGE]를 사용해
> "현재 설계 사실"과 "논문을 보고 추가로 제안하는 것"을 분리함.
>
> 특히 `XDP_PASS ablation`, IRQ 재배치, 추가 tracing 등은 현재 기본 설계가 아니라 선행연구 분석에서 나온 제안일 수 있으므로 구분해서 작성함.

---

# 0. 현재 Ground Truth — 칠판 기준

## 0.1 전체 시스템 구조

칠판에서 가장 명확한 구조는 다음과 같음.

```text
                         Embedded NIC
                              │
                        Single RX path
                              │
                             XDP
                              │
              ┌───────────────┼───────────────┐
              │               │               │
              ▼               ▼               ▼
           AF_XDP          CPUMAP          XDP_PASS
              │               │               │
          Userspace         RT CPU        Normal Linux
          fast path       + PREEMPT_RT       path
                              │
                       Linux network stack
```

본 프로젝트에서 XDP는 단순한 성능 향상 기술이 아니라 packet classifier / dispatcher임.

XDP가 패킷의 특성을 보고 서로 다른 execution path를 선택함.

---

## 0.2핵심 설계 질문

```text
왜 XDP인가?
왜 PREEMPT_RT인가?
왜 CPUMAP인가?
왜 AF_XDP인가?
왜 DPDK가 아닌가?
커널 기능이 필요한 traffic은 무엇인가?
어떤 traffic을 어떤 path로 보낼 것인가?
```
 논문을 읽을 때도
단순히 "비슷한 기술을 썼다"가 아니라
이 질문 중 무엇에 근거를 제공하는 논문인지를 확인해야 함.

---

## 0.3 현재 traffic classification 철학

 현재 분류 철학은 다음과 같음.

```text
Packet
  ↓
XDP classifier
  ↓
criticality / determinism requirement
  +
kernel networking functionality requirement
  ↓
```

### Path 1 — AF_XDP

```text
Critical
+
kernel networking 기능 불필요
        ↓
AF_XDP
        ↓
Userspace
```

목적:

- 가장 짧은 packet-to-userspace path
- kernel networking stack bypass
- ultra-low-latency reference/processing path

---

### Path 2 — CPUMAP + PREEMPT_RT

```text
Critical
+
kernel networking 기능 유지 필요
        ↓
XDP
        ↓
CPUMAP
        ↓
RT CPU
        ↓
XDP_PASS / SKB 전환
        ↓
Linux network stack
```

목적:

- kernel networking functionality 유지
- critical packet-processing workload를 RT execution domain으로 steering
- AF_XDP와 normal path 사이의 trade-off

칠판 기준으로 CPUMAP의 당위성은 단순히:

```text
"CPU를 바꾼다"
```

가 아니라:

> kernel stack을 완전히 bypass하지 않으면서 latency-sensitive traffic을 RT execution domain으로 보낼 수 있는 경로

로 설명하는 것이 더 정확함.

---

### Path 3 — XDP_PASS

```text
Best-effort / low-criticality
        ↓
XDP_PASS
        ↓
Normal Linux networking path
```

목적:

- 기존 Linux networking compatibility 유지
- 별도의 RT isolation 또는 kernel bypass가 필요하지 않은 traffic 처리

---

## 0.4 Kernel functionality가 필요한 이유

칠판 오른쪽에 적힌 예시를 기준으로 다음과 같은 기능을 고려함.

```text
TCP processing
firewall / netfilter
security mechanism
routing / policy routing
standard socket API
existing middleware compatibility
```

주의:

> 이 예시가 "본 실험의 main traffic을 TCP로 바꾼다"는 뜻은 아님.

Main latency experiment는 현재대로 UDP를 사용할 수 있음.

TCP/firewall/security는 왜 일부 traffic이 kernel path를 유지해야 하는가를 설명하는 설계 rationale임.

---

## 0.5 왜 DPDK가 아닌가?

칠판에서 명시적으로 반론 포인트로 적혀 있음.

현재 설계의 방향은 모든 traffic을 userspace bypass로 보내는 것이 아니라:

```text
AF_XDP
CPUMAP + kernel stack
Normal kernel path
```

를 하나의 system에서 공존시키는 것임.

따라서 현재 프로젝트 관점의 설명은:

> DPDK처럼 전체 packet processing을 userspace 중심으로 재구성하는 것이 목적이 아니라,
> kernel 기능이 필요한 traffic과 그렇지 않은 traffic을 XDP에서 구분하여 서로 다른 execution path로 선택적으로 보내는 구조를 평가함.

이 문장은 현재 설계 rationale이고,
DPDK의 성능/구조를 정량적으로 비교한 논문 근거가 추가되면 더 강화할 수 있음.

---

## 0.6 CPU 구성 — 현재 확정 사실

현재 설계에서:

```text
CPU0 ─┐
CPU1 ─┼─ general / normal processing 영역
CPU2 ─┘

CPU3 ─── RT-critical 전용 core
```

CPU3은 RT workload용으로 고정함.

중요:

```text
PREEMPT_RT는 CPU3에만 적용되는 것이 아님.
전체 kernel이 PREEMPT_RT kernel로 동작함.

CPU3은 그 위에서 RT-critical workload 전용 execution domain으로 구성함.
```

CPUMAP의 역할:

```text
selected critical packet
        ↓
CPUMAP
        ↓
CPU3 RT execution domain
```

다만 CPU0/1/2 중 정확히 어느 core가 NIC IRQ/NAPI를 담당할지는 확정된 사실로 보지 않음.

이는 실제 platform behavior를 측정한 뒤 결정해야 하는 [VERIFY] 항목임.

---

# 1. 논문 평가 기준

각 논문은 다음 순서로 평가함.

## Grade

```text
A / A- /
```

## 설계 영향 태그

```text
[KEEP]
현재 설계를 지지

[VERIFY]
현재 설정이 실제 platform에서 성립하는지 확인 필요

[PROPOSE]
논문 근거를 바탕으로 추가 제안

[OPTIONAL]
좋은 아이디어지만 구현량/범위 때문에 선택 사항

[NO CHANGE]
관련은 있지만 현재 설계를 바꿀 근거는 아님
```

---

# 2. A/A-급 논문 PDF 다운로드

> 아래는 현재 대화에 업로드되어 로컬에 존재하는 PDF 기준.

- [Towards Timing Isolation for Mixed-Criticality Communication in Software-Defined Vehicles](sandbox:/mnt/data/Towards_Timing_Isolation_for_Mixed-Criticality_Communication_in_Software-Defined_Vehicles.pdf)
- [XvSomeIP: A High-Performance In-Vehicle Communication Middleware Based on XDP](sandbox:/mnt/data/XvSomeIP_A_High-Performance_In-Vehicle_Communication_Middleware_Based_on_XDP.pdf)
- [Evaluating AMP for Automotive Gateways](sandbox:/mnt/data/Evaluating_AMP_for_Automotive_Gateways.pdf)
- [Raspberry Pi 5 PREEMPT_RT assessment](sandbox:/mnt/data/ecrtsOspert2024_dewit_rtlinux_paper.pdf)
- [New Insights Into the Real-Time Performance of a Multicore Processor](sandbox:/mnt/data/New_Insights_Into_the_Real-Time_Performance_of_a_Multicore_Processor.pdf)
- [OPC UA Safety Experimental Evaluation](sandbox:/mnt/data/master_Yang_Zong-Rong_2026.pdf)
- [Composable Distributed Real-Time Systems with Deterministic Network Channels](sandbox:/mnt/data/1-s2.0-S1383762123000322-main.pdf)
- [Startup Delay Performance Evaluation for SOME/IP](sandbox:/mnt/data/Startup_Delay_Performance_Evaluation_for_SOME_IP_A_Comparative_Analysis_on_Linux_Network_API.pdf)
- [Latency-Aware Industrial CPS for Connected Mobility](sandbox:/mnt/data/Latency-Aware_Industrial_CPS_for_Connected_Mobility_Real-Time_Benchmarks_from_NMPC_to_Edge_Lookup_Policies.pdf)
- [eBPF-XDP Hardware-Based Network Slicing Architecture](sandbox:/mnt/data/An_eBPF-XDP_Hardware-Based_Network_Slicing_Architecture_for_Future_6G_Front-_to_Back-Haul_Networks.pdf)
- [In-Kernel Packet Sanitization and Normalization Using eBPF](sandbox:/mnt/data/277679727.pdf)

`eCounter`는 현재 로컬 PDF가 없고 이전에 본문 텍스트로 분석한 논문임.

---

# 2. A급 논문 1 — Towards Timing Isolation for Mixed-Criticality Communication in Software-Defined Vehicles

[PDF 다운로드](sandbox:/mnt/data/Towards_Timing_Isolation_for_Mixed-Criticality_Communication_in_Software-Defined_Vehicles.pdf)

## 등급: A

## 5분 요약

### 1) 왜 연구했나?

중앙집중형 Linux 기반 차량 아키텍처에서 mixed-criticality application이 같은 Linux network stack과 CPU resource를 공유하면 latency/jitter가 불안정해질 수 있기 때문.

### 2) 무엇을 만들었나?

대략 다음 요소를 결합한 구조:

```text
PREEMPT_RT
+
fixed-priority middleware scheduler
+
XDP/eBPF filtering
+
AF_XDP userspace fast path
+
dedicated NIC queue
```

### 3) 어떻게 실험했나?

- PREEMPT_RT Linux
- critical DDS/RTPS UDP traffic
- 2 kHz / 500 us 수준 주기
- payload 64 / 512 / 1024 B
- iperf3 UDP interference
- baseline vs proposed architecture 비교
- baseline과 proposed 모두 RT scheduling/pinning을 사용

### 4) 결과는?

network interference가 있을 때 proposed architecture가 baseline보다
maximum latency/jitter를 크게 줄임.

중요한 점:

> 이 논문은 여러 isolation mechanism을 한 번에 결합해서 좋은 결과를 보여주었지만,
> 각 mechanism의 독립적인 contribution을 분리하지 않았음.

### 5) 우리 연구와 겹치는 점

매우 큼.

```text
Linux
PREEMPT_RT
mixed-criticality
UDP
XDP
AF_XDP
tail latency
network interference
```

### 6) 우리 설계에 재사용 가능한 것

- tail/maximum latency 중심 평가
- heavy network interference 조건
- RT task affinity/SCHED_FIFO를 baseline에도 적용해 공정성 유지

## 우리 설계에 대한 근거 기반 제안

### 제안

Case 3의 `XDP + CPUMAP` 개선 효과를 해석할 때
XDP hook 자체와 CPUMAP steering을 구분할 수 있는 ablation case를 하나 추가하는 것을 고려.

예:

```text
Optional Ablation A:
PREEMPT_RT + 동일 XDP classifier + forced XDP_PASS
```

이건 "논문에 XDP_PASS case가 있어서 따라 하는 것"이 아님.

정확한 근거는:

> 가장 가까운 선행연구가 여러 isolation mechanism을 한꺼번에 켜서 individual contribution을 분리하지 못했다는 methodological gap을 우리가 피하자는 제안.

즉 A급 논문 분석으로부터 도출한 우리 실험 설계 제안임.

---

# 3. A급 논문 2 — XvSomeIP: A High-Performance In-Vehicle Communication Middleware Based on XDP

[PDF 다운로드](sandbox:/mnt/data/XvSomeIP_A_High-Performance_In-Vehicle_Communication_Middleware_Based_on_XDP.pdf)

## 등급: A

### 1) 왜 연구했나?

기존 vSomeIP(SOME/IP 쉽게 사용하게 해주는 C++라이브러리)가 Linux kernel network stack을 거치면서 발생하는 packet-processing overhead를 줄이고 automotive middleware 성능을 높이기 위해.

### 2) 무엇을 만들었나?

```text
SOME/IP
+
XDP
+
AF_XDP
+
UMEM
+
RX/TX/FILL/COMPLETION rings
+
CPU affinity
```

### 3) 어떻게 실험했나?

- 두 Linux host
- 다양한 payload
- 약 1M message 규모
- RTT latency
- throughput
- send/receive capability 비교

### 4) 결과는?

큰 payload에서 latency가 줄고,
throughput/send-receive capability가 크게 향상됨.

### 5) 우리와 겹치는 점

- automotive communication
- XDP
- AF_XDP
- CPU affinity
- packet-path optimization

### 6) 재사용 가능한 점

AF_XDP reference implementation을 만들 때:

```text
UMEM
RX ring
TX ring
FILL ring
COMPLETION ring
```

구조를 확인하는 좋은 구현 참고문헌.

## 우리 설계에 대한 제안

AF_XDP를 메인으로 승격할 근거는 아님.

오히려 이 논문은 AF_XDP가 좋은 fast-path reference라는 근거로 사용.

따라서 현재처럼:

```text
Main = XDP + CPUMAP
Reference = AF_XDP
```

를 유지하는 것이 자연스러움.

---

# 4. A급 논문 3 — Evaluating AMP for Automotive Gateways

[PDF 다운로드](sandbox:/mnt/data/Evaluating_AMP_for_Automotive_Gateways.pdf)

## 등급: A

### 왜 중요하나?

실제 NXP S32G3 automotive gateway에서
real-time workload를 다른 core로 분리해도
shared memory, bus, interrupt, Linux forwarding path 등 때문에
전체 end-to-end tail latency가 자동으로 bound되지 않음을 보여줌.
>NXP S32G3 Automotive Gateway는 쉽게 말하면 자동차 내부의 여러 ECU·네트워크를 연결하고, 데이터를 중계·처리·보안까지 담당하는 고성능 차량용 게이트웨이 프로세서 플랫폼
### 실험 특징

- 실제 automotive gateway
- PREEMPT_RT Linux
- Cortex-A53 + Cortex-M7 AMP
- 1 ms / 1000 PPS safety-critical stream
- idle vs burst
- P95/P99/worst-observed 계열
- software loopback + oscilloscope local measurement

### 핵심 결과

한 stage를 isolation한다고 전체 path가 자동으로 deterministic해지는 것은 아님.

## 우리 설계에 주는 의미

이 논문은 CPUMAP을 쓰지 않음.

따라서:

```text
"이 논문이 CPUMAP이 좋다고 증명했다"
```

라고 쓰면 안 됨.

대신 다음 논리의 근거임.

> CPU/core isolation만으로 network path 전체의 tail problem이 해결되지 않을 수 있으므로,
> packet-processing path 자체를 제어하는 mechanism을 별도로 평가할 가치가 있다.

즉 우리 XDP + CPUMAP 실험의 동기 강화에 사용.

또한 1 ms / 1000 PPS traffic은
우리 experimental benchmark 설정의 참고 근거가 될 수 있음.

---

# 5. A급 논문 4 — Raspberry Pi 5 PREEMPT_RT 평가

[PDF 다운로드](sandbox:/mnt/data/ecrtsOspert2024_dewit_rtlinux_paper.pdf)

## 등급: A

### 연구 내용

Raspberry Pi 5에서 Vanilla/일반 Linux와 PREEMPT_RT를 비교하고
`cyclictest`, `stress-ng`, `iperf3` 등을 이용해 실시간성을 평가.

### 우리와 겹치는 점

매우 직접적.

```text
Raspberry Pi
PREEMPT_RT
stress-ng
iperf3
determinism
scheduling latency
```

### 우리에게 재사용 가능한 것

- Raspberry Pi를 RT experimental platform으로 사용하는 근거
- `cyclictest`를 main network metric이 아니라 kernel pre-validation으로 사용하는 근거
- CPU/network stress toolchain의 근거

## 설계 제안

현재:

```text
cyclictest
→ PREEMPT_RT 사전검증

Custom OWD
→ 실제 network experiment
```

구조 유지 권장.

---

# 6. A급 논문 5 — New Insights Into the Real-Time Performance of a Multicore Processor

[PDF 다운로드](sandbox:/mnt/data/New_Insights_Into_the_Real-Time_Performance_of_a_Multicore_Processor.pdf)

## 등급: A

### 왜 중요하나?

우리의 "CPU3을 RT용으로 고정"하는 생각과 가장 직접적으로 연결되는 방법론 논문.

### 핵심 내용

- RT multicore scheduling latency 분석
- `isolcpus`
>isolcpus: 커널 부팅 옵션 중 하나로, 특정 CPU 코어를 일반 스케줄링 대상에서 최대한 제외하는 설정, 원하는 CPU 코어에 일반 task 안올라가게
- RT/non-RT core separation
- interrupt 배치
- memory/CPU/network interference
- `mlockall`
- 장시간/대규모 sample

### 우리에게 중요한 교훈

```text
task affinity
≠
완전한 CPU isolation
```
>task affinity: 특정 프로세스나 스레드늘 특정 CPU 코어에 지정
thread를 CPU3에 pinning해도:

- IRQ
- kernel thread
- softirq
>인터럽트 처리를 커널이 다 안 끝내고 소프트웨어 인터럽트 형태로 처리하는 메커니즘
- housekeeping work

가 같은 CPU에서 동작하면 RT core가 여전히 방해받을 수 있음.

## 현재 프로젝트와 연결

현재 프로젝트에서 CPU3은 RT-critical workload용으로 고정한 상태임.

이 논문을 바탕으로 제안할 수 있는 것은:

> CPU3을 RT task용으로 고정한다면,
> application affinity만 확인하지 말고
> 해당 core로 들어오는 IRQ/non-RT workload가 실제로 얼마나 남는지 확인하는 것이 좋다.

### 제안 수준

추천이지 확정 변경 아님.

예:

```text
현재:
CPU3 = RT task fixed

추가 검토 후보:
- CPU3 IRQ 유입 확인
- irqbalance 상태 확인
- 필요할 경우 IRQ affinity 조정
- 필요할 경우 isolcpus/cpuset 검토
```

CPU0/CPU1/CPU2를 정확히 무엇에 쓸지는
이 논문이 정해주는 것이 아니므로
실제 Pi IRQ/NAPI behavior를 측정한 뒤 결정해야 함.

---

# 7. A급 논문 6 — OPC UA Safety Experimental Evaluation

[PDF 다운로드](sandbox:/mnt/data/master_Yang_Zong-Rong_2026.pdf)

## 등급: A

### 왜 중요하나?

우리 구조 자체보다 측정 실험 방법론에 직접적인 근거를 줌.

### 핵심 특징

- PREEMPT_RT embedded platform
- software timestamp
- packet/message pairing
- 1,000,000 sample 규모
- automated experiment
- 반복 실험 중 drift 관찰
- reboot 후 distribution 안정화 확인

## 우리에게 재사용 가능한 것

```text
Application boundary timestamp
큰 sample count
run automation
run-to-run variability 확인
reboot/drift pilot
````

# 8. A-급 — 설계/측정에 강한 보조 근거

## A-1. Composable Distributed Real-Time Systems with Deterministic Network Channels

[PDF 다운로드](sandbox:/mnt/data/1-s2.0-S1383762123000322-main.pdf)

중요 포인트:

- Vanilla vs PREEMPT_RT
- CPU load + network interference
- PTP
- memory preallocation / memory locking
- packet hot path에서 file I/O 회피
- 결과를 memory에 저장한 뒤 test 후 disk write

### 우리에게 직접 반영

현재 RAM logging:

```text
tmpfs
→ mmap
→ mlockall
→ pre-touch
→ fixed-size buffer
→ after-run dump
```

방향을 지지하는 강한 근거.


논문이 직접 지지하는 핵심은:

```text
measurement hot path에서 file I/O 회피
preallocated/locked memory 사용
test 후 저장
```

이고,

`tmpfs + mmap`은 그 원칙을 구현하기 위한 우리의 구체적 설계 선택임.

---

## A-2. Startup Delay Performance Evaluation for SOME/IP

[PDF 다운로드](sandbox:/mnt/data/Startup_Delay_Performance_Evaluation_for_SOME_IP_A_Comparative_Analysis_on_Linux_Network_API.pdf)

중요 포인트:

```text
SCHED_FIFO
CPU affinity
mlockall
```

실제 automotive Linux 환경에서 사용.

우리에게는:

- RT memory locking
- affinity
- observer effect를 줄이는 setup

의 보조 근거.

---

## A-3. Latency-Aware Industrial CPS for Connected Mobility

[PDF 다운로드](sandbox:/mnt/data/Latency-Aware_Industrial_CPS_for_Connected_Mobility_Real-Time_Benchmarks_from_NMPC_to_Edge_Lookup_Policies.pdf)

중요 포인트:

- p50/p95/p99/observed max
- warm-up discard
- timing harness overhead pretest
- tail outlier 중요성

우리에게:

```text
E0 Logging OFF vs ON
warm-up
Observed Max
```

설계의 근거.

---

## A-4. eBPF-XDP Hardware-Based Network Slicing Architecture

[PDF 다운로드](sandbox:/mnt/data/An_eBPF-XDP_Hardware-Based_Network_Slicing_Architecture_for_Future_6G_Front-_to_Back-Haul_Networks.pdf)

중요 포인트:

```text
flow classification
→ RX queue
→ AF_XDP
→ dedicated CPU
```

>비슷한듯 다른점: 6G smart NIC 결합에서 슬라이스 간섭 줄임, 처리량
> embede, RT, 3계층으로 나눠진 처리경로(CPU MAP), 실시간성, 결정성


queue/IRQ/CPU가 서로 독립 변수가 아니라는 점을 이해하는 참고문헌.

우리 CPUMAP과 동일 구조가 아니고, 임베디드 환경이나 단일큐가 아니고, smart NIC, 단계적 부하등 차이가 있으므로
직접 근거로 과장하지 않음.

---

## A-5. eCounter

> 현재 이 대화에 로컬 PDF가 없어서 sandbox 다운로드 링크는 없음. 이 논문은 이전에 본문 텍스트로 분석한 상태임.

중요 포인트:

- XDP/TC hook 비교
- 반복 run
- user-space telemetry observer effect 측정
- Arm/x86 비교

우리에게:

```text
perf/ftrace/telemetry를 main run과 diagnostic run으로 분리
```

할 이유를 강화.

---

## A-6. In-Kernel Packet Sanitization and Normalization Using eBPF

[PDF 다운로드](sandbox:/mnt/data/277679727.pdf)

중요 포인트:

- XDP internal processing latency
- per-CPU BPF map
- rule complexity
- synthetic packet-rate sweep
- XDP generic 환경


>처리 병목은 파싱하는 부분에서 발생하지 않는다는 강력한 증거.

우리에게:

> 필요하면 XDP/CPUMAP 내부 timing을 eBPF timestamp로 별도 diagnostic할 수 있음.

---
# Appendix — 현재 실험/구현 세부안

> 아래는 논문 분석 결과를 실제 실험으로 옮길 때 참고하는 implementation draft임. A급 논문 분석보다 우선하는 확정안이 아님.

---

# 1. 연구 목적

Resource-constrained ARM Embedded Linux에서 critical UDP traffic을 대상으로,
CPU/network interference가 tail latency와 결정성에 미치는 영향을 측정하고,
XDP + CPUMAP을 이용한 packet-processing CPU steering/isolation이 일반 Linux stack 대비 얼마나 효과적인지 평가함.

핵심 질문:

> PREEMPT_RT 환경에서 kernel stack을 완전히 bypass하지 않고도,
> XDP + CPUMAP으로 packet-processing workload를 특정 CPU로 steering했을 때
> 일반 Linux network stack 대비 tail latency와 결정성이 개선되는가?

부가 질문:

1. XDP hook 자체의 overhead는 어느 정도인가?
2. CPUMAP steering이 실제 tail 개선에 기여하는가?
3. AF_XDP kernel bypass와 비교하면 XDP + CPUMAP은 어느 정도 수준인가?

---

# 2. 기본 실험 토폴로지

```text
Raspberry Pi (Sender)
        │
        │ Ethernet / UDP
        ▼
Raspberry Pi (Receiver / DUT)
```

가능하면 network stress용 장비를 하나 더 두는 것이 가장 깨끗함.

---

---

# Multi-queue는 메인 구조에 사용하지 않음

참고 논문처럼 dedicated NIC queue 여러 개를 구성하는 것이 메인 목표가 아님.

본 연구는:

```text
software packet-processing placement
=
XDP + CPUMAP
```

에 집중함.

따라서 contribution은:

> resource-constrained ARM Embedded Linux에서 XDP + CPUMAP의
> packet-processing steering 효과를 CPU/network interference 조건에서 실험적으로 분석

으로 잡음.

---

# Traffic

Main critical traffic:

```text
Protocol : UDP
Period   : 1 ms
Rate     : 1000 PPS
Payload  : 기본 64 B
```

1 ms는 automotive standard deadline이라고 주장하지 않고
experimental benchmark period/deadline이라고 표현함.

TCP는 넣지 않아도 됨.

UDP normal socket만으로도:

- routing
- qdisc / TC
- netfilter
- policy routing
- standard socket API
- middleware compatibility

등의 standard Linux networking 기능을 유지할 수 있음.

---

# Interference matrix

| Condition | CPU stress | Network stress |
|---|---:|---:|
| I0 Idle | Off | Off |
| I1 CPU | On | Off |
| I2 Network | Off | On |
| I3 Combined | On | On |

Combined:

```text
stress-ng
+
iperf3 UDP background traffic
```

동시 적용.

---

# CPU stress

도구:

```text
stress-ng
```

목적:

> CPU contention 상황에서 packet-processing 결정성이 얼마나 흔들리는지 확인.

고정 80% 같은 값을 처음부터 가정하지 말고
pilot에서 실제 per-core utilization을 보고 high-but-stable load를 정함.

---

# 13. Network stress

도구:

```text
iperf3 -u
```

예:

```bash
iperf3 -c <RECEIVER_IP> -u -b 500M -t 60
```

단 500 Mbps를 최종 값으로 미리 확정하지 않음.

먼저:

```text
offered load
actual throughput/PPS
critical OWD
packet loss
receiver CPU
```

를 보고 stable max를 찾은 뒤
main high load를 대략 stable max의 85~90% 수준에서 정함.

---
# Receiver sample record

예:

```c
struct sample_record {
    uint64_t seq;
    int64_t tx_ns;
    int64_t rx_ns;
    uint32_t len;
    uint16_t flags;
    uint16_t reserved;
};
```

hot path에서 계산하지 않고
raw timestamp만 저장해 offline에서 계산함.

---

# 18. RAM logging 구조

관찰자 효과를 줄이기 위해:

```text
tmpfs
+
mmap
+
mlockall
+
pre-touch
+
preallocated linear buffer
```

사용.

실험 전:

```text
open
→ ftruncate
→ mmap
→ mlockall(MCL_CURRENT | MCL_FUTURE)
→ mapped area pre-touch
```

---

# 19. Receiver hot path

실험 중:

```text
recv
→ rx timestamp
→ mapped_buffer[index]에 fixed struct 기록
→ index++
```

하지 않을 것:

```text
printf per packet
fprintf
write
fsync
malloc
calloc
realloc
CSV
JSON
```

즉 packet마다 disk/file I/O 하지 않음.

---

# 20. 왜 linear buffer인가?

한 run의 packet 수를 미리 알기 때문.

```text
records[0]
records[1]
...
records[N-1]
```

장점:

- overwrite 없음
- synchronization 단순
- seq validation 쉬움
- 구현 단순

ring buffer는 필요 없음.

---

# 21. 실험 종료 후

```text
tmpfs mmap buffer
        ↓
run 종료
        ↓
valid sample count 확인
        ↓
samples.bin 저장/copy
        ↓
Python offline analysis
```

---

# 22. Main packet count

권장:

```text
200,000 valid packets / run
×
5 independent runs / condition
```

즉 condition당 총 1,000,000 packets.

1 kHz라면 한 run 약 200초.

---

# 23. Warm-up

recording 전 약:

```text
5,000 packets
```

warm-up 권장.

목적:

- ARP/neighbour
- cache
- BPF JIT
- AF_XDP ring/UMEM
- CPU frequency

등 초기 상태 안정화.

---

# 24. Main experiment vs Diagnostic experiment

분리함.

Main:

```text
OWD
loss
CPU
NIC
PTP
```

Diagnostic:

```text
perf
ftrace / trace-cmd
rtla
```

Main OWD run에서 perf/ftrace/rtla를 상시 켜지 않는 이유:

> profiling/tracing 자체가 observer effect를 만들 수 있기 때문.

---

# 25. ★ perf / ftrace / rtla 실무 경험용

이 도구들은 "써봤다"가 아니라:

> tail latency 이상을 발견하고 Linux kernel/runtime 관점에서 원인을 좁혔다

는 경험을 만드는 데 사용.

---

# 26. ★ perf stat 경험용

대표 counter:

```text
cycles
instructions
cache-references
cache-misses
context-switches
cpu-migrations
page-faults
```

예:

```bash
perf stat -p <RX_PID>   -e cycles,instructions,cache-references,cache-misses,context-switches,cpu-migrations,page-faults   -- sleep 30
```

핵심 비교:

```text
C1 vs C2
→ XDP hook 추가 비용

C2 vs C3
→ CPUMAP steering이 CPU behavior에 미친 변화

C3 vs C4
→ kernel path 유지 vs kernel bypass
```

---

# 27. ★ perf record/report 경험용

대표 case에서:

```bash
perf record -g -p <RX_PID> -- sleep 30
perf report
```

목적:

- normal stack hotspot
- XDP/CPUMAP case hotspot
- AF_XDP userspace processing hotspot

비교.

결과 보기 전 특정 함수가 반드시 나온다고 단정하지 않음.

---

# 28. ★ ftrace / trace-cmd 경험용

관심:

```text
sched_switch
sched_wakeup
IRQ
softirq
NAPI/network receive
```

예:

```bash
sudo trace-cmd record   -e sched:sched_switch   -e sched:sched_wakeup   -e irq:irq_handler_entry   -e irq:irq_handler_exit   -e irq:softirq_entry   -e irq:softirq_exit   sleep 30
```

분석:

```bash
trace-cmd report
```

실제 network tracepoint는:

```bash
trace-cmd list -e
```

로 kernel에서 지원되는 항목을 먼저 확인.

---

# 29. ★ rtla osnoise 경험용

목적:

```text
IRQ
softirq
preemption
kernel activity
```

로 인해 critical CPU가 방해받는 시간을 정량화.

특히:

```text
C2 XDP_PASS
vs
C3 XDP + CPUMAP
```

에서 CPUMAP steering이 OS noise/CPU occupancy에 어떤 영향을 줬는지 보조 분석.

---

# 30. rtla timerlat — optional

Main metric은 network OWD이므로 필수는 아님.

다만:

```text
1 ms periodic sender wake-up jitter
RT thread scheduling latency
```

가 의심될 때 사용.

---

# 31. cyclictest

Main network metric이 아님.

역할:

```text
Vanilla vs PREEMPT_RT
기본 scheduling latency validation
```

정리:

```text
cyclictest
→ RT kernel validation

Custom OWD
→ Main network metric

perf/ftrace/rtla
→ root-cause diagnostic
```

---

# 34. mpstat

```bash
mpstat -P ALL 1
```

확인할 것:

- CPUMAP target CPU
- stress-ng CPU
- critical receiver CPU
- per-core utilization 변화

---

# 35. ethtool

실험 전/후:

```bash
ethtool -S eth0
```

확인:

- packet
- drop
- error
- driver counter

또한 metadata에:

- MTU
- ring
- offload
- coalescing
- NIC driver

기록.

---

# 36. bpftool

C2/C3/C4에서:

- XDP program attach
- BPF map
- CPUMAP
- XSKMAP

상태 확인.

---

# 37. XDP / AF_XDP mode 기록

XDP:

```text
native
generic
offload
```

중 실제 mode 기록.

AF_XDP:

```text
zero-copy
copy mode
```

실제 mode 기록.

지원되지 않는데 native/zero-copy라고 주장하지 않음.

---

# 38. 결과 figure

최소 추천:

- OWD CCDF
- Median / P99 / P99.9
- Observed Max per run
- packet loss
- deadline miss
- critical goodput
- per-core CPU utilization
- offered vs actual background throughput
- PTP offset

---

# 39. 가장 중요한 결과 비교

본 연구의 핵심 figure:

```text
C1 Normal RT
vs
C2 XDP_PASS
vs
C3 XDP + CPUMAP
```

특히:

```text
Network interference
Combined interference
```

조건에서:

- p99
- p99.9
- Observed Max
- loss

비교.

AF_XDP C4는 reference line으로 표시.

---

# 40. 결과 해석 예시

가상 결과:

```text
C1 Normal RT       p99.9 = 900 us
C2 XDP_PASS        p99.9 = 870 us
C3 XDP + CPUMAP    p99.9 = 350 us
C4 AF_XDP          p99.9 = 240 us
```

해석:

```text
C1 → C2 변화 작음
→ XDP hook 자체 overhead가 제한적

C2 → C3 큰 개선
→ CPUMAP packet-processing steering이 tail 감소에 기여

C3 → C4 추가 개선
→ kernel bypass가 추가적인 fast-path 이점 제공

하지만 C3는 kernel stack을 유지하면서도
C1보다 크게 개선
```

이 결과 구조가 본 연구의 가장 좋은 story.

---

# 41. 예상과 다르게 나와도 의미 있음

예:

```text
C2 ≈ C3
```

이면 CPUMAP steering 자체가 bottleneck 해결에 충분하지 않은 것.

그때:

```text
perf
ftrace
rtla
IRQ affinity
NAPI
NIC driver
```

로 원인을 분석.

C4까지 크게 안 좋아지면:

- NIC
- driver
- IRQ
- shared hardware
- sender scheduling
- clock sync

등 다른 bottleneck 가능성 검토.

---

# 42. Main run metadata

각 run마다 최소 저장:

```text
kernel version
PREEMPT_RT 여부
CPU governor/frequency
task affinity
SCHED policy/priority
IRQ affinity
irqbalance state
XDP mode
CPUMAP target CPU
AF_XDP copy/zero-copy
NIC driver/ring/offload/coalescing/MTU
critical PPS/payload/period
background offered rate
actual throughput
stress-ng parameters
PTP status
```

---

# 44. Observer effect 검증

RAM logging도 완전히 free는 아니므로 E0 pretest:

```text
Logging OFF
vs
Logging ON
```

동일 조건에서:

- Median
- P99
- P99.9
- Observed Max
- CPU

비교.

차이가 작으면 measurement harness overhead가 제한적이었다고 근거 있게 기술.

---

# 46. 최종 contribution framing

안전한 표현:

> PREEMPT_RT 기반 resource-constrained ARM Embedded Linux에서
> XDP + CPUMAP을 이용한 packet-processing CPU steering이
> 일반 Linux network stack 대비 CPU/network interference 상황의
> critical UDP tail latency와 결정성에 미치는 영향을 실험적으로 분석하였다.

결과가 좋다면:

> CPUMAP steering이 kernel stack을 유지하면서도
> critical UDP tail latency를 감소시키는 효과를 관찰하였다.

피해야 할 과장:

```text
hard real-time 보장
WCET 보장
실제 자동차 ECU에서 검증 완료
TSN 대체
AF_XDP보다 우수
```

---

# 47. 선행연구와의 차별점

가까운 mixed-criticality/SDV 연구들은:

- PREEMPT_RT
- XDP
- AF_XDP
- middleware scheduler
- dedicated NIC queue

등 여러 mechanism을 함께 사용하는 경우가 있음.

본 연구는 오히려:

```text
Normal
→ XDP_PASS
→ XDP + CPUMAP
```

을 분리하여

> XDP hook 자체와 CPUMAP packet-processing steering의 효과를
> resource-constrained ARM에서 직접 비교

하는 데 초점을 둠.

---

# 48. 논문의 핵심 story

```text
1. Vanilla baseline

2. PREEMPT_RT baseline

3. XDP_PASS로 XDP hook 비용 확인

4. CPUMAP으로 packet-processing CPU steering

5. C2 vs C3에서 본 mechanism의 효과 분석

6. AF_XDP는 kernel-bypass reference

7. perf/ftrace/rtla로 tail 원인 보조 분석
```

---

# 설계/실험 제안 종합 — 반드시 논문 분석 이후에만 적용

> 이 절은 위 A/A- 논문 분석에서 반복적으로 확인된 내용만 모아
> 현재 칠판/README 구조에 적용할 수 있는 제안을 정리한 것임.
>
> 아래 내용은 현재 Ground Truth를 덮어쓰지 않음.

## [KEEP] 현재 구조를 그대로 유지할 근거가 충분한 항목

```text
Single RX path
→ XDP classifier
→ AF_XDP / CPUMAP / XDP_PASS 3-path 구조

CPU3 = RT-critical 전용 core

PREEMPT_RT baseline

UDP 기반 latency experiment

CPU stress / network stress 분리

tail latency 중심 metric

PTP + software application timestamp OWD

RAM preallocation / memory locking / after-run dump
```

선행연구들은 특히 PREEMPT_RT, affinity/isolation, XDP/AF_XDP,
CPU/network interference, memory logging, tail metric의 필요성을 반복해서 지지함.

---

## [VERIFY] 현재 설계가 실제 target에서 성립하는지 확인할 항목

### 1. CPU3 isolation이 실제로 성립하는가?

`New Insights Into the Real-Time Performance of a Multicore Processor`의 교훈:

```text
task affinity
≠
complete isolation
```

따라서 CPU3에 RT task를 pinning했다고 끝내지 말고:

- IRQ가 CPU3으로 들어오는가?
- softirq/NAPI processing이 CPU3을 침범하는가?
- irqbalance가 CPU3에 interrupt를 다시 배치하는가?
- kernel/background work가 CPU3에 남는가?

확인 필요.

제안은 CPU3을 다른 core로 바꾸자는 것이 아님.
현재 CPU3 RT-core 설계를 실제 system에서 검증하자는 것임.

---

### 2. CPU/network interference를 따로 먼저 분석

선행논문에서 CPU stress와 network interference를 독립적으로 주는 방법이 반복적으로 사용됨.

따라서:

```text
Idle
CPU-only
Network-only
Combined
```

구조 권장.

Combined만 보면 원인이 섞이므로
CPU-only / Network-only 결과가 먼저 필요.

---

### 3. Main run과 diagnostic run 분리

Main:

```text
Custom OWD
mpstat
ethtool
PTP status
```

Diagnostic:

```text
perf
ftrace / trace-cmd
rtla osnoise
```

이유:

- tracing/profiling 자체 observer effect
- 선행연구에서도 telemetry overhead 문제 확인

---