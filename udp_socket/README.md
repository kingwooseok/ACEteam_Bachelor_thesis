# UDP Socket Baseline

Raspberry Pi 5 기반 Embedded Linux에서 critical UDP 통신의 tail latency를
비교하기 위한 일반 Linux UDP socket baseline이다.

UDP socket 프로그램은 C0(Vanilla), C1(PREEMPT_RT),
C2(PREEMPT_RT + CPU isolation)에서 동일하게 사용한다. PTP 동기화와 CPU
affinity는 프로그램 밖에서 설정한다.

## 구조

```text
include/
└── ace_packet_abi.h   # BPF와 UDP가 함께 쓰는 32-byte wire ABI

udp_socket/
├── include/
│   ├── config.h       # 한곳에 모은 기본값
│   └── protocol.h     # shared wire ABI include와 binary record 형식
├── src/
│   ├── sender.c
│   ├── receiver.c
│   └── phc_calibrate.c
└── Makefile
```

## UDP socket 빌드

추가 library 없이 Linux C library만 사용한다.

```bash
# 저장소 최상위 디렉터리에서
make udp

# BPF와 UDP 전체 build + NIC 없는 loopback/ABI/CLI 회귀 검사
make
make test

# 또는 udp_socket 디렉터리에서
make
```

생성 파일은 `udp_socket/bin/sender`, `udp_socket/bin/receiver`,
`udp_socket/bin/phc_calibrate`다. 컴파일에는 `-Wall -Wextra -Wpedantic`가
적용된다.

## 실행

Receiver를 먼저 실행한다. 결과 디렉터리는 실행 전에 만들어 둔다.

```bash
mkdir -p results/run01

./udp_socket/bin/receiver \
  --bind 0.0.0.0 \
  --port 9000 \
  --flow-id 0 \
  --payload-size 64 \
  --count 200000 \
  --tmpfs /dev/shm/ace_udp_run01.tmp \
  --output results/run01/samples.bin
```

Sender에서 receiver의 IPv4 주소를 지정한다.

```bash
./udp_socket/bin/sender \
  --port 9000 \
  --flow-id 0 \
  --count 200000 \
  --period-ns 1000000 \
  --payload-size 64 \
  192.168.1.20
```

`--flow-id`는 sender와 receiver 모두 필수다. 한 flow를 주고받는 두 프로그램에는
같은 값을 사용하고, 한 loader run에서 동시에 보내는 각 flow/path에는 서로 다른
값을 사용한다. 예를 들어 PASS/CPUMAP/XSK 흐름을 각각 0/1/2로 지정한다.

`--payload-size`는 32-byte application header 뒤에 붙는 실험 payload 크기다.
기본값은 64 bytes이므로 실제 UDP datagram payload는 96 bytes다. 각 프로그램은
시작할 때 role, 주소, port, period/개수, 파일 위치, clock을 한 번 출력하고
packet별 로그는 남기지 않는다. 전체 옵션은 `--help`로 확인할 수 있다.

CPU isolation 실험에서는 코드 설정을 바꾸지 않고 예를 들어 다음처럼 외부에서
affinity를 적용한다.

```bash
taskset -c 3 ./udp_socket/bin/receiver [options]
```

실행 전 `ptp4l`과 `phc2sys`로 두 Pi의 `CLOCK_REALTIME`을 동기화해야 한다.
이 프로그램은 PTP daemon을 직접 제어하지 않는다.

## 측정 boundary와 메모리 저장

Sender는 sequence number를 정한 뒤 `CLOCK_REALTIME`의 nanosecond timestamp를
packet header에 기록하고 `sendto()`를 호출한다. 주기는
`CLOCK_MONOTONIC` absolute sleep으로 유지하므로 realtime clock 보정이 pacing에
영향을 주지 않는다.

`flow_id + sequence`가 한 loader run 안에서 packet을 고유하게 식별한다. `run_id`는
wire packet에 넣지 않고 `results/run01` 같은 결과 디렉터리와 별도 run manifest로
관리한다. 동시 flow 사이에 `flow_id`가 겹치면 같은 sequence의 BPF HASH key가
충돌하므로 run 시작 전 flow ID 배정을 manifest에 기록한다.

Sender는 `IP_PMTUDISC_DO`를 설정하며 UDP payload를 IPv4 MTU 1500에서 fragmentation이
발생하지 않는 최대 1472 bytes로 제한한다. 현재 32-byte header를 제외하면
`--payload-size`의 최댓값은 1440이다. 이는 실험 packet을 macb의 단일 RX
descriptor 범위에 유지하기 위한 조건이다. Native XDP가 붙어 있을 때 이 범위를
벗어나 multi-descriptor가 된 RX frame은 분류기를 우회하지 않고 driver에서 frame
전체가 DROP되며 `rx_xdp_multidesc_drops`에 집계된다. Jumbo negative test는 실제
외부 Ethernet 환경에서 별도로 수행해야 한다.

Receiver setup 순서는 다음과 같다.

```text
open(tmpfs) → ftruncate(expected × 56) → mmap(MAP_SHARED)
→ mlockall(MCL_CURRENT | MCL_FUTURE) → 전체 영역 pre-touch
```

수신 hot path에는 다음 작업만 있다.

```text
recvmsg → CLOCK_MONOTONIC → CLOCK_REALTIME → HWTS decode
→ header decode → records[index] 대입 → index++
```

hot path에서는 파일 I/O, allocation, text logging, mutex를 사용하지 않는다.
정상 완료 또는 SIGINT/SIGTERM 후 실제 수신된 record만 output의
`samples.bin`으로 복사하고 `fsync()`한다. tmpfs staging 파일도 실제 record
크기로 줄여 남긴다. Receiver는 staging 경로의 filesystem이 실제 tmpfs인지
검사한다. `mlockall()`에 실패하면 측정을 시작하지 않으므로 Pi에서 실행 계정의
memlock limit 또는 권한을 미리 설정해야 한다.

## Hardware RX timestamp

Receiver는 `SO_TIMESTAMPING`으로 raw hardware RX timestamp control message를
요청한다. 실제 측정에서는 interface filter도 함께 설정하고 timestamp 누락을
실패로 처리한다.

```bash
./udp_socket/bin/receiver \
  --interface eth0 \
  --require-hwts \
  --flow-id 0 \
  [나머지 옵션]
```

`--interface`를 사용하면 시작 전에 기존 HWTSTAMP 설정을 저장하고
`HWTSTAMP_FILTER_ALL`을 요청하며, 종료할 때 저장한 설정을 복원한다. XDP attach가
interface close/open을 발생시키므로 receiver와 PHC calibration은 XDP attach가
완료된 다음 시작해야 한다. `--require-hwts`를 생략하면 loopback 같은 기능
테스트에서 hardware timestamp가 없는 record도 보존할 수 있다.

PHC와 `CLOCK_MONOTONIC`의 offset은 XDP attach와 HWTSTAMP 설정이 끝난 다음
별도 도구로 수집한다.

```bash
./udp_socket/bin/phc_calibrate \
  --interface eth0 \
  --samples 25 \
  --iterations 10 \
  --output results/run01/phc-monotonic.csv
```

`--interface`는 `ETHTOOL_GET_TS_INFO`로 해당 netdev와 연결된 `/dev/ptpN`을 찾아
PTP index 변경을 추적한다. 명시적 `--device /dev/ptpN`과 동시에 사용할 수 없다.
각 표본은 `system-before, PHC, system-after`와 system span을 기록한다. 각 ioctl
batch에서 span이 가장 짧은 표본의 `selected` 값이 1이다. `offset_ns`는
`PHC - CLOCK_MONOTONIC`이므로 PHC timestamp를 MONOTONIC으로 바꿀 때는
`phc_ns - offset_ns`를 사용한다. `/dev/ptpN` 접근 권한이 필요하다. `--output`
파일은 `O_EXCL`로 새로 만들고 flush/`fsync()`하므로 기존 결과를 덮어쓰지 않는다.

## Binary 형식

Packet header는 [`../include/ace_packet_abi.h`](../include/ace_packet_abi.h)에
정의된 32-byte `struct ace_experiment_header`이며 모든 field는 network byte
order다. UDP sender/receiver와 BPF parser가 이 한 정의를 공유한다.

```c
struct ace_experiment_header {
    uint32_t magic;
    uint16_t version;
    uint16_t size;
    uint32_t flow_id;
    uint32_t reserved;
    uint64_t sequence;
    uint64_t tx_realtime_ns;
};
```

`magic`, `version`, `size`, `flow_id`가 일치하지 않는 packet은 invalid로
집계한다. `samples.bin`은 아래 56-byte record가 연속된 raw binary 파일이다.

```c
struct sample_record {
    uint64_t seq;
    int64_t tx_ns;
    int64_t user_rx_mono_ns;
    int64_t user_rx_real_ns;
    int64_t hw_rx_ns;
    uint32_t flow_id;
    uint32_t len;
    uint16_t flags;
    uint16_t reserved16;
    uint32_t reserved32;
};
```

Record는 receiver native byte order로 저장된다. Raspberry Pi 5의 일반적인
ARM64 little-endian 환경에서는 Python `struct` format `<QqqqqIIHHI` 또는 같은
layout의 NumPy dtype으로 읽을 수 있다. `hw_rx_ns`는 PHC domain이고
`user_rx_mono_ns`는 `CLOCK_MONOTONIC`이므로 calibration 전에는 직접 빼면 안
된다. 기존 OWD는 `user_rx_real_ns - tx_ns`로 계산할 수 있다. 완주한 run의
파일 크기는 `56 × expected_samples` bytes다.

## 기본값

- UDP port: 9000
- period: 1,000,000 ns (1000 packets/s)
- flow ID: 기본값 없음, `--flow-id` 필수
- experiment payload: 64 bytes (32-byte header를 포함한 UDP payload는 96 bytes)
- samples per run: 200,000
- tmpfs: `/dev/shm/ace_udp_samples.tmp`
- result: `./samples.bin`

전체 연구 구조와 XDP/AF_XDP 설명은 상위 `README.md`를 참고한다.
