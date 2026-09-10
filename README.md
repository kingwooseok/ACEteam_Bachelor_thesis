# Deterministic Mixed-Criticality Packet Processing on Embedded PREEMPT_RT Linux with Driver-Level BPF

초기 macb 포팅부터 현재 드라이버 변경 및 RT 커널 설정을 재현하는 패치는
[포팅 패치 묶음과 적용 안내](patches/macb-xdp-20260910/README.md)에 있다.

실물 수신 검증 결과와 개별 경로·동시 부하 측정의 진행 순서는
[측정 돌리는 법](측정돌리는법.md)에 정리했다.

### 코드를 처음 읽을 때

`드라이버 포팅한 그 모든것을 담아.md`는 초기 포팅의 배경과 당시 검증 기록이다.
그 뒤 추가한 측정 기능은 아래 순서로 읽으면 된다. 코드의 한국어 주석은
주로 처리 순서, 버퍼를 넘기는 이유, 각 timestamp가 의미하는 지점을 설명한다.

1. [`include/ace_packet_abi.h`](include/ace_packet_abi.h),
   [`BPF/metadata.h`](BPF/metadata.h): 송신되는 패킷 ID와 수신 장치 내부 metadata의 차이.
2. [`BPF/xdp_kern.c`](BPF/xdp_kern.c): 최초 XDP 측정 → 포트별 분류 →
   PASS/CPUMAP/AF_XDP 전달, 목적 CPU에서의 두 번째 관찰.
3. [`BPF/xdp_loader.c`](BPF/xdp_loader.c): 실행 준비, map 공유, 장치 attach와
   HW timestamp 설정 순서, 종료 후 CSV 저장.
4. [`udp_socket/src/receiver.c`](udp_socket/src/receiver.c),
   [`BPF/afxdp_recv.c`](BPF/afxdp_recv.c): socket과 UMEM 수신 방식,
   사용자 수신 시각을 찍는 위치. 송신 주기는 `udp_socket/src/sender.c`에서 읽는다.
5. [`udp_socket/src/phc_calibrate.c`](udp_socket/src/phc_calibrate.c) →
   [`analysis/join_results.py`](analysis/join_results.py) →
   [`analysis/aggregate_runs.py`](analysis/aggregate_runs.py): 시계 기준 맞추기,
   같은 패킷의 기록 합치기, 반복 실험 결과 집계.

드라이버 쪽 핵심은 `macb_main.c`의 `gem_xdp_run()`과 `gem_rx()`,
`macb_ptp.c`의 `gem_ptp_rxstamp_ns()`다. 해당 한국어 설명 주석도
패치 묶음에 들어 있으며, 적용한 커널 소스에서 직접 읽을 수 있다.

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

### UDP socket baseline

C0(Vanilla Linux), C1(PREEMPT_RT), C2(PREEMPT_RT + CPU isolation)는
동일한 일반 Linux UDP socket 프로그램을 사용한다. 빌드 및 실행 방법,
application timestamp boundary, tmpfs/mmap 저장 형식은
[`udp_socket/README.md`](udp_socket/README.md)에 분리해 두었다.

```bash
make udp
```

## 구현 코드

메모의 임시 분류 정책을 실행 가능한 libbpf 프로젝트로 구현했다.
포트, RT CPU, map 크기, queue 크기, 기본 interface와 bpffs pin 경로는
`BPF/config.h` 한 곳에서 관리한다.

```text
UDP dst 9001  -> CPUMAP[3] (queue size 256)
UDP dst 9002  -> XSKMAP[RX queue] (AF_XDP socket 등록 시)
그 외         -> XDP_PASS
```

필요한 도구가 설치된 타깃에서 다음처럼 빌드한다. `vmlinux.h`는 빌드할 때마다
실행 중인 커널의 BTF에서 다시 생성해 비교하고, 내용이 바뀐 경우에만 교체한다.
따라서 패치한 커널로 부팅한 뒤 BPF 프로그램을 다시 빌드해야 한다.

```bash
sudo apt install clang llvm bpftool libbpf-dev pkg-config

# AF_XDP receiver까지 빌드할 때 추가
sudo apt install libxdp-dev
```

```bash
make            # BPF tools와 UDP sender/receiver/calibrator를 모두 빌드
make test       # 외부 NIC/root 권한 없이 가능한 offline 회귀 test

mkdir -p results/run01
sudo ./BPF/xdp_loader \
  --output-dir results/run01 \
  --prefix xdp- \
  eth0
```

Loader는 `XDP_FLAGS_DRV_MODE`로 Native XDP만 attach하며, 기존 프로그램을
덮어쓰지 않는다. 또한 프로그램을 해당 interface에 device-bound 상태로 load한다.
Native attach가 netdev를 close/open한 **뒤에** RX hardware timestamp filter를
활성화하며, detach 뒤에는 저장했던 설정을 명시적으로 다시 적용한다.
`CAP_NET_ADMIN` 등 BPF/XDP 작업에 필요한 권한이 있어야 한다.

`xdp_loader`는 `/sys/fs/bpf/ace_xdp`에 `cpu_map`, `xsk_map`, `xsk_owners`,
`stats`, `ingress_records`, `cpumap_records`, `runtime_config`를 pin한다. 따라서
`/sys/fs/bpf`가 bpffs로 마운트되어 있어야 하며, pin 디렉터리가 비어 있지 않으면
다른 run의 상태를 덮어쓰지 않고 실패한다.

기본 측정 record 용량은 262,144개이며 `--records`로 1~1,048,576 범위에서
변경할 수 있다. Loader는 기본 quiet mode이고 `--verbose`를 지정할 때만 1초마다
통계를 출력한다. SIGINT/SIGTERM으로 종료하면 자신이 붙인 프로그램인지 확인한
뒤 detach한다. 그 다음 최대 5초 동안 10 ms 간격으로 CPUMAP counter를 확인한다.
Requested 수와 terminal record/error 수가 같으면 최소 100 ms grace와 10회 연속
안정 snapshot 뒤 export한다. 5초가 지나도 안정된 차이가 남으면, 마지막 1초 이상
counter 변화가 없고 10회 연속 안정됐을 때만 redirect/enqueue loss 가능성을 뜻하는
`unobserved`로 기록하고 export한다. 그 외 timeout은 불완전한 측정으로 보고 pin을
보존한다.

```text
xdp-ingress_records.csv
xdp-cpumap_records.csv
xdp-stats.csv
```

출력 파일은 기존 파일을 덮어쓰지 않는다. 정상 종료는 detach → drain/export →
기존 HWTSTAMP 설정의 명시적 재적용 → unpin 순서로 끝난다. Detach, drain/export,
HWTSTAMP 복구 중 하나라도 실패하면 복구를 위해 pin을 남긴다. `--prefix`는
`run_id` 자체가 아니라 결과 디렉터리 안의 안전한 파일명 접두사다.

비정상 종료나 `SIGKILL` 뒤에는 다음처럼 복구한다. 소유 프로그램이 아직 붙어
있어도 아래 조건을 모두 만족하면 recovery가 직접 안전하게 detach한다.

```bash
mkdir -p results/run01-recovery
sudo ./BPF/xdp_loader --recover --output-dir results/run01-recovery eth0

# 또는 CSV를 별도 보존하면서 성공 후 pin까지 제거하려는 경우
mkdir -p results/run01-final-recovery
sudo ./BPF/xdp_loader \
  --recover --cleanup-pins \
  --output-dir results/run01-final-recovery \
  eth0
```

Loader는 attach 전에 고정 map 7개를 pin하고, 56-byte runtime ABI v4에 interface와
record capacity 외에도 XDP program ID, owner PID와 process start ticks(PID 재사용
구분), 저장/적용 HWTSTAMP 설정과 `APPLYING`/`APPLIED`/`STOPPING` 상태를 남긴다.
HWTSTAMP 적용 예정 상태를 ioctl 전에 publish하므로 ioctl과 map update 사이에
강제 종료되어도 saved/effective 둘 중 현재 상태를 판별해 복구할 수 있다.

XSKMAP은 userspace에서 실제 점유 여부를 lookup할 수 없으므로, AF receiver는
별도 `xsk_owners` HASH에 PID+process start ticks로 queue를 먼저 claim한 뒤 runtime을
재검증하고 XSKMAP에 socket을 등록한다. Loader/recovery는 `STOPPING`을 먼저
publish하고 살아 있는 claim이 있으면 detach를 거부하며, 죽은 receiver의 stale
claim만 제거한다. Owner가 죽었을 때 현재 Native XDP ID가 저장된 ID와 정확히 같으면 그
program만 자동 detach하고, 다른 program이 붙어 있으면 foreign XDP로 보고
거부한다. 이어서 저장된 HWTSTAMP를 복구하고 위의 최대 5초 CPUMAP drain 정책을
통과한 뒤 CSV를 export한다.

기본 recovery는 pin을 유지한다. `--cleanup-pins`도 export와 HWTSTAMP 복구가 모두
성공한 경우에만 고정 pin을 제거하며, 상태 복구 실패는 pin 보존과 exit 1로
남긴다. 기존 CSV는 덮어쓰지 않는다. 정상 종료와 recovery 모두 AF_XDP
receiver를 먼저 종료해 socket을 닫고 `xsk_owners` claim을 해제해야 한다.

`UDP/9002` 경로는 AF_XDP 소켓의 파일 디스크립터를 `xsk_map`의 RX queue key에
등록해야 활성화된다. socket이 없는 queue에서는 XDP가 안전하게 `XDP_PASS`로 fallback한다.

생성 파일을 정리하려면 `make clean`을 실행한다.

### 새 커널 부팅 후 최소 런타임 확인 (2026-09-10)

Pi 5의 `6.18.46-thesis-test-rt+`에서 BTF에 맞춰 BPF를 다시 빌드했다.
최초 로드에서는 UDP parser의 packet 범위 검증이 거부됐다. IP/UDP 길이 검사는
정수 비교로 유지하고, 메모리 접근은 `data_end`와 직접 비교하도록 수정해 해결했다.
커널 재빌드는 필요하지 않았다.

수정 후 BPF load와 `eth0` Native XDP attach, RX timestamp filter `all` 설정,
종료 시 detach·CSV 저장·기존 filter `none` 복원·pin 정리를 확인했다.
오프라인 테스트도 통과했다. 이 확인은 케이블 없이 수행해 실제 RX 표본은 0개이며,
AF_XDP 수신·CPUMAP 패킷 전달·HW timestamp 값·multi-descriptor DROP의 실제
패킷 검증을 완료했다는 뜻은 아니다.

### 외부 Ethernet 수신 검증 (2026-09-10)

이후 다른 Linux Pi에서 각 경로에 1 ms 주기로 1,000개씩 송신했다.
PASS·CPUMAP·AF_XDP 모두 최초 XDP 기록과 사용자 수신이 1,000개씩 일치했으며,
CPUMAP의 목적 CPU 기록도 1,000개였다. Packet ID에 누락·중복이 없었고,
최초 XDP의 HW timestamp는 세 경로 모두 1,000개에서 유효했다.
PASS socket 및 AF_XDP metadata로 전달된 HW timestamp도 원본과 일치했다.

CPUMAP UDP의 ancillary HW timestamp 누락은 새 SKB에 그 값을 채우지 않는
현재 구조의 예상 동작이며, 최초 XDP 기록과 packet ID로 연결해서 분석한다.
이 테스트는 기능 확인으로, 전후 PHC 보정·CPU 격리 조건을 갖춘 성능 측정이나
동시 부하·jumbo DROP 검증까지 완료한 것은 아니다.
자세한 결과와 남은 실험 범위는 [측정 돌리는 법](측정돌리는법.md)을 참고한다.

### AF_XDP 수신기

AF_XDP socket과 UMEM/RX ring까지 함께 테스트하려면 `afxdp_recv`를 사용한다.
AF_XDP socket API는 현재 libbpf에서 분리되어 libxdp로 제공되므로, 타깃에
`libxdp-dev`를 설치해야 한다.

```bash
sudo apt install -y libxdp-dev
make
```

먼저 loader를 별도 터미널에서 실행해 XDP와 pinned map을 유지한다.

```bash
mkdir -p results/run01
sudo ./BPF/xdp_loader --output-dir results/run01 --prefix xdp- eth0
```

그 다음 receiver를 실행한다. receiver는 BPF object를 load하거나 XDP를 attach하지
않고, loader가 pin한 `xsk_map`, `xsk_owners`, `stats`, `runtime_config`를
열어 schema/owner/program 상태를 검증한다. 두 번째 인자는 AF_XDP를
연결할 RX queue 번호이며 기본값은 `0`이다.

```bash
sudo ./BPF/afxdp_recv \
  --output results/run01/xsk-user-records.csv \
  --records 262144 \
  eth0 0
```

Receiver는 packet별 출력이나 hot-path 파일 I/O를 하지 않는다. UMEM packet 바로
앞의 56-byte XDP metadata와 32-byte experiment header를 검증하고, 미리 할당한
record 배열에 userspace 관측 시각을 저장한다. 종료하면
`xsk-user-records.csv`를 `fsync()`하고 BPF/AF_XDP/receiver 통계를 한 번 출력한다.
각 RX batch에서는 4,096개 UMEM frame의 userspace 소유 상태를 추적해 descriptor
중복과 recycle 누락을 치명적 invariant 오류로 처리한다.
출력 파일은 기존 파일을 덮어쓰지 않으며 record 배열이 가득 차면 partial CSV를
보존한 뒤 run을 실패 처리한다. 현재 AF_XDP 경로는 `XDP_COPY` mode다.

### 확정한 실험 packet 정책

- 실험용 wire header는 [`include/ace_packet_abi.h`](include/ace_packet_abi.h)의
  32-byte `struct ace_experiment_header` 하나를 UDP/BPF가 공유한다.
- 한 run 안의 packet key는 `flow_id + sequence`다. `run_id`는 packet이나 BPF
  map key에 넣지 않고 결과 디렉터리와 별도 manifest로 관리한다.
- UDP sender/receiver의 `--flow-id`는 필수다. 한 loader run에서 동시에 실행하는
  각 flow/path에는 서로 다른 flow ID를 배정하고 manifest에 기록한다.
- 실험 범위는 Ethernet II/IPv4/UDP, VLAN 없음, fragmentation 없음,
  single-descriptor packet이다.
- IPv4 MTU는 1500, UDP payload는 최대 1472 bytes로 제한한다. Sender는 DF를
  설정한다.
- Native XDP가 활성화된 동안 multi-descriptor RX frame은 전체 chain을
  drain/recycle하고 DROP한다. XDP multi-buffer/frags는 논문 범위가 아니며,
  drop 수는 driver의 `rx_xdp_multidesc_drops` 통계로 확인한다.

### Run manifest와 controller

`monitor/controller.sh`는 timestamp와 PID를 조합한 고유 `run_id` 디렉터리를
만들고, `ACE_RESULT_ROOT`와 `ACE_INTERFACE` 환경변수로 결과 root/interface를
받는다. 실행 시작 시 `monitor/capture_manifest.py`가 `manifest.json`, source
tree 상태와 dirty patch, kernel/BPF/userspace artifact hash, boot/CPU/NIC/PTP/tool
정보를 기록한다. 기본 `rpi-6.18.y` merge-base와 이후 커밋 목록/메일형 patch
series, `git diff --binary`와 untracked source의 raw binary patch/hash, 실제 boot
config·선택 kernel image·DTB,
overlay와 설치 module tree의 결합 SHA-256도 함께 남긴다. 기준 branch 이름이
다르면 `capture_manifest.py --kernel-base-ref REF`로 지정한다. 기존 파일은
덮어쓰지 않는다.

```bash
ACE_INTERFACE=eth0 \
ACE_RESULT_ROOT="$PWD/results" \
./monitor/controller.sh 50 1 60 0,1,2
```

현재 controller는 CPU/network 부하와 monitoring 및 manifest capture까지
담당한다. XDP loader, receiver, sender, calibration을 하나의 완전 자동화된
lifecycle로 묶는 작업은 아직 남아 있다.

### Offline record join

한 run의 record는 `analysis/join_results.py`로 `flow_id + sequence` full outer
join한다. CPUMAP/AF_XDP/UDP 입력은 해당 경로를 측정했을 때만 지정한다.

```bash
python3 analysis/join_results.py \
  --manifest results/run01/manifest.json \
  --ingress results/run01/xdp-ingress_records.csv \
  --cpumap results/run01/xdp-cpumap_records.csv \
  --afxdp results/run01/xsk-user-records.csv \
  --udp-native results/run01/udp-samples.bin \
  --phc-calibration results/run01/phc-monotonic-before.csv \
  --phc-calibration results/run01/phc-monotonic-after.csv \
  --output-csv results/run01/joined.csv \
  --summary-json results/run01/summary.json
```

`--manifest`는 필수다. Analyzer는 manifest의 version/`run_id`와 모든
입출력이 같은 resolved run directory에 속하는지 먼저 검증한다. 또한
requested path, metadata/validation flag, timestamp/error 조합, ingress와
CPUMAP/AF_XDP/UDP sink의 일치를 검사한다. 의미적으로 모순된 raw row는
`inconsistency_flags`로 보존하되 해당 metric에서 제외한다.

`--phc-calibration`은 전/후 파일처럼 반복 지정할 수 있고, 선택된 PHC
calibration 표본 사이 offset을 선형 보간한다. 기본
정책에서는 calibration 범위 밖 timestamp의 clock-crossing metric을 제외하고
out-of-range로 집계한다. `--allow-endpoint-clamp`는 가까운 endpoint offset을
사용하는 탐색적 분석에서만 명시한다. 최종 run은 직전/직후 calibration이 측정
구간을 bracket해야 한다. `xdp-hw`, `cpumap-xdp`, `user-cpumap`, `user-xdp`, `user-hw` 지연과
누락·중복·충돌·음수/out-of-range, 각 metric의
count/min/mean/median/p95/p99/observed maximum을 summary JSON에 남긴다. 출력 두
파일은 `O_EXCL`과 `fsync()`를 사용해 기존 결과를 덮어쓰지 않는다. 모든
입력과 두 출력의 크기/SHA-256을 묶은 `summary.json.complete`를 마지막에
만들어 완결된 run만 후속
분석에 들어가게 한다. UDP binary는 capture host와 같은 native C
ABI/byte order의 환경에서 분석해야 한다.

여러 독립 run은 completion marker만 입력으로 받는
`analysis/aggregate_runs.py`로 묶어 pooled percentile·명시적 deadline miss와,
독립 run에 동일 가중치를 둔 p50/p95/p99·miss rate 평균의 양측 95%
Student-t 신뢰구간을 계산한다. Pooled packet 통계에는 CI를 붙이지 않는다.
자세한 사용법과 통계 단위는
[`analysis/README.md`](analysis/README.md)에 있다.

```bash
python3 -m unittest analysis.tests.test_join_results \
  analysis.tests.test_aggregate_runs tests.test_capture_manifest
```

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

### Observed maximum latency

측정 구간에서 관찰된 최대 latency를 확인한다. 유한한 실험에서 얻은 값이므로
수학적으로 보장된 worst-case 또는 WCET로 표현하지 않는다.

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
libxdp (AF_XDP socket/ring API)
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

---

## 13. 기대되는 결과 형태

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
