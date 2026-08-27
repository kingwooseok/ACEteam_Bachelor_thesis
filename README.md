# Deterministic Multi-Path Packet Processing on Embedded Linux with XDP, AF_XDP, CPUMAP and PREEMPT_RT

## 1. 프로젝트 개요

본 프로젝트는 임베디드 Linux 환경에서 네트워크 패킷을 하나의 RX 경로로 수신한 뒤, XDP를 이용해 패킷의 특성에 따라 서로 다른 처리 경로로 분기하는 구조를 설계하고 성능을 분석한다.

핵심 아이디어는 모든 패킷을 하나의 방식으로 처리하지 않고, 다음 세 가지 경로를 하나의 시스템에서 공존시키는 것이다.

```text
                         Embedded NIC
                              |
                         Single RX path
                              |
                             XDP
                              |
              +---------------+---------------+
              |               |               |
              v               v               v
          AF_XDP           CPUMAP          XDP_PASS
              |               |               |
          Userspace       RT CPU          Normal CPU
          application    + PREEMPT_RT      Linux path
                              |
                        Linux network stack
```

본 연구에서 XDP는 패킷의 최초 분류 및 경로 선택 역할을 담당한다.

---

## 2. 연구 목적

본 연구의 목적은 단순히 XDP 또는 AF_XDP가 일반 Linux 네트워크 스택보다 빠르다는 것을 다시 검증하는 것이 아니다.

대신 하나의 임베디드 Linux 시스템에서 패킷의 처리 요구사항에 따라 적절한 execution path를 선택할 수 있도록 하고, 각 경로의 latency 및 workload tolerance를 정량적으로 비교한다.

핵심 연구 질문은 다음과 같다.

> 하나의 RX 경로로 유입되는 mixed traffic을 XDP에서 분류하여 AF_XDP userspace path, PREEMPT_RT 기반 isolated kernel path, 기존 Linux path로 선택적으로 분기하면, 높은 시스템 부하에서 latency-sensitive traffic의 tail latency와 deadline miss를 얼마나 줄일 수 있는가?

---

## 3. 세 가지 처리 경로

### 3.1 AF_XDP 경로: Ultra-low-latency userspace path

```text
NIC
 ↓
XDP
 ↓
XSKMAP
 ↓
AF_XDP socket
 ↓
UMEM / RX ring
 ↓
Userspace application
```

AF_XDP는 일반 Linux IP/TCP/UDP socket path를 우회하고 packet을 userspace의 UMEM으로 전달한다.

XDP 프로그램은 `XSKMAP`을 이용해 특정 패킷을 지정된 AF_XDP socket으로 redirect한다.

이 경로의 목적은 가능한 한 짧은 packet-to-userspace path를 제공하는 것이다.

### 3.2 CPUMAP + PREEMPT_RT 경로: Kernel functionality path

```text
NIC
 ↓
XDP
 ↓
CPUMAP
 ↓
RT CPU
 ↓
SKB
 ↓
Linux network stack
 ↓
Application
```

XDP에서 특정 패킷을 `CPUMAP`으로 지정된 CPU에 전달한다.

해당 CPU는 실시간 workload를 담당하도록 격리하고 PREEMPT_RT 커널을 사용한다.

이 경로의 목적은 일반 Linux networking stack을 유지하면서 latency-sensitive packet processing을 RT execution domain에서 수행하는 것이다.

AF_XDP처럼 모든 networking 기능을 userspace에서 다시 구현하지 않고 기존 kernel networking functionality를 유지할 수 있다는 것이 핵심적인 trade-off이다.

### 3.3 XDP_PASS 경로: Normal Linux path

```text
NIC
 ↓
XDP
 ↓
XDP_PASS
 ↓
Normal Linux networking path
```

별도의 low-latency isolation이나 kernel bypass가 필요하지 않은 일반 traffic은 기존 Linux networking path로 전달한다.

---

## 4. XDP가 하는 역할

XDP 프로그램은 NIC에서 패킷을 수신한 매우 초기 단계에서 실행된다.

본 프로젝트에서 XDP는 packet parser 및 dispatcher 역할을 수행한다.

예를 들어 다음과 같은 구조를 가진다.

```c
if (is_ultra_low_latency(packet)) {
    return bpf_redirect_map(&xsks_map, queue_id, 0);
}

if (is_rt_kernel_traffic(packet)) {
    return bpf_redirect_map(&cpu_map, rt_cpu, 0);
}

return XDP_PASS;
```

실제 조건은 연구에서 정의하는 traffic classification policy에 따라 결정한다.

XDP가 직접 packet을 처리하는 것이 아니라, **어느 execution path에서 처리할 것인지를 결정하는 것이 핵심**이다.

---

## 5. 주요 BPF Map

### XSKMAP

`XSKMAP`은 XDP packet을 특정 AF_XDP socket으로 redirect하기 위한 map이다.

```text
XDP
 ↓
XSKMAP
 ↓
AF_XDP socket
 ↓
Userspace
```

XSKMAP 자체가 packet data를 저장하는 것은 아니며, 특정 key와 AF_XDP socket의 연결 정보를 관리한다.

### CPUMAP

`CPUMAP`은 XDP frame을 지정한 CPU로 전달하기 위한 map이다.

```text
XDP
 ↓
CPUMAP[CPU 3]
 ↓
CPU 3
 ↓
SKB / Linux network stack
```

본 프로젝트에서는 RT 전용 CPU를 지정하여 latency-sensitive kernel traffic을 해당 CPU의 execution domain으로 steering한다.

---

## 6. CPU 구성

예를 들어 4-core embedded board에서는 다음과 같이 구성할 수 있다.

```text
CPU 0 ─┐
CPU 1 ─┼─ General-purpose processing
CPU 2 ─┘

CPU 3 ─── RT-critical processing
```

중요한 점은 PREEMPT_RT가 CPU 3에만 적용되는 것이 아니라 **커널 전체가 PREEMPT_RT 커널로 동작하고**, 그 위에서 CPU 3을 RT workload 전용 execution domain으로 구성한다는 것이다.

일반 RX/NAPI 처리는 CPU 0~2에 배치하고, XDP에서 RT-critical packet만 CPUMAP을 통해 CPU 3으로 전달한다.

실험 환경에서는 다음 요소를 명시적으로 제어한다.

- NIC IRQ affinity
- RPS 설정
- RT CPU isolation
- RT task CPU affinity
- RT task scheduling policy
- CPU frequency scaling
- 불필요한 background workload

CPUMAP과 CPU affinity/isolation은 동일한 기능이 아니다.

- CPU affinity / IRQ affinity: RX interrupt 및 일반 실행 context를 어느 CPU에 배치할지 제어
- CPU isolation: RT CPU에 일반 workload가 유입되는 것을 최소화
- CPUMAP: XDP가 특정 packet의 후속 처리를 어느 CPU로 보낼지 packet 단위로 결정

---

## 7. 왜 단일 RX path인가?

본 연구는 고급 NIC의 hardware flow steering이나 전용 RX queue isolation을 전제로 하지 않는다.

임베디드 Ethernet 환경에서는 NIC마다 hardware classification 및 queue steering 기능의 수준이 다르며, 반드시 PC용 고급 NIC와 동일한 기능을 제공한다고 가정할 수 없다.

따라서 실험의 핵심은 다음과 같다.

```text
NIC
 ↓
Single RX path
 ↓
XDP software classification
 ↓
Different execution paths
```

이를 통해 하드웨어가 traffic을 미리 완벽하게 분리해 주는 환경이 아니라, **software-defined packet-to-execution-path steering**을 평가한다.

---

## 8. 부하 실험

시스템 및 네트워크 부하를 증가시키면서 각 처리 경로의 latency behavior를 측정한다.

예시:

```text
0%
20%
40%
60%
80%
100%
```

부하는 실험 목적에 따라 CPU workload와 network packet workload를 별도로 설정할 수 있다.

중요한 것은 모든 조건에서 동일한 workload를 유지하고 processing path만 변경하는 것이다.

---

## 9. 측정 지표

평균 latency만으로는 실시간성을 판단하지 않는다.

주요 측정 지표:

### Average latency

평균적인 packet processing latency를 비교한다.

### Tail latency

- p99
- p99.9
- 필요 시 p99.99

높은 workload에서 발생하는 latency spike를 비교한다.

### Worst-case latency

측정 구간에서 관찰된 최대 latency를 확인한다.

### Jitter

latency 분포의 변동성을 분석한다.

### Deadline miss ratio

정해진 deadline을 초과한 packet의 비율을 측정한다.

예:

```text
Deadline = 1 ms

deadline miss ratio
= deadline을 초과한 packet 수 / 전체 packet 수
```

---

## 10. 실험 비교군

권장되는 기본 비교 구조는 다음과 같다.

### Case A — Vanilla Linux

```text
Normal Linux
+ Normal networking path
```

기본 baseline이다.

### Case B — PREEMPT_RT

```text
PREEMPT_RT
+ Normal networking path
```

RT kernel 자체의 영향을 측정한다.

### Case C — PREEMPT_RT + XDP/CPUMAP

```text
PREEMPT_RT
+ RT CPU isolation
+ XDP
+ CPUMAP
+ Normal kernel networking stack
```

본 연구의 RT-kernel packet path를 평가한다.

### Case D — AF_XDP

```text
XDP
+ XSKMAP
+ AF_XDP
+ Userspace processing
```

kernel bypass path의 latency 특성을 비교한다.

### Case E — 제안 architecture

```text
                    XDP
                     |
          +----------+----------+
          |          |          |
      AF_XDP      CPUMAP     XDP_PASS
          |          |          |
      userspace   RT CPU    normal path
```

패킷 특성에 따라 서로 다른 execution path를 사용하는 전체 구조를 평가한다.

---

## 11. 연구의 핵심 관점

본 연구의 핵심은 **“임베디드 Linux에서 deterministic한 packet processing을 위해 서로 다른 요구사항을 가진 traffic을 적절한 execution path로 분기하는 아키텍처를 제시하는 것”**이다.

```text
                  Packet
                     |
                     v
                XDP classifier
                     |
       +-------------+-------------+
       |             |             |
       v             v             v
   AF_XDP         CPUMAP        XDP_PASS
       |             |             |
       v             v             v
  Userspace       RT CPU       Normal CPU
                  + RT
                  kernel
```

따라서 각 경로는 다음과 같은 목적을 가진다.

| Path | 목적 |
|---|---|
| AF_XDP | 최소 packet-to-userspace latency |
| CPUMAP + PREEMPT_RT | kernel networking 기능 유지 + RT execution domain |
| XDP_PASS | 일반적인 Linux networking compatibility |

최종적으로 workload 증가에 따라 세 경로의 latency, tail latency, jitter, deadline miss 특성을 비교하여 **임베디드 환경에서 multi-path packet processing architecture의 실효성과 trade-off를 분석**한다.

---

## 12. 구현 환경

권장 개발 도구:

```text
clang / LLVM
libbpf
bpftool
iproute2
bpftrace
```

역할:

- `clang/LLVM`: XDP C 코드를 eBPF bytecode로 컴파일
- `libbpf`: BPF program/map loading 및 관리
- `bpftool`: BPF program/map/link 확인 및 디버깅
- `iproute2`: 네트워크 인터페이스 및 XDP 설정
- `bpftrace`: scheduler, IRQ, latency 등의 kernel tracing 및 분석

BCC와 eBPF-Go는 현재 구조의 필수 구성요소가 아니다.

---

## 13. 권장 구현 순서

1. `XDP_PASS`만 수행하는 최소 XDP 프로그램 구현
2. Ethernet/IP/UDP 등 기본 packet parsing 구현
3. `CPUMAP`을 이용한 CPU 3 steering 구현
4. CPU isolation 및 IRQ/RPS configuration 적용
5. RT CPU에서 kernel path 동작 확인
6. `XSKMAP + AF_XDP` userspace path 구현
7. 세 경로를 하나의 XDP dispatcher로 통합
8. workload를 0~100%까지 증가시키며 latency 측정
9. p99/p99.9/worst-case/deadline miss 분석

---

## 14. 기대되는 결과 형태

이 연구에서 중요한 결과는 하나의 path가 모든 조건에서 우월하다는 것이 아니다.

예상되는 분석 형태는 다음과 같다.

```text
Low load
→ 세 path 간 차이가 작을 수 있음

High load
→ Normal path의 latency tail 증가 가능
→ RT-kernel path의 interference 완화 여부 확인
→ AF_XDP의 kernel-stack bypass 효과 확인

Extreme load
→ 각 path가 어느 수준까지 deadline을 유지하는지 비교
```

실제 결과는 실험을 수행하기 전에는 단정하지 않는다.

---
