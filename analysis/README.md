# Offline analysis

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
capture host와 같은 native C ABI와 byte order에서 분석한다.

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

`--phc-calibration`은 여러 번 지정할 수 있고 selected 표본을 합쳐 시간순
선형 보간한다. 기본은 calibration 범위 밖 clock-crossing metric을
제외하고 before/after/out-of-range 건수와 거리를 요약한다.
`--allow-endpoint-clamp`는 탐색적 분석용이며, 최종 실험은 직전/직후
calibration selected 범위가 전체 측정 구간을 bracket해야 한다.

Joined CSV와 summary JSON을 `O_EXCL`로 생성하고 파일/디렉터리를
`fsync()`한 뒤, 모든 입력과 두 출력의 크기/SHA-256을 묶은
`summary.json.complete`를 마지막에 만든다. `--completion-marker PATH`로
경로를 바꿀 수 있으며, marker가 없는 CSV/JSON은 후속 aggregate의
완결된 run으로 취급하지 않는다.

## 반복 run aggregation

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

아래 도구는 **측정 종료 후 VMware 등 분석 호스트에서** 실행한다. Pi에서
패킷을 송수신하는 동안 실행하지 않는다. 두 Pi에서 회수한 결과를 새 경로에
결합한다. 각 입력은 단일 `run.json` 디렉터리 또는 `runs/`가 있는 suite다.

```bash
python3 analysis/analyze_experiment.py collect \
  --receiver-dir received/receiver --sender-dir received/sender \
  --output results/collected
python3 analysis/analyze_experiment.py --suite-dir results/collected
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
수신 프로그램의 `receiver-flowN.json`과 `monitor/mpstat.log`,
`monitor/network.log`도 함께 보존·분석한다.

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
