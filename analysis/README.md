# Offline analysis

## 단일 run join

`join_results.py`는 한 run의 ingress/CPUMAP/AF_XDP/UDP record를
`flow_id + sequence`로 full outer join한다. `--manifest`와 ingress CSV,
하나 이상의 PHC calibration CSV는 필수이고, 해당 경로를 측정했을 때만
CPUMAP/AF_XDP/UDP 입력을 추가한다.

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

## 오프라인 test

```bash
python3 -m unittest analysis.tests.test_join_results \
  analysis.tests.test_aggregate_runs
```
