# VEX 예외 문서

`DOCKER_SCOUT_CVE_ASSESSMENT.md`의 “해결 가능 15건”을 근거 기반 OpenVEX
`not_affected` 문서로 종결한 결과다. 전역 CVE ignore는 쓰지 않고, 제품 식별자를 조사한
이미지 digest로만 제한한다.

- 문서: `nudge-me-after-work.not-affected.vex.json` (OpenVEX v0.2.0, 15 statements)
- 대상 이미지 ID: `sha256:ea60d777491ef899675a4eda9c31dfc6834d76a3f3eebdc82e53d5b2b1872da0`
- 대상 repo digest: `sha256:db5e8509759f793d5db4a9ee1e2e8d9d97333688d81f4fa16e81594dfa97b740`
- 플랫폼: `linux/arm64`, 베이스 `python:3.14-slim-bookworm`

## 적용과 검증

```console
docker scout cves local://tls2323/nudge-me-after-work:latest \
  --platform linux/arm64 --vex-location ./vex
```

15건에 `VEX : not affected [vulnerable code not present]`가 붙고, 그 외 항목에는 붙지
않아야 한다. 정확히 15건인지 확인:

```console
docker scout cves local://tls2323/nudge-me-after-work:latest \
  --platform linux/arm64 --vex-location ./vex \
  | rg -B4 'VEX +: not affected' | rg -o 'CVE-[0-9]{4}-[0-9]+' | sort -u | wc -l
```

## 알아야 할 두 가지 동작

**1. Scout CLI는 표시만 하고 개수를 줄이지 않는다.** Scout v1.24.0에서
`--vex-location`은 finding에 VEX 판정을 주석으로 붙일 뿐, `48 vulnerabilities found`
요약, SARIF 출력, `--exit-code`(2) 중 어느 것도 바뀌지 않는다. `--ignore-suppressed`는
Scout 조직 예외용이고 로컬 VEX에는 적용되지 않는다. 숫자까지 줄이려면 이 문서를
attestation으로 이미지에 붙이고(`docker scout attestation add`) Scout Dashboard 예외로
평가해야 하며, 여기에는 레지스트리 쓰기 권한과 별도 승인이 필요하다.

**2. 제품 PURL은 스캔 방식에 따라 다른 digest로 매칭된다.** 실측 결과
`local://` 스캔은 **image ID** digest에만, `registry://` 스캔은 **repo** digest(및 태그)에만
매칭된다. 그래서 statement마다 두 digest를 모두 product로 넣었다. 태그(`@latest`)는
일부러 넣지 않는다 — 재빌드 후에도 예외가 살아남아 검증 없이 계속 적용되기 때문이다.

## 만료 조건

다음 중 하나라도 발생하면 이 문서는 자동으로 매칭이 끊기거나, 끊기지 않더라도 다시
검증해야 한다.

- 이미지 재빌드 → 두 digest가 모두 바뀌므로 statement가 매칭되지 않는다(의도된 동작).
- 베이스 이미지 갱신으로 `perl` / `sqlite3` / `openssl` / `util-linux` / `gcc-12` /
  `shadow` 버전이 바뀜 → subcomponent PURL이 버전까지 고정하므로 매칭이 끊긴다.
- 앱이 `subprocess`, archive 해제, 외부 SQLite 파일 입력, Perl 실행을 추가 → 판정 근거
  자체가 무효가 된다.

## 갱신 절차

1. 새 digest 확인:
   `docker image inspect <이미지> --format '{{.Id}} {{json .RepoDigests}}'`
2. 근거 재확인 — 컨테이너 안에서 모듈·바이너리 부재를 다시 관찰한다
   (`perl -MHTTP::Tiny -e1`, `command -v zipdetails`, `dpkg -S /usr/bin/chfn`,
   `command -v nm`, `python -c 'import ssl;print(ssl.OPENSSL_VERSION)'`).
3. Scout 스캔의 subcomponent PURL을 그대로 복사한다. Scout는 **소스** 패키지명
   (`perl`, `gcc-12`, `shadow`)을 쓰므로 설치된 바이너리 패키지명(`perl-base`,
   `libgcc-s1`, `passwd`)과 다르다.
4. `timestamp`를 갱신하고 `version`을 올린 뒤 위 검증 명령을 다시 돌린다.
