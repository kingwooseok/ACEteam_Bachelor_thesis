# 커밋 전 실제 통신 성능 점검 — 2026-10-29

- 본 측정 시각: 2026-10-29T16:18:53+09:00 ~ 2026-10-29T16:23:21+09:00
- 본 측정: 8조건 × 10초 × 3회 = 24 run. 별도 손실 원인 진단 1회, 무부하 ping 100개.
- 실험 링크: craftt eth0 192.168.10.11 → craftp eth0 192.168.10.10, 양측 1Gbps Full Duplex, MTU 1500.
- iperf3 3.18, 단방향 UDP, 병렬 stream 1개, 임시 포트 55201. 송신 iperf CPU3, 수신 iperf CPU0.
- CPU 부하 조건은 수신 CPU0·1·2에 각각 SCHED_OTHER stress-ng worker 1개, --cpu-load 100.
- governor와 IRQ affinity를 바꾸지 않았다. 양측 ondemand, 수신 IRQ106의 설정은 0-3이었다.
- 상태는 측정 전후 및 약 1초 간격으로 RAM에 수집하고, 결과를 로컬에 저장했다. 별도 Python 계측과 ping의 비용이 포함된 진단 시험이다.
- 커밋·브랜치 배포·systemd 등록·XDP attach는 하지 않았다. 이번 시험용 iperf/stress 프로세스만 생성·종료했다.

## 주요 결과

배경 CPU 부하가 없을 때 1472B UDP의 400·800·950Mbps 목표를 거의 달성했고, 각 조건 3회에서 관측 손실은 0%였다.
96B UDP의 3000·11000·100000 PPS 목표도 각 조건 3회에서 관측 손실 0%였다. 11000 PPS는 CPU 부하 조건에서도 관측 손실 0%였다.
800Mbps와 CPU 부하를 함께 준 본 측정 3회에서는 손실 0.1752~0.2605%가 관측됐다.
보조 진단 1회에서는 iperf 손실 1867개와 UDP RcvbufErrors 증가 1867개가 일치했다. NIC rx_errors/rx_dropped 증가는 모두 0이었다. 해당 보조 조건의 손실은 UDP 수신 소켓 버퍼에서 발생한 것으로 해석할 수 있다.
수신 NIC IRQ 증가량은 본 측정 24회 모두 CPU0에서만 관측됐다. CPU2 배치가 현재 적용되지 않았다는 실행 증거다. CPU2로 설정을 바꿨을 때의 효과는 이번에 시험하지 않았다.

## 조건별 실제 측정값

| UDP 데이터 크기 | 목표 부하 | 수신 CPU 부하 | 평균 수신 Mbps | 관측 손실률 범위 | 수신 CPU0 평균 점유율 |
|---|---|---|---:|---:|---:|
| 1472B | 400 Mbps | 추가 부하 없음 | 399.962 | 0.0000~0.0000% | 7.7% |
| 1472B | 800 Mbps | 추가 부하 없음 | 799.925 | 0.0000~0.0000% | 16.6% |
| 1472B | 950 Mbps | 추가 부하 없음 | 949.913 | 0.0000~0.0000% | 15.9% |
| 96B | 3,000 PPS | 추가 부하 없음 | 2.304 | 0.0000~0.0000% | 1.8% |
| 96B | 11,000 PPS | 추가 부하 없음 | 8.447 | 0.0000~0.0000% | 4.1% |
| 96B | 100,000 PPS | 추가 부하 없음 | 76.793 | 0.0000~0.0000% | 17.9% |
| 1472B | 800 Mbps | CPU0~2 각 100% 목표 | 798.228 | 0.1752~0.2605% | 99.0% |
| 96B | 11,000 PPS | CPU0~2 각 100% 목표 | 8.447 | 0.0000~0.0000% | 99.1% |

Mbps는 UDP payload 전송률이다. Ethernet/IP/UDP header 등 회선 overhead까지 포함한 사용률과 다르다. CPU 부하 100%는 worker 목표값이며 실제 CPU0 평균은 표의 실측값이다.

## 클럭·온도·throttling

| 장치 | 관측 주파수 범위 | 관측 온도 범위 | 전후 throttling |
|---|---|---|---|
| 수신 craftp | 1.5~2.4 GHz | 41.90~50.70°C | throttled=0x0 (48회 조회) |
| 송신 craftt | 1.5~2.4 GHz | 41.90~51.80°C | throttled=0x0 (48회 조회) |

양측 governor는 측정 전후 모두 ondemand였다. 관측 주파수가 실제로 변했으므로 C08의 주파수 정책 통제와 상태 기록은 여전히 필요하다. 1초 표본은 그 사이의 모든 순간을 나타내지는 않는다.

## RTT 및 해석 제한

시험 종료 후 무부하 ICMP ping 100개의 평균 RTT=0.1621ms, 관측 p99=0.1963ms, 최댓값=0.3280ms였다.
각 부하 run에서도 ICMP ping 약 100개를 함께 기록했다. 상세 p99/최댓값은 CSV와 JSON에 있다. 이는 왕복 ICMP 지연이며 논문의 NIC HW RX→사용자 수신 지연이나 100µs 기준과 직접 비교할 수 없다.
이번 96B는 iperf UDP payload 크기이고 ACE 32B 헤더를 가진 실험 패킷이 아니다. 포트 55201의 한 흐름이므로 세 경로 동시 실험과도 다르다.
10초×3회 진단 결과다. 장시간 무손실이나 deadline 보장을 입증하지 않는다. 일반 UDP 처리량과 ACE 중요 흐름의 tail latency는 별도로 측정해야 한다.

## 측정 당시 확인한 사항 (자동 설정 코드 추가 전)

- C08: 상태 수집 자체는 가능하고 이번 원자료에 전후·구간 기록이 있다. 이 측정 당시에는 ace-run 통합 전이었다. 이 커밋의 monitor helper와 ace-run 연결은 별도 변경이며, 위 성능 수치는 그 변경 전의 기준 자료다.
- C10: 이 기준 측정 당시 실제 IRQ 처리는 CPU0이었다. 자동 IRQ 설정·서비스가 필요한 이유를 확인했다. 이 기준 측정 이후의 CPU2 적용·서비스 검증은 아래에 별도로 기록했다.
- C07/C09: 도구 버전과 NIC 설정을 함께 보관해야 한다. 시험 종료 후 coalescing/ring 및 XDP·잔여 프로세스를 재확인했다.
- CPU 부하에서 UDP socket drop이 발생하므로 손실을 지연 표본에서 지워버리지 말고 송신/수신/기록 수와 함께 집계해야 한다.

## 기준 성능 측정 종료 상태 (16:23, 이후 설정 변경 전)

- craftp: 시험 프로세스 잔여 0개, governor {'/sys/devices/system/cpu/cpufreq/policy0/scaling_governor': 'ondemand'}, IRQ affinity {'106': {'smp_affinity_list': '0-3', 'effective_affinity_list': '0-3'}}.
- craftt: 시험 프로세스 잔여 0개, governor {'/sys/devices/system/cpu/cpufreq/policy0/scaling_governor': 'ondemand'}, IRQ affinity {'105': {'smp_affinity_list': '0-2', 'effective_affinity_list': '0-2'}}.

상세 명령·iperf 원문 JSON·ping 원문·IRQ delta·CPU 표본·온도/주파수·전후 counter는 개별 run JSON에 보존했다.


## 이후 실험에 적용할 설정과 실행 순서

다음 명령은 저장소 최상위에서 실행한다. 수신 Pi의 IRQ 자동 배치 서비스만
부팅 시 활성화하며 송신 Pi에는 서비스를 설치하지 않는다. 기존 README는
수정하지 않았으며, 이 커밋의 준비 동작은 아래와 script 주석에 기록한다.

```bash
# 수신 Pi 최초 1회: 지금 CPU2에 적용하고 다음 부팅에도 자동 적용
sudo bash monitor/install_irq_service.sh install eth0 --receiver
systemctl status ace-rx-irq-affinity@eth0.service
journalctl -u ace-rx-irq-affinity@eth0.service -b

# 수신 Pi 읽기 전용 확인. 패킷 처리 위치는 아래 counter 시험으로 따로 검증
bash monitor/set_irq_affinity.sh eth0 2 --check
sudo bash monitor/verify_irq_affinity.sh eth0 2 60
# 위 60초 동안 송신 Pi에서 eth0 트래픽을 보낸다.

# 필요 시 수신 서비스 자동 실행 해제(현재 affinity를 임의 변경하지 않음)
sudo bash monitor/install_irq_service.sh disable eth0 --receiver
```

양측을 동일한 자동화 버전으로 빌드하고, `config.example.conf`의 빈칸을 채운
동일 설정 파일을 사용한다. 환경 키는 `environment.governor=performance`,
`environment.rx_irq_cpu=2`다. 각 실제 run 시작 전에 양측 governor를 적용하고,
수신 IRQ만 해당 CPU로 배치한다. 현재 요청값뿐 아니라 effective affinity도
일치해야 시작한다. `socket-only`는 governor·IRQ를 변경하지 않는다.

```bash
make -C udp_socket
make -C BPF
make -C experiment
./experiment/ace-run plan --config experiment/run.conf
# 수신 Pi에서 먼저 실행
sudo ./experiment/ace-run receiver --config experiment/run.conf
# 송신 Pi에서 같은 설정으로 실행
sudo ./experiment/ace-run sender --config experiment/run.conf
```

각 역할의 각 run에 `system-before.txt`, `system-after.txt`가 추가된다.
주파수·온도·throttling·IRQ·NIC coalescing/ring/counter·도구 버전을 캡처 전후에
수집한다. 미지원 항목은 0이 아니라 unavailable/명령 종료 상태로 남긴다.
캡처 중에는 이 helper를 호출하지 않으므로 per-packet 경로에 센서/외부 명령을
추가하지 않는다. 전후 표본만으로 모든 순간의 주파수 고정이나 hard deadline을
보증하지 않는다.

IRQ 설정 helper의 실패는 성공으로 숨기지 않는다. 여러 IRQ 중 일부 설정에
실패하면 바꾼 요청값을 복원한다. 준비 단계에서 IRQ 적용 실패가 발생하면
그 단계에서 바꾼 governor도 복원한다. 정상 실험 준비 이후의 performance와
IRQ 배치는 다음 run에도 유지하며, governor를 부팅 서비스에서 고정하지는 않는다.

## 결과 파일 보존

이 폴더에는 조건별 CSV, 집계·개별 run 수치 JSON과 원자료 SHA-256을 보관한다.
원본 iperf/ping/센서 로그 전체는 작업 폴더의 `통신성능_사전점검_증거.zip`에 있다.
이 결과는 24회 기준 점검과 별도 1회 진단을 구분하며, 설정 변경 후 ACE 본시험의
측정값으로 재사용하지 않는다.

## 추가한 코드 및 실제 설치 검증 (16:54 이후)

- 수신 Pi의 임시 RAM 복사본에서 `tests/run_offline.sh` 전체 통과. Python suite 36개·41개·5개 통과와 빌드/스크립트 검사를 포함한다. 실제 저장소 경로의 자동화 브랜치 전체를 배포한 것은 아니다.
- 양측 `prepare_env.sh`의 performance 적용·읽기 확인과 `capture_state.sh`의 `capture_complete=1`을 확인했다. 검증 후 양측 governor는 기존 ondemand로 복원했다. 실제 실험에서는 ace-run이 각 run 전에 performance를 적용한다.
- 사용자 승인에 따라 수신 Pi에는 IRQ 서비스와 helper를 실제 설치하고 활성화했다. `ace-rx-irq-affinity@eth0.service`는 enabled, active(exited), ExecStart 종료값 0이었다.
- 자동 탐지한 수신 IRQ106의 requested/effective affinity 모두 `2`다. 서비스 설치본과 저장소 helper의 SHA-256은 `4330097eee05fa3ae64c8bf828ef79261c8a5048075126dd5fb5f45625612a8b`로 같다.
- 수신 Pi에서 eth0 경유 ping 20개를 송신 Pi로 보낸 구간의 IRQ106 증가량은 CPU0·1·2·3 순으로 `[0, 0, 120, 0]`이었다. CPU2에서 IRQ가 실제 처리됨을 확인한 연결 점검이며, 위 24회 성능 시험을 CPU2로 재실행한 결과는 아니다.
- 송신 IRQ105는 준비 전후 `0-2`로 유지됐다. 송신 Pi에는 IRQ 서비스를 설치하지 않았다.
- 재부팅 자체는 수행하지 않았다. 부팅 자동 실행 등록과 현재 서비스 실행 성공을 검증했으며, 향후 재부팅 후에는 `--check`와 트래픽 counter 검증을 다시 수행한다. NIC 드라이버 재적재나 별도 관리 도구의 이후 affinity 변경까지 감시하는 서비스는 아니다.
- README 파일은 변경하지 않았다. 신규 도구는 기존 monitor 구조에 두고, 실행 제어기와 기존 테스트 흐름에 연결했다.
