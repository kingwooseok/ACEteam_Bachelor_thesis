# BPF 측정 프로그램 자동 실행

`make -C BPF`는 실행 중인 Linux 커널의 BTF로 BPF object와 skeleton을 만들고 `xdp_loader`, `afxdp_recv`를 빌드한다. clang의 BPF target, bpftool, libbpf·libxdp 개발 패키지가 필요하다.

## 경로 정책

```sh
BPF/xdp_loader --output-dir "$RUN_DIR" --records "$TOTAL_RECORDS" \
  --mode mixed --ready-file "$RUN_DIR/loader.ready.json" eth0
```

- `--mode mixed`가 기본값이다. UDP/9000은 PASS, UDP/9001은 CPUMAP CPU 3, UDP/9002는 AF_XDP COPY로 전달한다.
- `--mode all-pass`는 세 포트의 실험 패킷을 동일한 파서·타임스탬프·ingress map으로 기록한 뒤 모두 PASS로 전달한다. `flow_id`, `sequence`, 목적 포트와 패킷 본문은 유지한다. 기록의 `requested_path`는 PASS(0)다. 세 UDP 수신기를 실행하며 AF_XDP 수신기는 실행하지 않는다.
- 모드는 BPF load 전에 read-only 설정으로 고정하고 runtime map의 `ALL_PASS` flag에도 남긴다. runtime ABI는 5이며 wire header는 기존 32바이트, runtime 구조는 기존 56바이트다.
- 단일 RX queue 0, CPUMAP CPU 3·queue 256, AF_XDP COPY·4096 UMEM frames·RX ring 2048·batch 64를 유지한다. AF_XDP는 scatter/gather를 요청하지 않으며 다중 descriptor 패킷을 수신 오류로 처리한다.

`--records`는 ingress 및 CPUMAP HASH의 용량이다. **세 흐름의 예상 총 패킷 수** 이상으로 설정한다. 기본 262144, 상한 1048576이며 기존 실행 동안 map을 교체하거나 기록을 덮어쓰지 않는다. 더 긴 측정은 이 용량 안의 개별 run으로 구성한다.

## 준비 완료와 시작 순서

1. 고유한 run 디렉터리를 만들고 loader를 실행한다.
2. `loader.ready.json`의 유효 JSON과 해당 PID 생존을 확인한다.
3. mixed에서는 UDP 수신기 두 개와 다음 AF_XDP 수신기를 실행한다. all-pass에서는 UDP 수신기 세 개를 실행한다.
4. 모든 수신기의 ready JSON과 PID 생존을 확인한 뒤 송신을 시작한다.

```sh
BPF/afxdp_recv --output "$RUN_DIR/afxdp.csv" \
  --stats-json "$RUN_DIR/afxdp.stats.json" \
  --ready-file "$RUN_DIR/afxdp.ready.json" --drain-ms 100 eth0 0
```

AF_XDP `--records`를 생략하면 loader의 용량을 사용한다. 수신기는 loader 소유권, runtime ABI, attached program, map schema, RX queue, socket 등록을 검사하고 메모리 할당·잠금·prefault를 완료한 뒤 ready를 발행한다.

`--ready-file`은 완성된 JSON을 같은 디렉터리에서 원자적으로 공개한다. 기존 파일은 덮어쓰지 않으며 충돌하면 비정상 종료한다. stdout도 즉시 flush한다. 준비 파일은 실행 기록으로 남는다. 준비 파일만 보고 죽은 프로세스를 정상으로 간주하지 않도록 PID와 자식 종료 상태를 함께 검사한다.

## 종료와 결과 확정

1. 송신기의 패킷 전송 완료를 기다린다.
2. 수신기에게 SIGTERM을 보내 잔여 수신 처리를 시작한다.
3. 자동 실행에서는 모든 수신기의 완료 통지를 받은 뒤 SIGUSR1을 보내 저장을 해제하고 **수신기 종료 코드**를 기다린다.
4. 모든 수신기의 종료 후 loader에 SIGTERM을 보내고 **loader 종료 코드**를 기다린다.
5. 모든 프로세스가 성공으로 종료하고 결과 파일이 저장됐을 때 분석을 시작한다.

AF_XDP는 SIGINT/SIGTERM 이후 `--drain-ms` 동안 남은 RX descriptor를 계속 소비한다. 기본 100 ms, 범위 0~60000 ms이며 고정된 전체 유예 시간이다. 새로운 패킷이나 반복 신호가 마감 시각을 연장하지 않는다. 0이면 유예 없이 종료한다. 배치 처리 한 번을 마친 뒤 deadline을 검사하므로 실행 중인 배치는 끝까지 반환한다.

`--completion-fd N --defer-flush`는 여러 수신 경로의 저장 시점을 맞추는 자동 실행 옵션이다. `N`은 부모가 상속한 쓰기 가능한 pipe descriptor(3 이상)다. 수신·drain이 끝나면 성공은 숫자 byte 0, 오류는 byte 1을 쓰고 pipe를 닫는다. 이후 SIGUSR1까지 CSV·통계 출력을 보류한다. SIGTERM은 drain을 시작하며 저장 대기를 해제하지 않는다. 먼저 도착한 SIGUSR1도 기억하므로 실패 정리는 SIGTERM과 SIGUSR1을 함께 보낸 뒤 종료 코드를 회수한다. AF_XDP 커널 통계와 종료 시각은 저장 대기 전 수신 종료 시점에 고정한다. 옵션 없는 수동 실행은 drain 이후 곧바로 저장한다.

수신 기록은 사전 할당한 메모리에 쌓으며 측정 루프에서는 CSV를 쓰지 않는다. 수신 종료 후 CSV를 한 번 저장하고 `fflush`·`fsync`·`fclose` 성공을 검사한다. `--stats-json`도 종료 후 저장한다. 여기에는 수신·기록 수, malformed·metadata·overflow·UMEM 오류, kernel RX drop/ring 통계, 시작·종료 시각, drain 완료 여부, 종료 사유와 종료 코드가 들어간다. 기록 용량 초과나 저장·정리 오류는 0이 아닌 종료 코드다. 손실 계수는 결과에 남고 분석 단계에서 평가한다.

loader는 살아 있는 AF_XDP 소유권이 있으면 detach와 결과 확정을 거부한다. 정상 종료에서는 자신의 XDP를 detach한 뒤 CPUMAP 잔여 처리를 기다리고 `ingress_records.csv`, `cpumap_records.csv`, `stats.csv`를 저장한다. 하드웨어 타임스탬프 설정 복원과 pin 정리까지 성공한 **exit 0**이 결과 확정 신호다. 파일 생성만으로 성공 처리하지 않는다.

실패 시 pin이 남으면 원래 loader와 AF_XDP 수신기가 종료된 상태에서 다음 복구 명령을 사용한다. 복구 출력은 새로운 디렉터리로 저장한다.

```sh
BPF/xdp_loader --recover --cleanup-pins --output-dir "$RECOVERY_DIR" eth0
```

`--recover`에는 `--ready-file`을 지정하지 않는다. 기존 프로그램·map·socket·결과 파일을 다른 실행이 차지하면 교체하지 않고 오류로 종료한다. SIGKILL은 메모리 수신 기록의 flush를 실행하지 않으므로 정상 종료 수단으로 사용하지 않는다.

## 오프라인 검사

```sh
python3 -m unittest discover -s tests -p test_bpf_automation.py -v
```

경로 정책과 drain deadline 경계, 실제 AF_XDP 완료 통지·신호 대기 함수의 저장 차단과 조기 해제·pipe 오류를 검사한다. 빌드된 BPF 실행 파일이 있으면 CLI 오류 처리와 출력 충돌 보존도 검사한다. 하드웨어 수신·metadata·PHC 검증은 포팅한 Pi에서 수행한다.
