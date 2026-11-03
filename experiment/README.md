# Raspberry Pi에서 직접 실행하는 실험 제어기

두 Raspberry Pi에서 같은 설정 파일을 읽는 C 프로그램 `ace-run`을 실행한다.
VMware는 코드 관리·빌드 검증과 실험 후 분석에 사용한다. 실제 송신, 수신,
시계 보정과 부하 관리는 두 Pi에서 수행한다.

## 준비와 실행

양쪽 Pi의 저장소에서 프로그램을 빌드한다. `repo=AUTO`이면 `ace-run` 실행 파일의
상위 저장소를 사용한다. 실제 측정에는 Native XDP와 PREEMPT_RT를 준비한 기존
실험 환경 및 각 도구의 실행 권한이 필요하다. 실제 run 준비 단계에서 양측 CPU
governor와 수신 IRQ affinity를 설정값대로 적용·검증한다. 공유 영역 설정을 사용하는
수신 실행은 `monitor/check_cpu_partition.sh`로 CPU 0~3의 일반 사용 가능 상태와
RPS·NAPI 설정을 먼저 검사한다. mixed의 중요1이 활성화된 실행은 CPU 3을 동적으로
예약하고, 종료 후 반환한다.
`socket-only`는 장치 준비·예약 검사를 건너뛴다. 커널·드라이버, NIC MTU와 패킷 크기는
기존 실험 환경을 사용한다.

```bash
make -C udp_socket
make -C BPF
make -C experiment
cp experiment/config.example.conf experiment/run.conf
```

`run.conf`의 빈 값을 정하고 **동일 파일을 두 Pi에 복사**한다. 설정값은 C에서
정해진 키와 자료형으로 검사한다. shell source/eval을 사용하지 않는다.

```bash
./experiment/ace-run plan --config experiment/run.conf
```

수신 Pi에서 먼저 실행한다.

```bash
sudo ./experiment/ace-run receiver --config experiment/run.conf
```

송신 Pi에서 같은 파일로 실행한다.

```bash
sudo ./experiment/ace-run sender --config experiment/run.conf
```

최신 본실험은 수신 Pi의 cgroup v2 CPU 예약을 관리하므로 위 sudo 명령을 사용한다.
`receiver.shared_cpus=0,1,2,3`은 수신 Pi에서 사용할 전체 CPU 집합이다.
all-pass·pass-xsk의 일반 Linux 작업·PASS·AF_XDP·관리·관측은 이 집합을 공유한다.
mixed에서 중요1(`flow2.enabled=1`)을 CPUMAP으로 받을 때만 `receiver.cpumap_cpu=3`을
전용으로 예약하고, 일반 실행 영역은 CPU 0·1·2를 사용한다.
각 `flowN.rx_cpu`와 수신 측 supervisor/network CPU는 `inherit`로 작성한다.
실행기가 경로에 맞는 허용 집합을 적용하며, 앱별 단일 CPU 고정값과 혼용하지 않는다.

공유 영역 키가 없는 기존 설정은 원래 scalar CPU/inherit 동작을 유지한다.
policy는 `other/fifo/rr`, priority는 OTHER=0, FIFO/RR=1..99이며 최신 본실험의
수신 앱은 모두 OTHER/0이다. 장치 준비와 구성별 CPU 배치는
[실험 준비 안내](design/배포전_준비사항.md)를 따른다.

## 실행 순서

1. TCP 제어 연결에서 설정 파일 SHA256, run ID, 프로토콜 버전과 세션 nonce를 검사한다.
2. 수신기가 구성에 맞는 CPU 영역을 적용하고 XDP loader, 경로별 수신기,
   부하·관측 도구를 시작해 READY를 확인한다. 활성 CPUMAP 흐름이 있는 mixed는
   CPU 3 전용 영역을 만들고 중요1 앱을 그곳에 배치한다.
   로더와 수신기 준비 직후 `scheduler-ready.json`에 NIC IRQ·CPUMAP 커널 스레드의
   정책·RT 우선순위·허용 CPU를 기록한다. 이 단계는 스케줄링 정책을 변경하지 않는다.
3. 송신기가 흐름별 C sender와 네트워크 부하를 준비한다.
4. `warmup_ms` 동안 **부하가 안정화되도록 기다린 뒤** PHC 사전 보정을 수행한다.
5. 송신 Pi에서 공통 MONOTONIC 출발 시각을 파일로 원자적으로 공개한다. 모든 sender가
   같은 시각을 기준으로 기존 절대 주기 송신을 시작한다.
6. 모든 sender가 pipe로 송신 완료를 알리면 STOP을 전달하고 `settle_ms` 동안
   잔여 도착을 기다린다. 수신기들은 `drain_ms` 동안 제한된 수신을 마친 뒤
   pipe로 완료를 알리고 메모리에서 대기한다.
7. 모든 수신의 완료를 확인한 뒤 SIGUSR1로 수신 기록 저장을 허용한다. PHC 사후
   보정을 마치면 송신 측에 DONE을 전달한다. 송신 측은 sender의
   통계를 저장하고 네트워크 부하 클라이언트를 종료한 뒤 ACK를 보낸다.
8. 수신 측은 ACK를 받은 뒤 부하·관측 도구와 iperf3 서버를 종료하고,
   XDP 로더를 종료해 계측 기록을 저장한다. 네트워크 부하 송신이 끝날 때까지
   NIC의 XDP 연결을 유지한다.
   다른 흐름의 마지막 송수신과 파일 I/O가 겹치지 않도록 캡처 완료와 저장 완료를
   분리하며, 네트워크 부하 클라이언트가 정상 종료할 때까지 서버를 유지한다.
9. CPU 3 전용 영역을 해제하고 일반 작업의 CPU 0~3 사용을 복원한 뒤 완료를 기록한다.
   다음 구성은 복원을 확인한 뒤 시작한다.

CPU·네트워크 부하의 안전 타이머에는 측정·준비 대기·추가 수신 대기(`settle_ms`)·
drain과 준비/종료 제한 시간을 포함한다. 정상 종료는 위 단계의 명시적 종료 요청이
담당하므로 긴 settle을 설정해도 부하 타이머가 먼저 종료되지 않도록 한다.

CPU 관측은 C 프로그램 `udp_socket/bin/cpu_sample`이 기존 mpstat를 대신해 1초마다
수행한다. `/proc/stat` 누적 tick과 수신 기록에 공통인 MONOTONIC 시각을
`monitor/cpu-samples.csv`에 남긴다. 파일은 측정 중 tmpfs에 기록하고 종료 후
복사한다. 분석기는 최초 XDP부터 최종 수신 관측까지의 구간만 집계하며,
경계 표본은 겹친 시간으로 가중한다. 구간 전체를 덮는 표본이 없으면 값을
비워 두고 원인을 기록한다. 준비·저장 시간의 CPU 사용률은 이 평균에 포함하지 않는다.

측정 중 제어기는 종료·자식 실패·기한·상대 연결 변화에만 깨어나는 차단 대기를
사용한다. heartbeat, SSH와 Python 실행은 없다. 메타데이터 수집을 위한 git 및
sha256sum은 준비 단계에서만 실행한다. `monitor/run_load.sh`와
`monitor/network_load.sh`는 native 부하 도구를 제어하고 패킷별 처리에 개입하지 않는다.

`duration_ms`와 각 흐름의 `period_ns`로 `ceil(duration_ns/period_ns)`개를 전송한다.
활성 흐름은 1~3개이며 전체 수신 기록 수는 run당 1,048,576개 이하로 제한한다.
기존 64-byte 실험 payload, 32-byte wire header와 56-byte UDP record는 유지한다.
`modes=all-pass,pass-xsk,mixed`는 동일 입력으로 일반 소켓 처리, AF_XDP 추가,
CPUMAP 중요 수신 분리 구성을 순서대로 실행한다. flow ID·포트는 세 구성에서 유지한다.

최신 실험의 CPU 부하는 `load.cpu_layout=shared`, `load.cpu_workers=3`이다.
같은 세 워커가 구성별 일반 영역 전체를 공유하며 `load.cpu_percent`는 워커별 목표 강도다.
설정의 `load.cpu_cpus=0,1,2,3`에서 실행기가 mixed의 활성 CPUMAP CPU를 제외해 적용한다.
memory/io 항목은 해당 부하를 지정한다.
`load.network_bps`는 방향당 병렬 연결 전체의 전송률이며 helper가 나눠 적용한다.
network direction은 `forward/reverse/bidir`이며, `bidir`은 양방향에 각각 해당 전송률을 적용한다. `load.on_sec/off_sec`는 함께 0이면
연속 부하, 둘 다 양수이면 on/off 부하이다. 지연 기한을 정했으면 `flowN.deadline_ns`에
입력하고 0이면 아직 기한 지표를 사용하지 않는다.

## 기록과 실패 처리

기본 출력 구조는 다음과 같다. 양쪽 Pi에서 생성한 역할별 디렉터리를 실험 후
VMware로 가져와 사후 분석 도구에 전달한다.

```text
results/experiment/<run_id>/
  receiver/runs/<run_id>-<mode>-r0001/
    run.json  manifest.json  .complete
    udp-flowN.bin  receiver-flowN.json  afxdp.csv
    xdp-ingress_records.csv  xdp-cpumap_records.csv
    phc-before.csv  phc-after.csv  monitor/
    scheduler-ready.json  monitor/cpu-samples.csv
    cpu-partition-active.json  cpu-partition-restored.json
  sender/runs/<run_id>-<mode>-r0001/
    run.json  manifest.json  .complete
    sender-flowN.json  sender-flowN.log  network-load.log
```

`run.json`에는 실제 count·주기·경로·CPU 허용 집합(`rx_cpus`)과 부하 조건을 기록한다.
all-pass·pass-xsk의 앱은 `rx_cpus="0,1,2,3"`이다. mixed의 중요1은 `"3"`,
나머지는 `"0,1,2"`이며 CPUMAP 중요1을 끈 실행은 전체 CPU를 공유한다.
환경에는 전체 CPU와 실제 일반 영역, CPUMAP 예약 여부를 함께 기록한다.
`cpu-partition-active.json`과 `cpu-partition-restored.json`은 CPU 3을 예약한 실행의
실제 분리 상태와 반환 결과를 남긴다.
기존 `rx_cpu`는 단일 CPU일 때 번호를, 여러 CPU를 허용할 때 -1을 남긴다. 준비 실패,
TCP 단절, Ctrl+C와 제한 시간 초과 시 제어기가 만든 자식들만 종료하고 기록 저장을
기다린 뒤 CPU 3 예약도 해제한다. 실패한 실행은 `.failed`로 남으며 완성된 실행과 섞지 않는다. 같은 run ID의
기존 실행 디렉터리는 덮어쓰지 않는다. 다시 실행하려면 새 run ID를 사용한다.
TCP 제어는 두 Pi의 신뢰된 실험망에서 사용한다.

내부 자동화 옵션 `--completion-fd FD --defer-flush`는 inherited pipe에 숫자 byte
0(캡처 완료) 또는 1(실패)을 한 번 보내고 SIGUSR1까지 저장을 기다린다. 제어기는
실패 정리에서 TERM과 SIGUSR1을 함께 보내 대기 중인 자식도 기록을 저장하게 한다.

## 소켓 전용 운영 검증

`socket-smoke.conf`는 한 Linux 장비의 터미널 두 개에서 위 receiver/sender 명령에
각각 전달한다. XDP·PHC·부하를 사용하지 않고 3개 흐름의 준비, 동시 출발, 두 번의
반복, 종료와 파일 저장을 검증한다. 해당 숫자는 정식 실험 조건이 아니다.

```bash
python3 -m unittest -v experiment.test_runner
```

## 최신 조건표 실행: 고정 1,000 PPS · 네 부하 조건

`build_plan.py`와 `run_matrix.sh plan`은 같은 최소 실행표를 만든다.
`--d01-d11`도 동일한 설계의 호환 진입점이다. 실행표 개정 번호는
`fixed-1000pps-75-v4`이며 기존에 생성한 계획은 새 폴더로 다시 생성한다.

일반 9000·중요1 9001·중요2 9002는 각각 1,000 PPS로 고정한다.
다음 네 조건에서 같은 입력으로 세 수신 구성을 비교한다.

| 본실험 조건 | CPU 연산 부하 | iperf3 배경 네트워크 부하 |
|---|---|---|
| 기본 상태 | 끔 | 끔 |
| CPU 경합 | 켬 | 끔 |
| 네트워크 부하 | 끔 | 고부하 |
| 복합 부하 | 켬 | 고부하 |

iperf3의 UDP/5201 패킷도 PASS로 처리하므로 일반 통신량은 9000번 계측 흐름과
iperf3 배경 트래픽을 합한 양이다. 세 계측 흐름의 주기와 표본 수를 유지하면서
CPU·네트워크 부하를 조절한다.

| 구분 | 구성·조건 | 실행 수 |
|---|---|---:|
| 경로별 단독 | PASS/all-pass, CPUMAP/mixed, AF_XDP/pass-xsk에서 한 흐름씩 × 5회 | 15 |
| 부하 본실험 | 4조건 × all-pass/pass-xsk/mixed × 5회 | 60 |
| 합계 | 단독 15회 + 본실험 60회 | **75** |

단독 3개와 본실험 4개를 7개 조건 파일로 생성한다. CPU 부하는 같은 세 워커를
끄거나 워커별 100% 강도로 실행하며, 배경 네트워크 고부하 값은 예비측정으로 정한다.

### 세 구성과 수신기 위치

| mode | 9000 | 9001 | 9002 | 수신 앱의 허용 CPU |
|---|---|---|---|---|
| all-pass | PASS | PASS | PASS | 모두 0·1·2·3 공유 |
| pass-xsk | PASS | PASS | AF_XDP | 모두 0·1·2·3 공유 |
| mixed | PASS | CPUMAP | AF_XDP | 9001만 3, 나머지는 0·1·2 공유 |

all-pass는 일반 Linux 소켓 처리를, pass-xsk는 같은 공유 영역에 AF_XDP를 추가한
효과를 평가한다. mixed는 중요1을 전용 CPU 3으로 전달하여 커널 수신 처리와
수신 앱을 함께 실행한다. CPUMAP 큐와 전달 워커는 mixed에서만 준비한다.
총 네 CPU를 공유하는 구성과, 그중 CPU 3을 중요 수신용으로 분리한 구성을 비교한다.
단독 PASS·AF_XDP는 전체 네 CPU를 공유하며 CPUMAP 단독에서만 CPU 3을 예약한다.

CPU 부하 워커는 세 구성 모두 세 개이며 개별 CPU 고정 없이 일반 실행 영역을 공유한다.
허용 집합은 all-pass·pass-xsk에서 0·1·2·3, mixed에서 0·1·2다.
같은 부하 조건을 비교할 때 워커 수·목표 강도와 입력 트래픽을 유지한다.

`alternate_mode_order=1`은 세 구성의 시작 위치를 ABC/BCA/CAB로 순환한다.
각 구성은 다섯 번씩 측정한다. `matrix.csv`의 references에는 각 조건의
CPU·네트워크 부하 조합을 표시한다.

### 예비측정 값 적용과 실행

`profile.example.json`을 복사하고 다음 두 null 값을 예비측정 결과로 채운다.

- `network_high_bps`: UDP 단방향 1스트림 예비측정 기준 R의 90%.
- `settle_ms`: 송신 완료 이후 잔여 수신 대기시간.

수신 앱은 세 구성 모두 OTHER/0이며 공유 CPU 집합과 CPUMAP CPU는 profile에 담긴다.
송신 Pi는 중요 송신기에 FIFO80을 사용하여 시험 입력의 주기를 유지한다.
IRQ·CPUMAP 정책은 `scheduler-ready.json`에 기록하고, 각 수신 앱의 허용 CPU와
정책·우선순위는 `*-flowN.placement.json`에서 실행값과 대조한다.

```bash
# PC 또는 분석 장비에서 생성. 기존 폴더를 덮어쓰지 않는다.
python3 experiment/design/build_plan.py --profile /path/profile.json --output /path/new-matrix
# 같은 생성기를 사용하는 wrapper:
./experiment/run_matrix.sh plan --profile /path/profile.json --output /path/another-matrix
```

미확정 profile로 실행 파일을 만들지 않는다. profile 없는 생성은 `matrix.csv`와
`plan.json` 초안만 만든다. 실제 실행 계획에는 설정 파일, 해시, 각 조건의
`expected_modes`, `repeats`, `expected_runs`를 담은 `execution.json`이 포함된다.
설정 파일은 Windows에서도 저장 바이트와 SHA-256이 같은 UTF-8/LF로 생성된다.

같은 생성 폴더를 두 Pi에 복사하고 각 장치에서 직접 실행한다. 측정 중 제어는
기존 native `ace-run`이 담당하며 wrapper는 조건 사이에서만 다음 실행을 시작한다.

```bash
# 수신 Pi에서 먼저
sudo ./experiment/run_matrix.sh receiver --plan-dir /path/new-matrix
# 송신 Pi
sudo ./experiment/run_matrix.sh sender --plan-dir /path/new-matrix
```

wrapper는 모든 설정 해시와 native parser 검사를 마친 뒤 실행한다.
`verify_placement=1`에서 각 흐름의 실제 CPU 허용 집합·정책·우선순위를 준비 직후 검사하고
`*-flowN.placement.json`에 기록한다. 실패하면 다음 조건으로 넘어가지 않는다.
기존 실행 디렉터리는 보존하며, Ctrl+C/TERM은 native 실행기의 정리를 기다린다.

### 결과 수집과 구간 분석

양쪽 `output_root`를 Linux 분석 장비로 복사한 뒤 수집한다.

```bash
./experiment/run_matrix.sh collect --plan-dir /path/new-matrix \
  --receiver-root /copied/rx-output-root --sender-root /copied/tx-output-root \
  --output /path/new-collected-matrix
```

조건별 예정 mode·repeat와 실제 결과를 대조해 누락·중복·해시 불일치를 검사한다.
단독 조건은 5회, 본실험의 각 부하 조건은 세 구성 × 5회인 15회를 기대한다.
결과는 `analysis-report/report.json`과 `report.html`에 모은다.

같은 패킷의 HW 시각을 한 번 보정하여 d₁과 총지연에 재사용한다.
CPUMAP은 d₁=xdp−hw, d₂=cpumap−xdp, d₃=user−cpumap이며,
PASS·AF_XDP는 d₂=0, d₃=user−xdp이다. 같은 패킷에서 구간 합과 총지연을 대조하고,
총지연 p99는 패킷별 총지연으로 계산한다. 후단 비교는 PASS/AF_XDP의 d₃와
CPUMAP의 d₂+d₃를 사용한다. 손실과 기록 오류도 함께 보존한다.

P04는 복합 부하의 60초 리허설에서 송수신 수·실제 PPS·기록 용량·유효 시각 및
구간 합을 점검한다. 상세 필드는 [분석 README](../analysis/README.md)를 참고한다.
