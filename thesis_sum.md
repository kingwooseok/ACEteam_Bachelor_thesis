# 논문 내용 요약

## 1. Towards Timing Isolation for Mixed-Criticality Communication in Software-Defined Vehicles

### 연구 목적:
중앙집중형 Linux 차량 컴퓨터에서 안전 중요 통신과 일반 통신이 Linux 네트워크 스택을 함께 사용할 때 발생하는 지연·지터 간섭을 줄이고, 실시간 트래픽의 시간적 격리를 확보하는 것이 목적이다.

### 핵심 아이디어/구조:
세 계층을 함께 격리한다. 미들웨어에는 고정 우선순위 비선점 스케줄러와 전용 executor를 두고, 네트워크 계층에서는 AF_XDP로 실시간 패킷을 커널 네트워크 스택을 우회해 애플리케이션 메모리로 전달한다. 하드웨어에서는 NIC 큐 하나를 실시간 트래픽 전용으로 할당한다. DDS 통신으로 이를 구현했다.

### 실험·평가:
두 Linux 시스템을 Intel I226 2.5Gbps NIC로 연결하고, DDS 패킷 크기를 64·512·1024B로 바꾸며 RTT와 단방향 지연을 측정했다. 간섭 조건에서는 50개의 양방향 UDP 흐름으로 2.5Gbps 회선을 포화시켰다.

### 주요 결과:
기준 구성은 간섭 시 최대 RTT가 약 3.15~3.34ms까지 증가했다. 제안 구조에서는 모든 패킷 크기에서 최대 RTT가 약 195~197µs로 유지됐다. 1024B 기준 RTT 중앙값도 간섭 상태에서 기준 구조 2161.61µs, 제안 구조 158.64µs였다. 512B 간섭 실험의 RTT 분산은 148,494.1에서 50.03으로 크게 감소했다. 무간섭 상태에서는 평균·중앙 지연 개선이 작거나 일부 최솟값이 나빠졌지만 최댓값과 변동성이 개선됐다.

### 한계/의미:
각 격리 기법의 개별 기여도는 분리해 평가하지 않았다. 그럼에도 XDP 하나만 적용하는 수준을 넘어 미들웨어·커널 경로·NIC 큐를 함께 분리해야 회선 포화 상황에서도 예측 가능한 지연을 얻을 수 있음을 보여준다.

## 2. XvSomeIP: A High-Performance In-Vehicle Communication Middleware Based on XDP

### 연구 목적:
AUTOSAR Adaptive 환경에서 사용하는 vSomeIP의 커널 네트워크 스택 처리 비용, 데이터 복사, 컨텍스트 전환과 스레드 경쟁을 줄이는 것이 목적이다.

### 핵심 아이디어/구조:
vSomeIP에 XDP/AF_XDP 기반 데이터 경로를 결합한 XvSomeIP을 제안한다. 통신 hot path에서 Linux 네트워크 스택을 우회하고, 사용자 공간에서 Ethernet 프레임과 UDP 처리를 담당한다. zero-copy, CPU affinity, 코어별 전용 처리 스레드, 잠금 경쟁이 적은 메모리 구조도 사용한다.

### 실험·평가:
Linux 5.4.0-73, Intel i7-12700F, 16GB RAM 시스템 두 대를 스위치 없이 직접 연결했다. 16~1024B 메시지에 대해 100만 회 request/response RTT를 측정하고, 초당 처리량과 Fire/Forget 방식의 최대 송수신 능력을 vSomeIP과 비교했다. 각 설정은 최소 3회 반복했다.

### 주요 결과:
1024B에서 RTT가 vSomeIP보다 약 30% 낮았다. 처리량은 조건에 따라 최대 5배 이상, 최대 송수신 능력은 최대 4배가량 향상됐다. 논문은 전체적으로 처리량 및 송수신 성능이 약 4~5배 개선됐다고 결론 내린다.

### 한계/의미:
평균 RTT 중심의 평가이며 최악 지연, 혼잡 시 tail latency, 패킷 손실과 실제 차량 부하에 대한 분석은 제한적이다. 따라서 hard real-time 보장보다는 SOME/IP의 soft real-time 성능을 높이는 구조로 해석하는 것이 적절하다.

## 3. Evaluating AMP for Automotive Gateways

### 연구 목적:
이기종 자동차용 SoC에서 모든 처리를 Linux 애플리케이션 코어가 담당하는 AP-centric 구조와, 안전 중요 I/O를 실시간 코어로 분리하는 AMP 구조를 비교한다.

### 핵심 아이디어/구조:
NXP S32G3의 Cortex-A53에서는 Linux/PREEMPT_RT와 Ethernet 전달을 수행하고, AMP 구성에서는 Cortex-M7이 CAN-FD 수신과 즉각적인 GPIO 반응을 담당한다. 이후 IPCF를 통해 Linux 측으로 프레임을 넘겨 UDP로 전달한다.

### 실험·평가:
1·5·10ms 주기 제어 트래픽, 혼합 진단 트래픽, 3000 PPS flood를 사용했다. 소프트웨어 loopback 지연과 오실로스코프 기반 CAN 수신→GPIO 동작 지연을 별도로 측정했다.

### 주요 결과:
무부하 1ms 제어 흐름의 loopback 평균은 AP-centric 171.3µs, AMP 186.5µs로 IPCF 단계가 있는 AMP가 조금 느렸다. burst에서도 1ms 제어 흐름의 P99는 양쪽 모두 313µs 미만이었지만, 진단·flood 트래픽은 6~16ms 수준의 긴 꼬리를 보였다. 반면 CAN→GPIO 평균은 AP-centric 약 53.9µs에서 AMP 33.62µs로 약 37.5% 감소했으며 표준편차도 약 727~777ns에서 241~257ns로 줄었다.

### 한계/의미:
AMP는 안전 중요 로컬 반응을 Linux 경로로부터 분리하는 데 효과적이지만, Ethernet까지 이어지는 종단 전달 지연을 자체적으로 제한하지는 못한다. 한 SoC, 한 커널/BSP, 한 burst 유형만 사용했고 GPIO 측정도 조건당 100회다. 종단 deadline을 위해서는 CAN 부하 제어, 우선순위 큐, TSN 등의 추가 기법이 필요하다.

## 4. Raspberry Pi 5 PREEMPT_RT Assessment

### 연구 목적:
Raspberry Pi 5에서 PREEMPT_RT가 높은 CPU·메모리·네트워크 부하 중에도 실시간 스케줄링의 결정성을 향상시키는지 예비 평가한다.

### 핵심 아이디어/구조:
일반 Linux 커널과 PREEMPT_RT 커널에서 동일한 Cyclictest를 실행하고, 여러 stressor를 동시에 가동해 스케줄링 지연 분포를 비교한다.

### 실험·평가:
네 개의 실시간 태스크를 100µs마다 한 시간 동안 깨웠다. 이와 동시에 CPU 사용률 100%, RAM 약 70%, 100개 네트워크 클라이언트의 iperf3 트래픽을 발생시켰다.

### 주요 결과:
일반 커널의 평균·최대·표준편차는 각각 14.69µs, 36,802µs, 122.08µs였다. PREEMPT_RT에서는 5.91µs, 약 124~125µs, 3.25µs로 감소했다. 최대 지연은 약 294배, 평균은 약 2.49배 개선됐다. 핵심 효과는 평균 속도보다 극단적인 outlier와 변동성 감소다.

### 한계/의미:
커널을 적극적으로 튜닝하지 않은 예비 결과이고, 종단 응답시간이나 실제 제어 애플리케이션 deadline은 측정하지 않았다. 저가형 Raspberry Pi에서도 PREEMPT_RT가 결정성을 크게 개선하지만, 관측 최대 125µs가 hard real-time 보장을 뜻하지는 않는다.

## 5. New Insights Into the Real-Time Performance of a Multicore Processor

### 연구 목적:
멀티코어에서 C-state, 하이퍼스레딩, 코어 격리 같은 배치 설정이 Xenomai와 RT-Preempt의 실시간 성능에 어떤 영향을 주는지 체계적으로 밝힌다.

### 핵심 아이디어/구조:
여러 멀티코어 배치를 구성해 scheduling latency를 비교하고, 차이가 작은 Xenomai 구성에는 변형된 카이제곱 적합도 검정을 적용해 최적 배치를 고른다. 이후 다중 주기 태스크의 주기성·응답시간으로 실제 실행 가능성을 검증한다.

### 실험·평가:
Intel 기반 멀티코어 임베디드 시스템에서 Cyclictest를 사용했다. CPU·메모리 stress-ng와 100개 소켓 클라이언트의 네트워크 부하를 추가했으며, 실행시간/주기가 3/10ms, 5/20ms, 10/40ms인 세 우선순위 태스크의 WCRT를 측정했다.

### 주요 결과:
하이퍼스레딩 비활성화가 두 실시간 Linux 모두에서 가장 중요한 개선 요소였다. 최적 설정에서는 RT-Preempt가 Xenomai보다 scheduling latency와 주기성이 좋았고, Xenomai는 응답시간이 약간 짧았으나 차이는 10µs 미만이었다. 부하가 있어도 뚜렷한 성능 저하는 없었고, 논문은 양쪽 모두 최소 100µs 주기의 태스크를 처리할 수 있다고 판단했다.

### 한계/의미:
결과가 특정 Intel 보드와 당시 커널·Xenomai 버전에 의존하며, 다른 아키텍처로 일반화하려면 추가 검증이 필요하다. 단순히 PREEMPT_RT를 설치하는 것보다 SMT·전력관리·IRQ·코어 배치 설정이 최악 지연을 좌우한다는 점이 핵심이다.

## 6. OPC UA Safety Experimental Evaluation

### 연구 목적:
OPC UA Safety가 자원 제약이 있는 임베디드 실시간 플랫폼에서도 안정적이고 예측 가능한 안전 통신을 제공하는지 평가한다.

### 핵심 아이디어/구조:
Xilinx ZCU102 두 대와 PREEMPT_RT 기반 PetaLinux로 OPC UA Safety Publisher/Subscriber 시험환경을 구성했다. 핵심 지표는 안전 요청·응답 전체를 포함하는 UAS ReqRes RTT이며, CycleTime·Publishing Interval·SPDU payload의 영향을 분리해 본다.

### 실험·평가:
각 설정에서 100만 개 RTT 표본을 수집하고 CCDF 및 90·99백분위와 tail을 분석했다. payload는 10·100·1000B, CycleTime은 1~32ms 범위로 바꿨으며 CPU·RAM도 측정했다. 반복 실험 간에는 두 보드를 재부팅했다.

### 주요 결과:
CycleTime이 RTT 분포의 가장 큰 결정 요소였다. 1·2·4ms에서는 분포가 넓고 간헐적 peak가 있었지만, 8·16·32ms에서는 값이 더 조밀했다. Publishing Interval은 주로 극단 꼬리의 발생 확률에 영향을 줬고 payload 영향은 작았다. PREEMPT_RT는 OS 지터를 수백 µs에서 약 35µs 이하로 낮췄다. CPU 평균은 12% 미만, RAM은 약 3.3~3.5%였다.

### 한계/의미:
1:1 단방향 통신만 평가했고, 다수 publisher/subscriber나 복잡한 DCS 토폴로지는 다루지 않았다. 재부팅하지 않으면 메모리 drift와 RTT 악화가 발생해 1ms 설정의 90백분위가 약 3.2ms에서 6ms 이상으로 증가했다. 이는 프로토콜 자체뿐 아니라 장시간 상태 관리와 시험 초기화가 결과에 큰 영향을 준다는 의미다.

## 7. Composable Distributed Real-Time Systems with Deterministic Network Channels

### 연구 목적:
각 노드의 실시간 계산뿐 아니라 노드 사이 통신도 시간적으로 합성 가능한 분산 실시간 시스템을 설계하는 것이 목적이다.

### 핵심 아이디어/구조:
Time Flow Graph와 Timed C에 Deterministic Network Channel을 추가한다. TSN의 Stream Reservation Protocol과 AVB Class A/B, PTP 시간 동기화를 이용해 채널의 최대 전달 지연과 분산 태스크의 동시 wake-up을 제어한다.

### 실험·평가:
Linux 및 PREEMPT_RT 시스템에서 50Hz 스트림을 전송했다. CPU·I/O 부하와 최대 약 830Mbps의 주기적 교차 트래픽을 주고, PTP 정확도, kernel wake-up, 종단 지연, 패킷 손실, 두 호스트의 상대 wake-up 오차를 측정했다.

### 주요 결과:
PREEMPT_RT는 Cyclictest 최대 wake-up 지연을 3092µs에서 16µs로 낮췄다. 네트워크 보호가 없으면 혼잡 중 패킷 손실 48.3%, 최대 E2E 지연 4196.9µs였지만 SRP 적용 후 손실 0%, 최대 184.3µs, 평균 58.9µs가 됐다. 두 호스트의 실행 시점은 부하와 간섭 중에도 최대 15.3µs 이내로 동기화됐다. PTP 오차는 최대 29ns로 스케줄러 오차보다 훨씬 작았다.

### 한계/의미:
평가한 AVB Class A의 표준 지연 상한은 2ms이므로, 이 구조의 합성 가능성도 2ms 지연을 허용하는 시스템에 한정된다. 100µs급 보장이 가능한 Time-Aware Shaper는 장비 부족으로 평가하지 못했다. Linux 네트워크 스택의 지터를 줄이기 위한 XDP 적용을 후속 연구로 제안한다.

## 8. Startup Delay Performance Evaluation for SOME/IP

### 연구 목적:
차량 ECU가 부팅된 뒤 최초 SOME/IP-SD Offer를 보내기까지의 startup delay가 Linux 저수준 네트워크 API 선택에 따라 얼마나 달라지는지 분석한다.

### 핵심 아이디어/구조:
Ethernet 프레임을 직접 구성해 `AF_PACKET` Raw Socket으로 보내는 구현과 libpcap 기반 구현을 비교한다. Raw Socket 구현에는 패킷 template/cache, `SCHED_FIFO`, CPU affinity, 메모리 잠금을 적용했다.

### 실험·평가:
NXP S32G 임베디드 Linux에서 애플리케이션 시작부터 최초 Offer 송신까지의 시간을 오실로스코프로 측정했다. 본문은 50회 측정 결과를 설명하며 그림 설명에는 100개 trace가 언급된다.

### 주요 결과:
Raw Socket은 약 13~15ms로 좁고 안정적인 범위를 보였다. PCAP은 약 150~235ms로 10~20배가량 느리고 변동도 컸다. 차이의 주원인은 `pcap_activate`의 버퍼·필터 초기화, 추가 추상화 계층, 해당 구현에서 사용한 여러 번의 `fork()`로 분석됐다.

### 한계/의미:
Raw Socket의 우위에는 API 자체뿐 아니라 구현별 최적화와 프로세스 구조의 차이도 포함되어 있어 모든 libpcap 구현에 그대로 일반화할 수 없다. 실차 수준의 가변 부하도 재현하지 않았다. 빠른 서비스 가용성이 중요하면 Raw Socket이 유리하지만, 수동 헤더·체크섬 처리와 Linux 종속성이라는 비용이 따른다.

## 9. Latency-Aware Industrial CPS for Connected Mobility

### 연구 목적:
대형 차량 종방향 제어에서 제어 품질뿐 아니라 100ms ECU deadline, tail latency, 메모리 사용량까지 함께 비교해 실제 배치에 적합한 제어기를 선택한다.

### 핵심 아이디어/구조:
P, PID, acados 기반 NMPC, PyTorch MPC, 사전 계산된 Memo-LUT를 비교한다. 계산 성능이 좋은 온라인 NMPC와 deadline을 확실히 지키는 LUT fallback을 결합한 하이브리드 구조를 제안한다.

### 실험·평가:
300개 경로, 총 30만 step을 x86-64에서 시험하고, Raspberry Pi 4B HIL 환경에서는 `SCHED_FIFO`와 `SCHED_DEADLINE`, 코어 고정 조건으로 설정당 1만 step을 검증했다. 지연 p50/p95/관측 WCET, deadline miss, 에너지, 속도 RMSE, 메모리를 측정했다.

### 주요 결과:
데스크톱에서는 모두 100ms를 만족했다. acados-NMPC는 중앙값 6.395ms, p95 7.038ms, WCET 7.623ms였고 Memo-LUT는 각각 0.042·0.052·0.066ms였다. 에너지 중앙값은 acados가 33.111kWh로 가장 낮았고, Memo-LUT는 RMSE 0.105m/s로 가장 정확했지만 에너지는 53.035kWh였다. Raspberry Pi에서 PID와 Memo-LUT는 WCET 1ms 미만, miss 0회였지만 임베디드 acados는 p95 약 26.9ms, WCET 약 195ms, 10,000회 중 1회 miss를 기록했다.

### 한계/의미:
WCET는 분석적 상한이 아니라 관측 최댓값이다. 특정 데스크톱과 Raspberry Pi 4B 결과이고 기능 안전 인증을 직접 입증하지 않는다. 평균·p95만 보면 NMPC가 충분해 보여도 희귀한 195ms outlier 때문에 안전 중요 시스템에서는 빠른 LUT fallback이 필요하다는 점을 보여준다.

## 10. eBPF-XDP Hardware-Based Network Slicing Architecture

### 연구 목적:
6G fronthaul·midhaul·backhaul 및 Industry 4.0 환경에서 테넌트별 트래픽을 분리하면서 최대 25Gbps의 처리량, 낮은 지연, 실시간 모니터링을 제공한다.

### 핵심 아이디어/구조:
Agilio CX SmartNIC에서 eBPF/XDP 프로그램과 map을 하드웨어 offload해 flow를 식별·제어하고, AF_XDP socket으로 사용자 애플리케이션에 전달한다. 큐·socket마다 CPU와 IRQ affinity를 전용 할당해 slice 사이 간섭을 줄인다. AF_XDP를 지원하도록 NFP 드라이버도 수정했다.

### 실험·평가:
132~1500B 패킷, 1·2·4·8·16개 socket/CPU, 최대 131,072개 제어 규칙으로 구성했다. 25Gbps 또는 최대 20Mpps 트래픽에서 CPU 사용률, 손실률, 수신 PPS와 RTT를 측정했고 각 실험을 5회 반복했다.

### 주요 결과:
socket 1개에서는 CPU 약 70%였으나 16개에서는 10% 미만이었고, 규칙 수가 131,072개로 늘어도 부하는 거의 일정했다. 132B 패킷은 1개 socket에서 약 70% 손실이 났지만 4개 이상에서는 거의 0%가 됐다. socket당 무손실 처리량은 약 5Mpps였다. 132B·25Gbps 조건의 RTT는 약 0.13ms였고, 1500B에서는 사용자 공간 처리까지 포함해 평균 약 0.55ms였다.

### 한계/의미:
작은 패킷의 0.13ms RTT는 0.1ms 요구에 가깝지만 엄밀히 그보다 크며, 큰 패킷·초저지연 조건에서는 요구를 충족하지 못할 수 있다. 거리와 복잡한 토폴로지 영향도 평가하지 않았다. SmartNIC offload, 다중 큐, AF_XDP를 결합하면 CPU 격리와 높은 처리량을 동시에 얻을 수 있음을 보여준다.

## 11. In-Kernel Packet Sanitization and Normalization Using eBPF

### 연구 목적:
비정상 TCP/UDP 헤더를 사용자 공간으로 넘기기 전에 커널 진입 초기에 검사·수정·폐기하면서도 낮고 예측 가능한 비용을 유지하는 패킷 sanitizer를 구현한다.

### 핵심 아이디어/구조:
XDP/eBPF 데이터 플레인과 Go 제어 플레인을 결합한다. 미리 직렬화한 규칙 descriptor, eBPF map, bounded loop와 tail call을 사용해 verifier 제약 안에서 최대 64개 TCP·UDP 규칙을 처리한다. TCP flag 정리, window/MSS 제한, timestamp 제거, SYN payload 제거 등의 stateless normalization을 지원한다.

### 실험·평가:
AMD Ryzen 5 PRO 5650U, 8GB RAM, Linux 6.8, JIT 활성화, generic XDP 환경에서 1~18개 규칙과 1~500kpps의 혼합 합성 트래픽을 시험했다. 동일한 TCP window 수정 기능을 Netfilter NFQUEUE 사용자 공간 구현과도 비교했다.

### 주요 결과:
전역 평균 처리비용은 규칙 1개에서 대략 195~223ns, 18개에서 약 374~400ns였다. 트래픽 속도에 따른 변동계수는 평균 1.2% 미만으로, 부하보다 실행된 규칙의 수와 복잡도가 지연을 결정했다. 개별 규칙은 대략 76~278ns 범위였고 no-op 규칙 영향은 작았다. 비교 시험에서 NFQUEUE는 약 43kpps, eBPF/XDP는 약 220kpps로 XDP가 약 5배 높은 처리율을 보였다.

### 한계/의미:
패킷 생성기와 sanitizer가 같은 호스트에 있었고, native/driver XDP가 아닌 generic mode를 사용했다. IPv4 및 stateless TCP/UDP 헤더 처리로 범위가 제한되며 고정 크기 규칙 배열은 확장성에 제약을 준다. 결과는 XDP가 단순 drop뿐 아니라 패킷 정규화도 수백 ns의 비교적 일정한 비용으로 수행할 수 있음을 보여주지만, 실제 악성 트래픽과 다양한 NIC에서 추가 검증이 필요하다.