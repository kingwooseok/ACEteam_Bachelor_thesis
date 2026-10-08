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
