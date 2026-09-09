# macb Native XDP 전체 포팅 및 RT 커널 설정 패치

2026-09-10 현재 소스와 보관된 커널 설정을 기준으로 생성했다.
`드라이버 포팅한 그 모든것을 담아.md`의 초기 수동 포팅 7개 커밋과
이후 미커밋 드라이버 변경을 모두 포함한다.
업로드 전 검토 과정에서 추가한 한국어 설명 주석도 반영했다. 초기 7개 커밋은
원본을 유지하며, 이후 주석은 0008과 통합 패치에 포함한다. 이 주석 보강에서
실행 코드와 커널 설정은 변경하지 않았다.

## 가장 간단한 적용 방법

정확한 커널 기준 커밋:

```text
Raspberry Pi Linux rpi-6.18.y / Linux 6.18.46
16f1da3c4e94437449d6aa151589ca0ad4b388bb
```

`kernel-and-config.patch` 하나에 드라이버 전체와 최종 커널 설정이 들어 있다.
설정은 `arch/arm64/configs/ace_thesis_rt_defconfig`라는 새 파일로 추가된다.
이 파일은 최소 defconfig가 아니라 현재 `.config` 전체를 담은 스냅샷이다.

현재 작업 트리와 별개로 위 기준 커밋에서 새 작업 디렉터리를 만드는 예:

```bash
PATCH_DIR=/home/craftp/ACEteam_Bachelor_thesis/patches/macb-xdp-20260910
cd /home/craftp/rt-experiment/linux-6.18-rt
git worktree add --detach ../linux-6.18-macb-reproduce \
  16f1da3c4e94437449d6aa151589ca0ad4b388bb
cd ../linux-6.18-macb-reproduce

git apply --check "$PATCH_DIR/kernel-and-config.patch"
git apply "$PATCH_DIR/kernel-and-config.patch"
make ARCH=arm64 ace_thesis_rt_defconfig

# Raspberry Pi 5에서 커널/모듈/DT 빌드
make -j4 Image.gz modules dtbs
```

다른 장치에서는 `PATCH_DIR`와 커널 저장소 위치를 맞춘다. 브랜치 이름만 같아도
커밋이 다르면 patch context가 달라질 수 있으므로 위 기준 커밋을 사용한다.
이 안내는 소스 적용과 빌드까지이며 설치·부팅 설정은 별도 단계다.

현재 장치에는 위 경로의 `linux-6.18-macb-reproduce` 작업 디렉터리를 실제로
만들어 패치와 설정을 적용해 두었다. 이 디렉터리를 계속 사용할 때는
`git worktree add`와 `git apply`를 반복하지 않고 빌드 단계부터 진행한다.

## 파일 구성

| 파일 | 내용 |
|---|---|
| `kernel-and-config.patch` | 권장: 드라이버 전체 + 최종 RT 설정 파일 |
| `macb-xdp-full.patch` | 기준 커밋에서 현재까지 드라이버만 합친 패치 |
| `series/0001-*.patch` ~ `0007-*.patch` | 초기 수동 포팅 커밋 원본, 작성자·설명 유지 |
| `series/0008-*.patch` | 이후 미커밋 드라이버 변경 |
| `kernel.config` | 현재 `.config` 전체 원본 |
| `config/before-rt.config` | `.config.backup-v8-4k` 원본 |
| `config/rt-before-xsk.config` | `.config.backup-pre-xsk` 원본 |
| `config/0001-before-rt-to-rt.patch` | 보관된 non-RT 설정 → RT 설정 |
| `config/0002-enable-af-xdp.patch` | RT 설정 → 현재 AF_XDP 활성화 설정 |
| `config/config-full.patch` | 보관된 non-RT 설정 → 현재 설정의 통합 diff |
| `boot/before-rt/` / `boot/current/` | firmware `config.txt`와 `cmdline.txt`의 이전/현재 원본 |
| `boot/boot-settings.patch` | RT 이전 백업 → 현재 부팅 설정의 참고용 diff |

통합 패치와 분할 패치는 같은 변경의 대안이므로 중복 적용하지 않는다.
BPF 프로그램·UDP 도구·분석 코드는 `ACEteam_Bachelor_thesis` 저장소의
별도 application 소스이며 이 커널 패치의 대상은 아니다.

## 드라이버 변경 내역

초기 포팅의 마지막 커밋은
`f934dc812d686488da3cfe1cba31a4e28e60e9b8`이다.

| 순서 | 원래 커밋 | 변경 |
|---|---|---|
| 1 | `c520b905a460` | RX 버퍼 할당을 link-up에서 open으로 이동 |
| 2 | `aa19c8495b99` | RX 버퍼 표현 일반화 |
| 3 | `b9ae9a455248` | page_pool 및 multi-descriptor RX |
| 4 | `862b05223b8c` | TX 버퍼 구조 이름 일반화 |
| 5 | `8bf757177434` | TX 버퍼 타입 구분 |
| 6 | `9088efa70019` | Native XDP PASS/DROP/TX/REDIRECT |
| 7 | `f934dc812d68` | `ndo_xdp_xmit` |
| 8 | 미커밋 소스 | HW timestamp metadata, metadata 보존, multi-descriptor DROP/counter, 공통 TX kick |

설명 주석을 포함한 전체 드라이버 변경은 다음 4개 파일의 `+986 / -264`다.

```text
drivers/net/ethernet/cadence/Kconfig
drivers/net/ethernet/cadence/macb.h
drivers/net/ethernet/cadence/macb_main.c
drivers/net/ethernet/cadence/macb_ptp.c
```

포팅 원리를 읽을 때는 `macb_main.c`의 다음 주석을 참고한다.

- `gem_create_page_pool()`: page 재사용, DMA 방향, XDP core의 page 반환 경로.
- `gem_xdp_setup()`: XDP 활성 여부가 바뀔 때 RX 메모리를 다시 준비하는 이유.
- `macb_xdp_buff` / `gem_xdp_run()`: 현재 RX descriptor와 metadata callback의
  연결, PASS/REDIRECT/TX/DROP에 따른 버퍼 처리.
- `gem_rx()`: multi-descriptor frame을 전체 DROP하는 조건과 metadata의 SKB 보존.
- `macb_tx_kick()`: SKB/XDP 공통 TX 시작 신호와 RP1 PCIe readback.

`macb_ptp.c`의 `gem_ptp_rxstamp_ns()`에는 HW timestamp를 공통 helper로
분리한 이유와 PHC 시계의 의미를 설명했다. RX descriptor 포인터를 다른 CPU나
사용자 공간에 metadata로 전달하는 것이 아니라, BPF가 읽은 숫자 값을 metadata에
복사해서 전달한다는 점이 핵심이다.

변경을 단계별로 적용하려면 기준 커밋에서 다음을 실행한다. 모든 파일은
`git apply`로 적용 가능하다. 0001~0007은 `git format-patch` 형식이고,
0008은 미커밋 변경의 일반 diff이므로 전체 8개를 `git am`에 넣지 않는다.

```bash
for port_patch in "$PATCH_DIR"/series/*.patch; do
    git apply "$port_patch" || break
done
```

이미 초기 7개 커밋까지 적용된 `f934dc812d68` 상태라면 0008만 적용한다.
현재 원본 작업 트리에는 0008 변경까지 이미 있으므로 다시 적용하지 않는다.

## 보관된 커널 설정의 차이

별도 작업 기록은 없었지만 같은 Linux 6.18.46의 설정 백업 두 개가 남아 있었다.
패치는 이 백업 사이의 실제 차이를 사용한다. 설정을 바꾼 모든 명령과 중간
순서를 복원한 것은 아니다.

| 설정 | 이전 백업 | RT 백업 | 현재 |
|---|---|---|---|
| `PREEMPT_RT` | n | y | y |
| ARM64 페이지 크기 | 4K | 16K | 16K |
| tickless 설정 | `NO_HZ_IDLE` | `NO_HZ_FULL` | `NO_HZ_FULL` |
| `HZ` | 250 | 1000 | 1000 |
| `LOCALVERSION` | `-v8` | `-thesis-test-rt` | `-thesis-test-rt` |
| DWARF5 / BTF | 비활성 | 활성 | 활성 |
| `XDP_SOCKETS` / `XDP_SOCKETS_DIAG` | 비활성 | 비활성 | 활성 |

RT 단계에는 ETF/BPF action, FQ/FQ_CODEL/MQPRIO built-in 설정과 Kconfig가
자동 선택한 의존 옵션도 포함된다. `NR_CPUS=4`, BPF syscall/JIT,
`PAGE_POOL`, `MACB`, `MACB_USE_HWSTAMP`는 이전 백업부터 동일했다.
이전 백업은 `bcm2712_defconfig` 그 자체가 아니므로 설정 diff를 임의의 기본
`.config`에 적용하지 말고 함께 제공한 시작 파일을 사용한다.

설정 변경을 단계별로 재현하는 예(새 커널 작업 디렉터리에서):

```bash
cp "$PATCH_DIR/config/before-rt.config" .config
git apply "$PATCH_DIR/config/0001-before-rt-to-rt.patch"
git apply "$PATCH_DIR/config/0002-enable-af-xdp.patch"
make ARCH=arm64 olddefconfig
```

최종 설정만 필요하면 `kernel.config`를 `.config`로 복사하고 `olddefconfig`를
실행해도 된다. 컴파일러 등 빌드 도구가 달라지면 자동 판정 옵션은 달라질 수 있다.

## 부팅 설정 참고

커널 `.config`는 빌드에 포함할 기능을 정하고, `/boot/firmware/config.txt`와
`cmdline.txt`는 실행할 커널 이미지와 부팅 인자를 정한다. 부팅 파일의
`before-rt` 백업도 발견해 별도 `boot/` 디렉터리에 보관했다.

백업에서 현재까지의 실제 변경은 다음과 같다.

- `config.txt`의 `[all]`에 `kernel=kernel_2712-thesis-xdp.img`와 `enable_uart=1` 추가.
- `cmdline.txt`의 `console=tty1`을 `console=ttyAMA0`으로 변경하고 `quiet` 제거.

부팅 diff는 `kernel-and-config.patch`에 포함되지 않는다. 이미지 선택 설정은
해당 이미지가 설치된 뒤 적용해야 하며, 다른 SD 카드에서는 현재 카드의
`root=PARTUUID` 등 장치 고유 부팅 인자를 유지해야 한다. `boot/` 원본은
이 장치의 스냅샷이다. 최초 RT 부팅과 이후 포팅 단계 사이의 중간 백업은 없어
각 부팅 설정을 어느 시점에 바꿨는지까지는 알 수 없다.

## 확인한 내용

- 통합/순차 드라이버 패치를 기준 커밋의 임시 index에 각각 적용해 동일한
  Git tree `dea83c077092854b49c9bcf0102f83e4ca1de404`를 얻었다.
- 복원된 드라이버 4개 파일은 현재 작업 파일과 동일하다.
- `kernel-and-config.patch` 적용 결과는 드라이버와 최종 설정을 함께 포함한
  tree `487076614a2eead5a014f73677d99e5fdc4b12af`다.
- 설정 통합/순차 패치 적용 결과와 현재 `.config`가 바이트 단위로 동일하다.
- 별도 복사본에서 `olddefconfig`를 실행해도 현재 설정이 그대로 유지됐다.
- 기준 커밋의 새 worktree에 통합 패치를 실제 적용한 뒤
  `make ARCH=arm64 ace_thesis_rt_defconfig`로 현재 `.config`와 동일한 파일이 생성됐다.
- 그 새 worktree에서 `macb_main.o`와 `macb_ptp.o`를 `W=1`로 빌드했다.
  빌드는 성공했고 기존 unused variable/통계 문자열 길이 경고 2개가 출력됐다.
- 부팅 diff도 임시 복사본에 적용해 현재 부팅 파일과 동일함을 확인했다.
- 최종 `.config` SHA-256:
  `57f3308d91dd2b719c9fe8da6fd24f18400758cbfe8cf045ce9da9e4e02b5f46`.

초기 7개 포팅의 장치 검증 기록은 원래 포팅 문서에 있다. 이후 0008 변경은
소스 빌드까지 확인한 상태이며 새 커널 부팅과 wire 검증은 아직 남아 있다.
이 묶음 생성 과정에서 원본 브랜치에 새 커밋을 만들거나 현재 커널을 설치하지 않았다.
전체 `Image.gz`/modules/dtbs 빌드와 새 커널 설치·재부팅은 이 재현 확인에 포함되지 않는다.

## 저장소 업로드와 배포

Application 소스와 이 디렉터리의 텍스트 패치·설정·문서는 프로젝트 Git 저장소에
commit/push해서 변경 이력을 보관한다. `patches/*.tar.gz` 압축본은 이 파일들을
묶어 만든 배포 산출물이므로 `.gitignore`로 제외하며, GitHub Release의
첨부파일로 올릴 수 있다. Release 첨부파일 업로드는 소스 commit/push와 별개다.

압축본을 다시 만드는 명령:

```bash
cd /home/craftp/ACEteam_Bachelor_thesis/patches
tar -czf macb-xdp-20260910.tar.gz macb-xdp-20260910
```

`linux-6.18-macb-reproduce`는 적용 확인용 worktree다. 실제 커널 수정 원본은
`linux-6.18-rt`의 `thesis-macb-xdp` 브랜치에 있으며, 확인용 복사본의 변경을
별도의 새 구현으로 올릴 필요는 없다. 이 묶음에는 원본의 미커밋 변경도 포함된다.
