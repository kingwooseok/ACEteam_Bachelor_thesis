# Raspberry Pi에서 직접 실행하는 실험 제어기

두 Raspberry Pi에서 같은 설정 파일을 읽는 C 프로그램 `ace-run`을 실행한다.
VMware는 코드 관리·빌드 검증과 실험 후 분석에 사용한다. 실제 송신, 수신,
시계 보정과 부하 관리는 두 Pi에서 수행한다.

## 준비와 실행

양쪽 Pi의 저장소에서 프로그램을 빌드한다. `repo=AUTO`이면 `ace-run` 실행 파일의
상위 저장소를 사용한다. 실제 측정에는 Native XDP와 PREEMPT_RT를 준비한 기존
실험 환경 및 각 도구의 실행 권한이 필요하다. 제어기는 커널 설정, NIC MTU,
IRQ affinity, 주파수와 패킷 크기를 변경하지 않는다.

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

각 역할의 CPU, RT 우선순위와 mlock 권한이 일반 계정에 제공된 경우 sudo를 생략할
수 있다. 제어기는 자기 자식 프로세스에만 CPU 배치와 scheduler policy를 적용한다.
`supervisor_cpu=inherit` 또는 흐름별 CPU의 `inherit`는 호출자의 affinity를 따른다.
policy는 `other/fifo/rr`, priority는 OTHER=0, FIFO/RR=1..99이다.

## 실행 순서

1. TCP 제어 연결에서 설정 파일 SHA256, run ID, 프로토콜 버전과 세션 nonce를 검사한다.
2. 수신기가 XDP loader, 경로별 수신기, 부하·관측 도구를 시작하고 READY를 확인한다.
3. 송신기가 흐름별 C sender와 네트워크 부하를 준비한다.
4. `warmup_ms` 동안 **부하가 안정화되도록 기다린 뒤** PHC 사전 보정을 수행한다.
5. 송신 Pi에서 공통 MONOTONIC 출발 시각을 파일로 원자적으로 공개한다. 모든 sender가
   같은 시각을 기준으로 기존 절대 주기 송신을 시작한다.
6. 모든 sender가 pipe로 송신 완료를 알리면 STOP을 전달하고 `settle_ms` 동안
   잔여 도착을 기다린다. 수신기들은 `drain_ms` 동안 제한된 수신을 마친 뒤
   pipe로 완료를 알리고 메모리에서 대기한다.
7. 모든 수신의 완료를 확인한 뒤 SIGUSR1로 수신 기록 저장을 허용한다. PHC 사후
   보정과 XDP 기록 출력을 마치면 송신 측에 DONE을 전달한다. 송신 측은 sender의
   통계를 저장하고 네트워크 부하 클라이언트를 종료한 뒤 ACK를 보낸다.
8. 수신 측은 ACK를 받은 뒤 부하·관측 도구와 iperf3 서버를 종료하고 완료를 기록한다.
   다른 흐름의 마지막 송수신과 파일 I/O가 겹치지 않도록 캡처 완료와 저장 완료를
   분리하며, 네트워크 부하 클라이언트가 정상 종료할 때까지 서버를 유지한다.

측정 중 제어기는 종료·자식 실패·기한·상대 연결 변화에만 깨어나는 차단 대기를
사용한다. heartbeat, SSH와 Python 실행은 없다. 메타데이터 수집을 위한 git 및
sha256sum은 준비 단계에서만 실행한다. `monitor/controller.sh`와
`monitor/network_load.sh`는 native 부하 도구를 제어하고 패킷별 처리에 개입하지 않는다.

`duration_ms`와 각 흐름의 `period_ns`로 `ceil(duration_ns/period_ns)`개를 전송한다.
활성 흐름은 1~3개이며 전체 수신 기록 수는 run당 1,048,576개 이하로 제한한다.
기존 64-byte 실험 payload, 32-byte wire header와 56-byte UDP record는 유지한다.
`modes=mixed,all-pass`는 각 반복에서 혼합 경로와 동일 입력의 all-PASS 구성을 순서대로
실행한다. 경로와 flow ID·포트는 기존 정책에 맞춰 고정된다.

`load.cpu_percent`와 `load.cpu_cpus`는 CPU 부하를, memory/io 항목은 해당 부하를
지정한다. `load.network_bps`는 방향당 병렬 연결 전체의 전송률이며 helper가 나눠 적용한다.
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
  sender/runs/<run_id>-<mode>-r0001/
    run.json  manifest.json  .complete
    sender-flowN.json  sender-flowN.log  network-load.log
```

`run.json`에는 실제 count·주기·경로·실행 배치와 부하 조건을 기록한다. 준비 실패,
TCP 단절, Ctrl+C와 제한 시간 초과 시 제어기가 만든 자식들만 종료하고 기록 저장을
기다린다. 실패한 실행은 `.failed`로 남으며 완성된 실행과 섞지 않는다. 같은 run ID의
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

## seong-integration: D01–D11 선택 실행

친구 기준본 `04dece3`의 `ace-run`, key=value 설정, profile JSON, native TCP
제어 및 사후 분석을 유지한다. `seong`의 SSH/Python 실시간 제어기는 가져오지
않는다. `run_matrix.sh`는 조건 사이에서만 다음 `ace-run`을 시작하는 wrapper다.
측정 중에는 native 제어기의 대기·완료·실패 정리 절차를 그대로 사용한다.

기존 `build_plan.py` 호출 및 config의 기본값은 유지된다. 전체 D01–D11 설계는
`--d01-d11` 또는 아래 wrapper의 `plan` 명령으로 명시적으로 선택한다.

```bash
# PC에서 실행 가능. 먼저 profile.example.json을 복사하고 null 항목을 채운다.
# network_high_bps/network_middle_bps/pass_high_pps는 담당자의 예비측정 결과다.
./experiment/run_matrix.sh plan --profile /path/profile.json --output /path/matrix
# 같은 기능의 기존 생성기 진입점:
python3 experiment/design/build_plan.py --d01-d11 \
  --profile /path/profile.json --output /path/another-matrix
```

선택 계획은 다음만 추가한다.

- 단독 PASS/CPUMAP/AF_XDP를 먼저 실행하고, 각 단독 조건도 all-PASS/mixed 각 5회
  비교한다. 기존 생성기는 단독 mixed 5회를 계속 사용한다.
- A의 네 극단 조건과 세 중간 조건, B의 PASS 0/1000/3000/9000/최고 PPS × CPU
  0/100%, C의 세 CPU 공유 조건을 생성한다. B/C에는 배경 네트워크 부하가 없다.
- 최고 PASS는 9000..15476 PPS다. 9000을 그대로 채택하면 중복을 만들지 않고,
  더 높은 최고값을 선택해도 기본 9000을 유지한다. native `flow_count` 계산과
  1,048,576개 기록 한도를 그대로 따른다. 중요 flow의 목표 개수는 각 60,000개다.
- 최고값 9000이면 18조건/180 run, 더 높으면 20조건/200 run이다. 동일 조건은
  `matrix.csv`·`execution.json`의 references로 재사용하며 표본을 복제하지 않는다.
- `verify_placement=1`로 준비 완료 직후 송신·수신 flow 프로세스의 실제 affinity,
  policy, priority를 검사한다. `sender-flowN.placement.json` 및
  `receiver-flowN.placement.json`에 실제 값과 verified를 남긴다. 불일치는 기존
  실패 정리 경로로 전달한다. 이 옵션의 기본값은 0이며 기존 실행은 바뀌지 않는다.

profile의 송신/수신 정책 배열과 CPU 값이 native 설정으로 전달된다. 기본 profile의
PASS는 OTHER/0, 두 중요 flow는 FIFO/80이다. TX CPU 0/1/2, TX 네트워크 부하 CPU 3,
RX 9001·CPUMAP CPU 3 및 IRQ CPU 2는 기존 생성기 배치를 사용한다. 각 역할의
관리 CPU와 RX 네트워크 부하 CPU는 기존 profile 항목으로 독립 설정한다.

생성 폴더를 같은 내용으로 두 장치에 복사한 뒤, **각 장치에서 로컬 실행**한다.
SSH는 필요하지 않으며 wrapper가 다른 장치에 접속하지 않는다. 양쪽 native 도구를
미리 빌드하고 XDP/PHC, mlock, scheduler, governor·IRQ 적용에 필요한 기존 권한을
준비한다. 보통 root로 실행한다. 네트워크 부하·관측 도구의 의존성도 기존과 같다.
비밀번호는 설정에 넣지 않는다.

```bash
# 수신 장치에서 먼저:
sudo ./experiment/run_matrix.sh receiver --plan-dir /path/matrix
# 송신 장치에서:
sudo ./experiment/run_matrix.sh sender --plan-dir /path/matrix
```

wrapper는 모든 config의 hash와 native parser 검사를 끝낸 후 실행하며 실패 시
다음 조건으로 넘어가지 않는다. Ctrl+C/TERM은 실행 중인 `ace-run`의 기존 정리를
기다린다. 출력은 기존 `results/experiment/<case_id>/<role>/runs/` 구조다.
기존 case/run 디렉터리를 덮어쓰거나 미완료 run을 자동 재사용하지 않는다. 새 캠페인을
실행하려면 기존 결과를 먼저 보관하고 출력 위치를 비워야 한다.

측정 후 양쪽 `output_root` 트리를 Linux 분석 장비에 각각 복사한다. UDP binary는
native ABI이므로 capture 장비와 호환되는 분석 환경을 사용한다.

```bash
./experiment/run_matrix.sh collect --plan-dir /path/matrix \
  --receiver-root /copied/rx-output-root --sender-root /copied/tx-output-root \
  --output /path/new-collected-matrix
```

수집은 기존 `analysis/analyze_experiment.py`의 collection, join 및
`aggregate_runs.aggregate_by_flow`를 호출한다. 출력은 `analysis-report/report.json`
및 `report.html`이다. 중요 flow의 100000 ns deadline, 지연 분포, 목표/실제 송신,
고유 수신, 손실, CPU 사용률을 기존 방식으로 집계한다. mode·repeat가 누락되거나
계획 config hash가 다른 조건은 실패로 표시하고 전체 조건 집계에서 제외한다.
이미 있는 분석기를 복제하거나 raw 파일을 수정하지 않는다.

### 의도적으로 유지한 차이와 검증 범위

- BPF runtime ABI 5 및 immutable forwarding mode를 유지한다. 친구 구현에서
  all-PASS의 `requested_path`는 실제 PASS 경로를 기록한다. flow ID가 원래 트래픽
  종류를 구분한다. 우리 ABI 4 및 요청 경로를 보존하는 다른 의미로 되돌리지 않는다.
- SSH JSON 제어 대신 친구 native 고정 길이 frame과 전체 전송 deadline을 유지한다.
  부분 frame이 들어와도 timeout을 지키는지 Linux socketpair로 검증한다.
- 5초 안정화→PHC before→공통 출발→제한된 drain→저장/PHC after 순서는
  친구 native 구현을 따른다. **60초는 설정상 송신 구간**이다. sender가 밀리면
  목표 개수 완료 또는 기존 `duration + start_delay + ready_timeout` 기한까지
  기다린다. 우리 이전 wrapper의 벽시계 60초 강제 중단은 이 절차를 바꾸므로 미반영했다.
  실제 송신 구간은 sender 통계로 확인해야 한다.
- 환경 준비는 기존 `prepare_env.sh`가 governor와 수신 IRQ를 적용하고,
  준비 도중 실패한 변경은 기존 rollback을 수행한다. 성공한 설정은 기존대로 유지된다.
  모든 run 실패에서 전체 장치 설정을 자동 원복하는 기능을 새로 만들지 않는다.
- Linux loopback은 제어·준비·송수신·drain·저장·실패 처리를 검증한다. NIC HWTS,
  PHC, 실제 XDP/CPUMAP/AF_XDP, RT 권한·부하 및 IRQ 배치는 실제 장치 검증이 필요하다.
