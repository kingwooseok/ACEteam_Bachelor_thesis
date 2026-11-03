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

## 자동 실험의 준비·시작·종료

송신기와 수신기의 `--ready-file PATH`는 socket, 메모리 잠금, 기록 공간 등
준비가 끝났을 때 완성된 JSON 파일을 원자적으로 공개한다. 파일에는 `role`,
`pid`, `ready_mono_ns`가 들어간다. 준비 메시지는 stdout에도 즉시 flush된다.
이미 있는 준비 파일과 `--stats-json PATH`는 덮어쓰지 않는다. 매 실행마다
새 디렉터리를 사용하고 준비 파일과 프로세스 생존 여부를 함께 확인한다.
준비 파일은 실행 기록으로 남는다.

여러 sender를 동시에 출발시킬 때는 모든 sender에 같은 `--start-file PATH`를
전달한다. sender는 READY 이후 이 파일을 기다리며, 파일에 적힌 **송신 장비의
CLOCK_MONOTONIC 절대 ns**에 송신을 시작한다. 준비 파일들이 모두 생성된 뒤
같은 송신 장비에서 다음과 같이 시작 파일을 공개한다.

```bash
./udp_socket/bin/ace_clock --after-ms 200 > "$run_dir/start.ns.tmp"
mv "$run_dir/start.ns.tmp" "$run_dir/start.ns"
```

`--start-at-ns NS`로 절대 시각을 직접 지정할 수도 있다. 두 시작 옵션은 함께
사용할 수 없으며, 잘못된 값이나 이미 지난 시작 시각은 전송 전에 거부한다.
시작 옵션을 생략하면 기존처럼 바로 송신한다. 시작 파일 대기는 측정 전에만
일어나며 SIGINT/SIGTERM으로 종료할 수 있다. 서로 다른 장비의 MONOTONIC 값은
공유하지 않고, 한 송신 장비에서 실행하는 흐름들이 하나의 시작 파일을 사용한다.

sender는 기존 절대 주기 누적과 catch-up 동작을 유지한다. `--stats-json`에는
`attempted`, `sent`, `errors`, `expected_count`, `flow_id`, 시작·종료 MONOTONIC 시각,
`termination_reason`을 저장한다. `first_send_mono_ns`와 `last_send_mono_ns`,
`interval_min_ns/max_ns/mean_ns`, `lateness_max_ns/mean_ns`, `late_period_count`는
사용자 공간의 송신 시도 간격과 예정 시각 대비 지각을 보여 준다. NIC HW TX
타임스탬프는 아니다. `elapsed_ns`는 첫 송신 시도부터 송신 loop 종료까지이며
준비와 예정 출발 시각까지의 대기는 제외한다. 송신 전에 중단되면 시작 시각과
elapsed는 0이다.

receiver는 SIGINT/SIGTERM을 받은 뒤 `--drain-ms` 동안 남은 수신을 처리한다
(기본 100 ms, 0이면 즉시 마무리). 추가 신호나 추가 패킷이 이 시간을 늘리지 않는다.
`--idle-timeout-ms`는 마지막 유효 패킷 이후의 대기 제한이며 기본 0은 비활성이다.
첫 패킷 전에는 준비 완료 시각을 기준으로 한다. blocking recvmsg에는 10 ms timeout을
사용하여 신호 직전/직후의 실행 순서 때문에 무기한 대기하는 경우를 막는다.
따라서 종료 기한 확인에는 최대 한 번의 timeout과 스케줄링 지연이 더해질 수 있다.
기존 관측점인 recvmsg 반환 직후 MONOTONIC/REALTIME 수집은 그대로 유지한다.

기록은 처음에 `--count × 56 bytes`를 확보한 배열에 순서대로 저장하고, 용량에
도달하면 더 쓰지 않고 종료한다. 종료 후 유효한 record만 저장하고 fsync한다.
수신 JSON에는 `received`, `invalid`, `truncated`, `missing_hwts`, `socket_drops`,
`receive_errors`, `capacity_reached`, `records_saved`, `exit_status`와 종료 이유가 있다.
`count`, `signal_drain`, `idle_timeout`은 수신 loop의 정상 종료 이유이며, 정상 종료
코드만으로 전체 패킷 수신을 판단하지 않고 `received`와 송신 통계를 함께 사용한다.
HWTS 필수 조건 위반이나 수신·저장 오류는 실패 코드로 반환한다.

`ace-run`은 송수신기에 `--completion-fd FD --defer-flush`도 전달한다. 송신기는
모든 sendto를 끝낸 뒤, 수신기는 TERM에 따른 drain을 끝낸 뒤 inherited pipe에
byte 0(성공) 또는 1(실패)을 보낸다. 정상 캡처 후에는 SIGUSR1이 올 때까지
통계·레코드를 메모리에 유지한다. 모든 흐름의 수신이 끝난 뒤 제어기가 기록 저장을
허용하므로 한 흐름의 파일 I/O가 다른 흐름의 마지막 패킷 처리와 겹치지 않는다.
수신기의 `--defer-flush`만 지정하면 count 완료 후 TERM까지 기다리는 단일 종료
장벽으로 사용할 수 있다. 자동화의 두 단계 종료 장벽에서는 완료 FD도 함께 사용한다.

자동화 회귀 검증은 Linux loopback에서 실행한다.

```bash
make -C udp_socket
python3 -m unittest -v tests.test_udp_automation
```
