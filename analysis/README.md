# Offline analysis

두 Pi에서 회수한 기록을 Linux·Windows의 Python으로 분석한다.
아래 명령은 Linux 표기이며 Windows에서는 `python3` 대신 `python`을 사용한다.

## 단일 run join

`join_results.py`는 한 run의 ingress/CPUMAP/AF_XDP/UDP record를
`flow_id + sequence`로 full outer join한다. `--manifest`와 ingress CSV,
하나 이상의 PHC calibration CSV는 필수이고, 해당 경로를 측정했을 때만
CPUMAP/AF_XDP/UDP 입력을 추가한다. `--udp-native`는 여러 번 지정해
PASS와 CPUMAP 수신 파일을 함께 전달할 수 있다. 같은 파일의 중복 지정이나
두 UDP 파일이 같은 `flow_id + sequence`를 소유하는 입력은 거부한다.

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

Manifest는 `manifest_version=1`과 안전한 `run_id`를 가져야 한다. 모든 입력과
출력은 manifest가 있는 resolved run directory 안의 일반 파일이어야 하며,
기존 파일과 symbolic link를 출력으로 덮어쓰지 않는다. UDP binary는
56-byte `sample_record` 배치와 byte order를 맞춰 읽는다. 현재 Pi의 little-endian
기록은 64-bit Linux·Windows Python에서 이 배치로 분석한다.

Analyzer는 raw row를 버리지 않고 source presence, requested path와
`inconsistency_flags`를 남긴다. 다음 의미 검증을 통과한 timestamp만
metric에 사용한다.

- ingress/CPUMAP/AF_XDP의 requested path, metadata flag와 timestamp/error 조합
- CPUMAP record의 `CPUMAP_SEEN` 및 ingress path 일치
- AF_XDP metadata/queue/timestamp가 ingress record와 일치하는지
- UDP flag/HW timestamp 조합, XSK path와 UDP sink 충돌 여부
- 하나의 key에 AF_XDP와 UDP userspace record가 모두 있는 모호한 경우

결과 metric은 `xdp-hw`, `cpumap-xdp`, `user-cpumap`, `user-xdp`,
`user-hw`다. `user-cpumap`은 CPUMAP remote program 관측 이후부터 UDP
userspace 관측까지를 뜻하며 순수 socket wakeup 시간으로 해석하지 않는다.

HW 수신 시각은 **패킷별로 한 번** PHC→MONOTONIC으로 변환한다. 최초 XDP
시각에서 보간한 offset을 사용하고, XDP 관측이 없는 UDP 패킷은 사용자 수신
시각의 offset을 사용한다. `hw_rx_mono_ns`를 `xdp-hw`와 `user-hw`에 공통으로
사용한다. `hw_calibration_reference`, `hw_calibration_reference_ns`,
`hw_calibration_offset_ns`, `hw_rx_source`에 실제 선택을 남긴다. 기존
`calibration_*_at_xdp/user` 관련 열은 각 관측 시각의 보정 범위 진단값이다.

| CSV 구간 | CPUMAP | PASS · AF_XDP |
| --- | --- | --- |
| `d1_ns` | `xdp_hw_ns` | `xdp_hw_ns` |
| `d2_ns` | `cpumap_xdp_ns` | `0` |
| `d3_ns` | `user_cpumap_ns` | `user_xdp_ns` |

`segment_residual_ns = user_hw_ns - (d1_ns + d2_ns + d3_ns)`를 같은 패킷에서
계산한다. `segment_status`는 `complete`(일치), `incomplete`(관측 부족),
`invalid`(음수 구간 또는 메타데이터 오류), `mismatch`(합계 불일치)이며
summary의 `decomposition`과 flow별 요약에 건수·최대 절대 잔차를 기록한다.
CPUMAP 관측 누락을 0으로 채우지 않는다. 총지연 p99는 패킷별 `user-hw`에서
계산하며 구간별 p99를 합산하지 않는다.

새 join summary는 schema 4다. 독립 집계기는 기존 schema 3도 읽으며 서로 다른
HW 보정 정책의 run을 섞어 집계하지 않는다. 통합 분석에서 이전 정책의 캐시를
만나면 새 수집 디렉터리에서 다시 분석하도록 안내한다. 원본·기존 분석은 보존한다.

`--phc-calibration`은 여러 번 지정할 수 있고 selected 표본을 합쳐 시간순
선형 보간한다. 기본은 calibration 범위 밖 clock-crossing metric을
제외하고 before/after/out-of-range 건수와 거리를 요약한다.
`--allow-endpoint-clamp`는 탐색적 분석용이며, 최종 실험은 직전/직후
calibration selected 범위가 전체 측정 구간을 bracket해야 한다.

Joined CSV와 summary JSON을 기존 파일을 덮어쓰지 않고 생성해 파일을
`fsync()`한 뒤, 모든 입력과 두 출력의 크기/SHA-256을 묶은
`summary.json.complete`를 마지막에 만든다. `--completion-marker PATH`로
경로를 바꿀 수 있으며, marker가 없는 CSV/JSON은 후속 aggregate의
완결된 run으로 취급하지 않는다.
Windows에서는 binary I/O와 LF 출력을 사용하고, Linux에서는 디렉터리도 `fsync()`한다.

## 반복 run aggregation

통합 보고서는 `all-pass`, `pass-xsk`(PASS/PASS/AF_XDP), `mixed`를 각각 표시한다.
반복 집계는 `(case_id, mode)` 단위로 수행해 단독 측정과 네 부하 조건을 각각 집계한다.
`run.json`의 수신 CPU 허용 집합(`rx_cpus`)과 경로를 구간별 표에 표시한다.
all-pass·pass-xsk의 앱은 `0,1,2,3`을 공유한다. 활성 CPUMAP 중요1이 있는 mixed에서는
중요1 앱이 `3`, PASS·AF_XDP 앱이 `0,1,2`를 사용한다.
허용 CPU 집합이 다른 실행은 별도 조건으로 보존한다. 기존 scalar `rx_cpu` 기록도 읽는다.

all-pass → pass-xsk는 공유 영역의 AF_XDP 추가 효과를, pass-xsk → mixed는
중요 흐름의 커널 처리·수신을 전용 CPU로 분리한 효과를 비교한다.
두 비교 모두 총 네 CPU를 사용하며, CPU 부하 워커 수는 세 개로 유지한다.
실제 일반 CPU 영역과 예약 상태, 워커 수·강도를 run 조건과 함께 읽는다.
CPU 부하 끔/켬 × iperf3 배경 부하 끔/고부하의 네 조건에서 세 구성의
총지연·기한 초과·손실·일반 처리량을 나란히 읽고,
같은 패킷의 d₁·d₂·d₃로 변화가 나타난 구간을 설명한다.
세 계측 흐름은 각각 1,000 PPS다. 일반 9000의 수신 PPS·손실과 함께
수신 측정 구간의 NIC 송수신량·패킷 수를 읽는다. 준비된 계획의 배경 부하는
iperf3 UDP payload 기준 855 Mbps이며, NIC 카운터는 계측·배경 트래픽을 포함한다.

HTML의 **구성별 CPU 사용 영역** 표에는 전체 CPU, 일반 영역과 CPUMAP 전용 CPU를 표시한다.
`report.json`에 전체 사용 가능 CPU(`receiver_available_cpus`), 실제 공유 집합과 예약 여부를
보존하고, `cpu-partition-active.json`·`cpu-partition-restored.json`의 실제 예약·반환 기록과
해시를 함께 남긴다. 완료된 동적 CPUMAP 실행은 두 상태 기록을 대조한다.

`aggregate_runs.py`는 같은 조건의 독립 run을 묶어 deadline과 tail
latency를 계산한다. 최소 두 개의 join completion marker가 필요하다.
`--deadline-ns`를 생략하면 지연 통계만 계산하고 deadline 통계는 null로 둔다.
`--by-flow`는 flow별 분포와 독립 run 신뢰구간을 각각 계산한다.
`--flow-deadline 2=100000 --flow-deadline 3=50000`처럼 flow별 deadline을
줄 수 있으며, 값이 지정되지 않은 flow에 임의 deadline을 적용하지 않는다.

```bash
python3 analysis/aggregate_runs.py \
  --run results/run01/summary.json.complete \
  --run results/run02/summary.json.complete \
  --deadline-ns 100000 \
  --output-csv results/rt-load50.aggregate.csv \
  --summary-json results/rt-load50.aggregate.json
```

입력은 CSV나 summary가 아니라 `join_results.py`의 completion marker다.
도구는 marker에 기록된 CSV/summary SHA-256, `run_id`, manifest, run
directory, summary와 joined CSV의 metric 의미를 재검증한다. Manifest parameter,
interface/kernel/source/artifact cohort 또는 calibration 정책이 다른 run은 한
분포로 합치지 않는다. 비교할 실험 조건은 각각 따로 aggregate한다.

각 metric에는 전체 run 수, 표본이 있는 run 수, deadline miss가 발생한
run 수와 pooled packet 수, 음수 표본 수, p50/p95/p99/관측 최댓값,
deadline miss 수·비율이 기록된다. Percentile은 nearest-rank이고
`latency > deadline_ns`일 때 miss다.

신뢰구간의 독립 단위는 packet이 아니라 run이다. 각 run의
p50/p95/p99와 deadline miss rate에 동일한 가중치를 주고, 그 평균에
양측 95% Student-t 신뢰구간을 계산한다. Packet을 한데 모은 pooled
통계에는 독립성을 과장할 수 있는 신뢰구간을 붙이지 않는다. 해당
metric을 기록한 독립 run이 1개뿐이면 평균은 기록하지만 표본
표준편차와 신뢰구간 양 끝은 `null`이다. 표본 표준편차가 0이면
신뢰구간도 그 값으로 수렴하며, 비율 신뢰구간은 임의로 0..1에
잘라내지 않는다.

출력 CSV와 JSON은 기존 파일을 덮어쓰지 않으며, 두 파일을
`fsync()`한 뒤 `SUMMARY_JSON.complete` marker를 마지막에 만든다.

## 두 Pi 결과 회수와 실험 보고서

아래 도구는 **측정 종료 후 Linux·Windows 분석 호스트에서** 실행한다. Pi에서
패킷을 송수신하는 동안 실행하지 않는다. 두 Pi에서 회수한 결과를 새 경로에
결합한다. 각 입력은 단일 `run.json` 디렉터리 또는 `runs/`가 있는 suite다.

```bash
python3 analysis/analyze_experiment.py collect \
  --receiver-dir received/receiver --sender-dir received/sender \
  --output results/collected
python3 analysis/analyze_experiment.py --suite-dir results/collected
```

Windows PowerShell에서는 한 조건의 송수신 결과를 다음과 같이 회수·분석한다.
`received/receiver`, `received/sender`는 같은 조건에서 가져온 역할별 폴더다.

```powershell
python analysis/analyze_experiment.py collect --receiver-dir received/receiver --sender-dir received/sender --output results/collected
python analysis/analyze_experiment.py --suite-dir results/collected
```

단일 run은 `--run-dir results/collected-run`으로 분석한다. 기본 출력은
입력 디렉터리의 `analysis-report/report.html`과 `report.json`이다.
HTML은 SVG 그래프와 표를 포함해 네트워크나 외부 라이브러리 없이 열린다.
`--output-dir`로 보고서 디렉터리를 바꿀 수 있다.

`collect`는 송수신 측의 run ID와 case·mode·duration·flows·load·config가
같은지 검사한다. 출력 디렉터리가 이미 있으면 중단한다. 수신 원본을 그대로
복사하고 송신 원본은 `sender/`에 보존하며 `sender-flowN.json`을 분석 입력으로
연결한다. 모든 복사본은 크기/SHA-256으로 검증하고 `collection.json`에 기록한다.
실패·불완전한 결과는 실패 상태로 남기며 성능 집계에 넣지 않는다.

수신 run에는 `run.json`, `manifest.json`, `.complete`,
`xdp-ingress_records.csv`, `xdp-cpumap_records.csv`, 경로별
`udp-flowN.bin` 또는 `afxdp.csv`, `phc-before.csv`, `phc-after.csv`를 둔다.
송신 run에는 같은 조건의 `run.json`, `.complete`, `sender-flowN.json`을 둔다.
수신 프로그램의 `receiver-flowN.json`과 `monitor/cpu-samples.csv`,
`monitor/network-samples.csv`를 함께 보존·분석한다. 기존 `monitor/mpstat.log`,
`monitor/network.log`도 원본에 포함되어 있으면 보존한다.

새 실행에서는 네이티브 CPU 관측기가 `monitor/cpu-samples.csv`에 수신 Pi의
MONOTONIC 시각과 CPU별 누적 실행 시간을 1초 간격과 종료 시점에 남긴다. 분석기는 같은
Pi의 패킷 기록에서 **최초 XDP → 최종 수신 관측(XDP·CPUMAP·사용자 수신)** 창을
구하고 그 구간의 CPU 사용률을 계산한다. 사용자 공간에 도달하지 못한 마지막
패킷도 XDP/CPUMAP 관측이 있으면 창에 포함한다. 준비 대기·저장 시간은 제외된다.

전체 표본 구간은 누적 카운터 차분을 그대로 사용하고, 측정창 경계에 걸친
구간은 겹친 시간 비율로 차분을 가중한다. CPU 사용률은 전체 시간에서 idle과
iowait를 뺀 비율이며 guest 시간은 중복 계산하지 않는다. 창의 시작·종료·실제
길이, 표본 간격과 포함 비율은 `cpu_measurement`, CPU별 결과는 `cpu`에 저장한다.
전체 창을 덮는 표본이 있는 CPU만 결과를 제공한다. 소켓 운영 검증은 최초·최종
사용자 수신 시각을 사용한다.

기존 `mpstat.log`의 전체 관측 평균은 `cpu_observation`에 보존한다. 새 CPU 표본이
없는 이전 결과는 측정창 CPU 값을 비워 두며, 전체 관측 평균을 측정창 평균으로
대체하지 않는다. HTML의 **수신 측정 구간 CPU 사용률**과 조건별 `cpu_busy_pct`는
새 측정창 결과만 집계한다.

NIC 수집은 같은 C 관측기 `udp_socket/bin/cpu_sample`에
`--interface eth0 --network-output FILE`을 전달해 켠다. 실행기는 이를 자동 적용하고
`/proc/net/dev`의 누적 카운터를 `monitor/network-samples.csv`에 저장한다.
CPU와 같은 MONOTONIC 시각 기준으로 1초 간격 및 종료 시점에 기록한다.

분석기는 CPU와 **동일한 수신 측정 구간**에서 카운터 증가량을 계산한다.
구간 내부 차분은 그대로 합산하고, 경계 차분은 겹친 시간 비율로 가중한다.
NIC 결과는 `network[인터페이스]`, 측정창·표본 간격·포함 비율과 계산 방식은
`network_measurement`에 저장한다.

| NIC 결과 | 단위와 의미 |
|---|---|
| `rx_bytes`, `tx_bytes` | 수신 측정 구간의 NIC 송수신 바이트 증가량 |
| `rx_packets`, `tx_packets` | 같은 구간의 패킷 수 증가량 |
| `rx_bps`, `tx_bps` | 바이트 증가량 × 8 ÷ 측정 구간 길이 — bit/s |
| `rx_pps`, `tx_pps` | 패킷 증가량 ÷ 측정 구간 길이 — packet/s |
| `rx_errors`, `rx_dropped`, `tx_errors`, `tx_dropped` | 같은 구간의 NIC 오류·드롭 증가량 |

HTML의 **수신 측정 구간 NIC 트래픽** 표가 이 결과를 표시한다.
기존 SAR `network.log` 평균은 `network_observation`에 `scope="full-monitor"`로
보존한다. 새 NIC 표본이 없는 이전 실행의 측정 구간 값은 비워 두고 수집 상태를 남긴다.

보고서는 같은 조건의 반복 run을 flow별로 묶는다. 기존 nearest-rank 분위수와
동일 run 가중치의 양측 95% Student-t 신뢰구간을 사용한다. 한 run만 있으면
지연 통계는 표시하고 CI는 null로 둔다. manifest의 반복 공통 조건과
소프트웨어/환경 정보가 다른 run은 묶지 않는다.

송신 stats의 `expected_count`, 실제 `sent`, `errors`를 원본 수신 packet ID와
연결한다. 손실률의 분모는 실제 송신 수이며 계획했지만 송신하지 않은 수는
`unsent`로 따로 남긴다. PPS 분모는 송신 프로그램이 기록한 첫 송신 시도부터 마지막
송신 종료까지의 `elapsed_ns`를 사용한다. 종료 후 drain 도착분은 수신
개수에 포함된다. 지연 계산에 필요한 시계 보정과 packet ID의 원본 검증은 유지한다.

`mode=socket-only`는 송수신 운영 검증으로 표시하고 HW 지연 통계에 넣지 않는다.
`purpose=warmup`으로 따로 보관한 run도 집계에서 제외한다. 일반 run의 준비 대기는
`warmup_s`에 남기며 분석기가 수신 표본을 임의로 잘라내지 않는다. `.failed`나
완료 표시가 없는 run도 보고서 실행 상태 표에 남는다.

## 오프라인 test

```bash
python3 -m unittest analysis.tests.test_join_results \
  analysis.tests.test_aggregate_runs analysis.tests.test_experiment
```
