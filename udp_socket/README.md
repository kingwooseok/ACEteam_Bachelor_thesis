# UDP Socket Baseline

Raspberry Pi 5 기반 Embedded Linux에서 critical UDP 통신의 tail latency를
비교하기 위한 일반 Linux UDP socket baseline이다.

UDP socket 프로그램은 C0(Vanilla), C1(PREEMPT_RT),
C2(PREEMPT_RT + CPU isolation)에서 동일하게 사용한다. PTP 동기화와 CPU
affinity는 프로그램 밖에서 설정한다.

## 구조

```text
udp_socket/
├── include/
│   ├── config.h       # 한곳에 모은 기본값
│   └── protocol.h     # wire header와 binary record 형식
├── src/
│   ├── sender.c
│   └── receiver.c
└── Makefile
```

## UDP socket 빌드

추가 library 없이 Linux C library만 사용한다.

```bash
# 저장소 최상위 디렉터리에서
make udp

# 또는 udp_socket 디렉터리에서
make
```

생성 파일은 `udp_socket/bin/sender`와
`udp_socket/bin/receiver`다. 컴파일에는 `-Wall -Wextra -Wpedantic`가
적용된다.

## 실행

Receiver를 먼저 실행한다. 결과 디렉터리는 실행 전에 만들어 둔다.

```bash
mkdir -p results/run01

./udp_socket/bin/receiver \
  --bind 0.0.0.0 \
  --port 9000 \
  --payload-size 64 \
  --count 200000 \
  --tmpfs /dev/shm/ace_udp_run01.tmp \
  --output results/run01/samples.bin
```

Sender에서 receiver의 IPv4 주소를 지정한다.

```bash
./udp_socket/bin/sender \
  --port 9000 \
  --count 200000 \
  --period-ns 1000000 \
  --payload-size 64 \
  192.168.1.20
```

`--payload-size`는 16-byte application header 뒤에 붙는 실험 payload 크기다.
기본값은 64 bytes이므로 실제 UDP datagram payload는 80 bytes다. 각 프로그램은
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

Receiver setup 순서는 다음과 같다.

```text
open(tmpfs) → ftruncate(expected × 32) → mmap(MAP_SHARED)
→ mlockall(MCL_CURRENT | MCL_FUTURE) → 전체 영역 pre-touch
```

수신 hot path에는 다음 작업만 있다.

```text
recv → CLOCK_REALTIME → header decode → records[index] 대입 → index++
```

hot path에서는 파일 I/O, allocation, text logging, mutex를 사용하지 않는다.
정상 완료 또는 SIGINT/SIGTERM 후 실제 수신된 record만 output의
`samples.bin`으로 복사하고 `fsync()`한다. tmpfs staging 파일도 실제 record
크기로 줄여 남긴다. Receiver는 staging 경로의 filesystem이 실제 tmpfs인지
검사한다. `mlockall()`에 실패하면 측정을 시작하지 않으므로 Pi에서 실행 계정의
memlock limit 또는 권한을 미리 설정해야 한다.

## Binary 형식

Packet header는 network byte order이며 16 bytes다.

```c
struct packet_header {
    uint64_t seq;
    int64_t tx_ns;
};
```

`samples.bin`은 아래 32-byte record가 연속된 raw binary 파일이다.

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

Record는 receiver native byte order로 저장된다. Raspberry Pi 5의 일반적인
ARM64 little-endian 환경에서는 Python `struct` format `<QqqIHH` 또는 같은
layout의 NumPy dtype으로 읽을 수 있다. OWD는 offline에서
`rx_ns - tx_ns`로 계산한다. 완주한 run의 파일 크기는
`32 × expected_samples` bytes다.

## 기본값

- UDP port: 9000
- period: 1,000,000 ns (1000 packets/s)
- experiment payload: 64 bytes (16-byte header를 포함한 UDP payload는 80 bytes)
- samples per run: 200,000
- tmpfs: `/dev/shm/ace_udp_samples.tmp`
- result: `./samples.bin`

전체 연구 구조와 XDP/AF_XDP 설명은 상위 `README.md`를 참고한다.
