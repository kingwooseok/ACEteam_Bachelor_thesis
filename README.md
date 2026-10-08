# Deterministic Mixed-Criticality Packet Processing on Embedded PREEMPT_RT Linux with Driver-Level BPF

Raspberry Pi 5의 PREEMPT_RT Linux에서 macb Native XDP를 분류기로 사용하여,
트래픽의 중요도와 커널 네트워크 기능 요구에 따라 PASS·CPUMAP·AF_XDP를 함께
운용하는 혼합 중요도 패킷 처리 아키텍처를 연구한다. 기존 Linux의 범용성과
호환성을 유지하면서 필요한 트래픽에 CPU 격리와 스택 우회를 선택적으로 적용해,
제한된 자원에서 중요 트래픽의 결정성을 확보하는 것이 목표다.
드라이버 패치, 송수신 프로그램, 시계 보정 및 결과 분석 도구를 포함한다.

## 연구 목표

세 경로의 역할을 함께 살리는 것이 연구의 중심이다. 개별 경로 측정으로 각
구성요소의 비용과 특성을 파악하고, 동시 혼합 트래픽 실험으로 분류·격리가
중요도별 tail latency와 deadline miss, 일반 트래픽 처리량 및 CPU 사용률에
미치는 영향을 평가한다. 같은 총 부하의 all-PASS 구성과 비교해 경로 분리의 효과를 분석한다.

## 경로 역할과 구현

프로젝트 송신기가 만드는 실험 패킷은 다음 경로로 전달된다.
포트·대상 CPU·인터페이스 기본값은 [BPF/config.h](BPF/config.h)에서 설정한다.

| 경로와 역할 | UDP 목적지 포트 | 현재 수신 경로 |
| --- | --- | --- |
| PASS: 일반 트래픽의 범용 처리와 기존 네트워크 기능 유지 | 9000 | XDP_PASS → Linux UDP socket |
| CPUMAP: 커널 기능이 필요한 중요 트래픽을 전용 CPU로 분리 | 9001 | CPUMAP → CPU 3 → Linux UDP socket |
| AF_XDP: 스택 기능이 불필요한 중요 트래픽의 저지연 처리 | 9002 | AF_XDP → 사용자 프로그램 (`XDP_COPY` 모드) |

현재 실험에서는 UDP 포트로 트래픽 종류를 지정한다. CPUMAP의 전용 CPU 격리는
CPU affinity·IRQ·스케줄링 설정과 함께 구성한다.
그 외 트래픽은 XDP_PASS로 전달하며, AF_XDP 소켓이 없는 RX queue도 PASS로 처리한다.

- [BPF/](BPF/): XDP 분류·기록, loader, AF_XDP 수신기.
- [udp_socket/](udp_socket/): UDP 송수신기와 PHC 시계 보정 도구.
- [monitor/](monitor/): 부하 생성, 시스템 모니터링, 실행 환경 기록.
- [analysis/](analysis/): 패킷별 기록 결합과 반복 실험 집계.
- [experiment/](experiment/): 두 파이에서 직접 실행하는 C 시험 제어기와 설정 파일.
- [patches/macb-xdp-20260910/](patches/macb-xdp-20260910/): macb 포팅 및 RT 커널 설정 패치.

2026-09-10에 세 경로의 외부 Ethernet 수신을 검증했다.
결과와 후속 성능 측정 절차는 [측정 돌리는 법](측정돌리는법.md)에 있다.

## 설치와 빌드

수신기는 위 패치를 적용한 커널로 부팅하고, `/sys/kernel/btf/vmlinux`를 제공해야 한다.
아래 명령은 저장소 최상위에서 실행한다.

```bash
sudo apt install build-essential clang llvm bpftool libbpf-dev libxdp-dev pkg-config python3
make
```

`make`는 XDP loader, AF_XDP 수신기, UDP 송수신기, PHC 보정 도구와 C 시험 제어기를 빌드한다.
커널을 바꾼 뒤에는 새 커널의 BTF에 맞춰 다시 빌드한다.
UDP 도구만 빌드하려면 `make udp`, 오프라인 회귀 검사는 `make test`,
생성 파일 정리는 `make clean`을 사용한다.

CPU·메모리·I/O 부하에는 `stress-ng`, 네트워크 부하에는 `iperf3`,
CPU·NIC 관측에는 `sysstat` 패키지의 `mpstat`·`sar`를 사용한다.
Python은 개발용 회귀 검사와 측정 종료 후 분석에 사용한다.

## 최소 실행: AF_XDP 수신

수신기의 인터페이스는 `eth0`, RX queue는 `0`인 예시다.
실행마다 새 결과 디렉터리를 사용한다.

수신기 터미널 1에서 loader를 실행하고 유지한다.

```bash
mountpoint -q /sys/fs/bpf || sudo mount -t bpf bpf /sys/fs/bpf
mkdir -p results/run01
sudo ./BPF/xdp_loader --output-dir results/run01 --prefix xdp- eth0
```

수신기 터미널 2에서 AF_XDP 수신기를 실행한다.

```bash
sudo ./BPF/afxdp_recv --output results/run01/xsk-user-records.csv eth0 0
```

Ethernet으로 연결된 별도 Linux 송신기에서 실행한다.
`192.168.10.10`은 수신기의 IPv4 주소로 바꾼다.

```bash
make udp
./udp_socket/bin/sender --port 9002 --flow-id 3 --count 1000 192.168.10.10
```

PASS·CPUMAP용 UDP 수신기 실행은 [UDP 사용법](udp_socket/README.md)을 참고한다.
동시에 보내는 흐름에는 서로 다른 `--flow-id`를 사용한다.
정식 지연 측정에서는 실행 환경 기록과 측정 전후 PHC 보정을 함께 수집한다.

## 종료와 복구

송신을 마친 뒤 **AF_XDP 수신기를 먼저 `Ctrl+C`로 종료하고, loader를 종료한다.**
수신기는 사용자 기록 CSV를 저장하며, loader는 XDP 해제·남은 처리 완료·CSV 저장·
기존 하드웨어 timestamp 설정 복구·map pin 정리를 수행한다.
위 예시의 loader 출력은 `xdp-ingress_records.csv`, `xdp-cpumap_records.csv`,
`xdp-stats.csv`다.

기존 XDP 프로그램, `/sys/fs/bpf/ace_xdp`의 남은 pin, 기존 결과 파일은 덮어쓰지 않는다.
비정상 종료로 pin이 남으면 수신기를 먼저 종료하고 새 디렉터리에 복구한다.

```bash
mkdir -p results/run01-recovery
sudo ./BPF/xdp_loader --recover --cleanup-pins \
  --output-dir results/run01-recovery eth0
```

복구·내보내기가 성공하면 pin을 제거한다. `--cleanup-pins`를 생략하면 pin을 보존한다.
종료나 복구가 실패한 경우에도 pin을 보존하므로, 직접 지우기 전에 오류를 해결하고 복구를 재시도한다.

## 주요 문서

- [커널·드라이버 패치 적용](patches/macb-xdp-20260910/README.md)
- [초기 드라이버 포팅 기록](드라이버%20포팅한%20그%20모든것을%20담아.md)
- [측정 순서와 수신 검증 결과](측정돌리는법.md)
- [UDP 송수신·시계 보정](udp_socket/README.md)
- [부하 생성과 모니터링](monitor/README.md)
- [기록 결합과 반복 실험 분석](analysis/README.md)
- [설정 파일 기반 직접 실행·결과 회수](experiment/README.md)

## 시험 자동화

두 파이에서 같은 설정 파일을 사용하고, 수신 파이와 송신 파이에서 각각
`experiment/ace-run receiver --config FILE`과
`experiment/ace-run sender --config FILE`을 직접 실행한다.
수신 준비, 공통 송신 시작, 부하, 종료 후 drain·flush와 반복 실행을 C 제어기가
관리한다. 파이 사이의 단계 제어는 준비와 종료 시점에만 이루어지며,
측정 중 제어기는 대기 상태를 유지한다. VMware나 SSH는 측정 실행에 필요하지 않다.

실험 조건은 [config.example.conf](experiment/config.example.conf)의 빈 항목에
넣는다. 기존 패킷 크기·점보 드롭·세 경로 정책을 유지하며,
`ace-run plan --config FILE`로 실행 조건과 기록 용량을 먼저 볼 수 있다.
측정 후 양쪽 결과를 VMware 등 분석용 컴퓨터로 옮기고,
`analysis/analyze_experiment.py collect`로 합친 뒤 HTML 보고서를 생성한다.
빠른 기능 검증용 `socket-smoke.conf`는 UDP loopback 실행 예제다.
