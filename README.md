# Nudge Me After Work

퇴근 후 저녁 시간을 계획한 활동으로 전환하도록 돕는 개인용 넛지 리마인더 시스템입니다.

정해진 요일과 시간에 ntfy 알림을 보내고, 사용자가 알림 버튼으로 응답한 내용에 따라 완료, 스누즈, 포기, 무응답 상태를 기록합니다. 반복되는 저녁 루틴을 관리 화면에서 규칙으로 만들고, 실행 이력을 통해 어떤 활동이 실제로 이어졌는지 확인하는 데 초점을 둡니다.

## 주요 기능

- 리마인더 규칙 관리
  - 규칙 이름, 요일, 시작 시각, 알림 메시지, 컷오프 시각, 활성 상태를 관리합니다.
  - 규칙별로 알림 버튼과 버튼별 후속 액션을 설정합니다.
  - 규칙은 삭제하지 않고도 일시적으로 비활성화할 수 있습니다.

- ntfy 알림 발행
  - 지정한 시각에 ntfy topic으로 알림을 발행합니다.
  - 알림에는 사용자가 즉시 응답할 수 있는 액션 버튼이 포함됩니다.
  - iOS ntfy 앱을 통해 저녁 시간대의 행동 시작 시점을 알려줍니다.

- 버튼 응답 처리
  - 완료 버튼은 세션을 완료 상태로 종료하고 후속 알림을 중단합니다.
  - 스누즈 버튼은 설정된 시간 뒤 재알림을 예약합니다.
  - 포기 버튼은 세션을 미완료 상태로 종료하고 당일 재알림을 중단합니다.

- 스누즈와 컷오프
  - 스누즈 응답이 있으면 지정한 분 단위로 재알림을 보냅니다.
  - 컷오프 시각이 지나도 응답이 없으면 세션을 무응답 상태로 정리합니다.
  - 서버가 재시작되어도 진행 중인 세션과 재알림 상태를 DB 기준으로 복원합니다.

- 실행 이력 확인
  - 알림 발행, 버튼 클릭, 자동 종료 이벤트를 세션 단위로 기록합니다.
  - 규칙별 최근 실행 결과와 완료율을 관리 화면에서 확인합니다.

- 접근 보안
  - 관리 화면은 HTTP Basic 인증으로 보호하고, 상태를 바꾸는 요청은 Origin 검사로 교차 사이트 요청(CSRF)을 차단합니다.
  - ntfy 버튼이 호출하는 webhook은 버튼 URL에 담긴 토큰을 상수 시간 비교로 검증합니다.

## 사용 예제

1. 관리 화면에서 `운동 리마인더` 같은 규칙을 만듭니다.
2. 요일, 시작 시각, 알림 메시지, 컷오프 시각을 입력합니다.
3. 알림에 표시할 버튼과 각 버튼의 액션을 설정합니다.
4. 지정한 시간에 ntfy 알림을 받습니다.
5. 알림 버튼으로 `하는중`, `나중에`, `안해` 같은 현재 상태를 응답합니다.
6. 응답 결과에 따라 세션이 완료되거나, 재알림이 예약되거나, 미완료로 종료됩니다.
7. 관리 화면에서 최근 실행 이력과 완료율을 확인합니다.

## 시스템 구성

- FastAPI: 관리 화면, webhook, 스케줄러를 하나의 프로세스에서 실행
- Jinja2: 규칙 관리와 이력 확인을 위한 서버사이드 렌더링 화면
- APScheduler: 최초 알림, 스누즈 재알림, 컷오프 종료 처리
- SQLModel + SQLite: 규칙, 버튼 액션, 세션, 이벤트 이력 저장 (로컬·배포 모두 단일 SQLite 파일, 로컬 파일에는 WAL 모드 적용)
- httpx: ntfy 알림 발행용 비동기 HTTP 클라이언트
- ntfy: 모바일 푸시 알림과 액션 버튼 제공
- nginx: 앱 앞단의 리버스 프록시(단일 인그레스). DuckDNS 도메인 인증서로 TLS를 직접 종단하고, 원 Host·스킴·클라이언트 IP 헤더를 앱으로 전달

## 데이터 모델

- `Rule`: 리마인더 규칙의 이름, 요일, 시작 시각, 메시지, 컷오프 시각, 활성 상태
- `RuleAction`: 알림 버튼 라벨과 완료, 스누즈, 포기 액션 정의
- `NudgeSession`: 특정 날짜에 실행된 규칙의 진행 상태
- `SessionEvent`: 알림 발행, 버튼 클릭, 자동 종료 같은 세션 이벤트

## 배포 (Docker)

단일 컨테이너를 자가호스팅(예: 라즈베리파이)으로 상시 구동합니다. 스케줄러가 매분
tick을 돌려야 하므로 상시 실행이 전제이며, `restart: unless-stopped`로 재부팅·크래시
후 자동 복귀합니다. 진행 중 세션과 스누즈 상태는 스케줄러 잡이 아니라 DB 행에 있으므로
컨테이너를 재기동해도 볼륨의 SQLite 파일에서 그대로 복원됩니다.

### 서비스 구성 · 포트 · 통신 흐름

두 컨테이너가 compose 기본 네트워크에서 서비스 이름으로 통신합니다. `nginx`만 호스트에
포트를 노출하고, `nudge`는 네트워크 내부에만 노출(`expose`)되어 nginx를 거쳐야만 도달합니다.
TLS는 이 nginx가 최종 엣지로서 직접 종단합니다(앞단 프록시 없음).

| 서비스  | 컨테이너 이름         | 컨테이너 포트 | 호스트 노출       | 역할                                      |
| ------- | --------------------- | ------------- | ----------------- | ----------------------------------------- |
| `nginx` | `nudge-nginx`         | `443` / `80`  | `18443` / `18080` | 유일한 인그레스. TLS 종단 + 리버스 프록시 |
| `nudge` | `nudge-me-after-work` | `8000`        | 없음(`expose`)    | FastAPI 앱(UI·webhook·스케줄러)           |

호스트 저포트(`443`/`80`)가 점유 중이라 nginx는 상위 포트 `18443`(https)·`18080`(http)으로
매핑합니다. 공유기 포트포워딩을 공인 `443` → 호스트 `18443`, 공인 `80` → 호스트 `18080`으로
걸어 인터넷 트래픽을 들이고, nginx가 DuckDNS 도메인 인증서로 TLS를 직접 종단합니다.

```
브라우저 / ntfy
     │  HTTPS (DuckDNS 도메인)
     ▼
인터넷 → DuckDNS
     │
     ▼
공유기 포트포워딩   공인 443 → 호스트 18443 (https)
                    공인 80  → 호스트 18080 (http→https 승격)
     │
     ▼
nginx :443 / :80   (nudge-nginx)  ── TLS 종단, X-Forwarded-Proto=https 설정
     │  compose 네트워크, HTTP
     ▼
nudge :8000        (nudge-me-after-work)
     │
     └─► ntfy 발행은 앱에서 외부로 아웃바운드(httpx)
```

- 인바운드는 모두 `인터넷 → DuckDNS → 공유기 포트포워딩 → nginx → nudge(8000)` 한 경로로만
  들어옵니다.
- HTTP(`18080→80`)는 `GET /healthz` 직응답을 빼고 전부 https로 `301` 승격합니다. 관리 화면·
  webhook의 HTTP Basic 자격증명이 평문으로 흐르지 않습니다. LAN에서 `http://호스트IP:18080`으로
  직접 붙어도 리다이렉트가 포트를 `18443`으로 바꿔 https로 올려보냅니다.
- nginx는 원 `Host`·`X-Forwarded-Host`를 보존해 앱의 Origin 검사(CSRF)·`WEBHOOK_BASE_URL`
  검증이 그대로 동작하고, `X-Forwarded-Proto=https`로 스킴을 확정합니다.
- uvicorn은 기본적으로 `127.0.0.1` 프록시의 `X-Forwarded-*`만 신뢰하는데, nginx는 compose
  네트워크의 다른 컨테이너(`172.x`)라 그대로면 스킴이 http로 잘려 `url_for`가 http 절대 URL을
  만듭니다(정적 자산이 TLS 포트에 http로 요청돼 400). 그래서 `nudge` 서비스에 환경변수
  `FORWARDED_ALLOW_IPS=*`를 줘 nginx의 헤더를 신뢰합니다. `8000`은 호스트에 노출되지 않아
  nginx만 도달 가능하므로 `*`가 안전합니다.
- nginx 자체 라이브니스는 앱과 분리된 `GET /healthz`(200), 앱 준비 상태는 프록시되는
  `GET /health`로 확인합니다.
- ntfy 발행은 앱이 외부로 직접 보내는 아웃바운드라 이 경로와 무관합니다.

### 1. 환경 변수 준비

`.env.example`을 복사해 실제 값을 채웁니다. compose가 `env_file: .env`로 주입하며,
`NTFY_*`·`WEBHOOK_*`·`ADMIN_PASSWORD`가 비어 있으면 앱이 부팅에서 즉시 실패합니다.

```bash
cp .env.example .env
# NTFY_ACCESS_TOKEN, ADMIN_PASSWORD 채우기
# WEBHOOK_TOKEN 은 임의 문자열로: openssl rand -hex 16
# WEBHOOK_BASE_URL 은 ntfy 버튼이 외부에서 접근할 이 앱의 공개 URL
```

`DATABASE_URL`은 `.env`에 넣지 않습니다. Docker는 이미지 기본값 `sqlite:////data/nudge.db`
(볼륨 절대경로), 로컬 개발은 코드 기본값 `./data/nudge.db`를 각자 적용합니다. compose의
`env_file: .env`는 여기 적힌 값을 이미지 `ENV`보다 우선 주입하므로, 상대경로를 채워 넣으면
볼륨(`/data`) 밖에 기록됩니다.

### 2. 실행

compose는 Docker Hub 이미지를 pull해 구동만 합니다(빌드는 아래 push 단계가 담당). 배포
서버에는 `compose.yaml`과 `.env`만 있으면 됩니다.

```bash
docker compose pull           # Docker Hub에서 latest 이미지 받기
docker compose up -d          # 백그라운드 상시 구동
docker compose logs -f        # 로그 추적(json-file, 10MB×3 로테이션)
docker compose ps             # 헬스체크 포함 상태 확인
```

Docker Hub에 올릴 때는 `latest`와 버전 태그(`pyproject.toml` 기준 `0.1.0`)를 함께 push합니다.
compose는 `latest`만 참조하므로 버전 태그는 롤백·감사용입니다.

```bash
docker login          # Docker Hub 사용자명 + Access Token (최초 1회)
docker buildx build --push \
  -t tls2323/nudge-me-after-work:latest \
  -t tls2323/nudge-me-after-work:0.1.0 .
```

### 3. 데이터 지속성

SQLite 파일은 named volume `nudge-data`에 저장됩니다. 이미지가 `/data`를 앱 사용자
(uid 10001) 소유로 만들어 두므로 비루트 프로세스가 bind 마운트 chown 없이 바로 씁니다.
컨테이너를 지워도 볼륨은 남습니다.

```bash
docker compose down           # 컨테이너 제거(볼륨 유지)
docker compose down -v        # 볼륨까지 제거(모든 규칙·이력 삭제 — 주의)
# 백업: 로컬 SQLite는 WAL 모드라 구동 중 .db만 복사하면 최신 커밋을 놓칠 수 있으니
# 먼저 `docker compose stop` 후 복사하는 편이 안전합니다
docker run --rm -v nudge-data:/data -v "$PWD":/backup alpine \
  cp /data/nudge.db /backup/nudge-backup.db
```

### 4. 헬스체크 · TLS

- 앱 컨테이너 HEALTHCHECK는 인증·DB 없이 프로세스 응답만 확인하는 `GET /health`, nginx
  컨테이너는 앱과 분리된 `GET /healthz`를 씁니다.
- 외부 공개 지점은 nginx 호스트 포트 `18443`(https)이며, `18080`(http)은 https로 승격만
  합니다(앱 `8000`은 호스트에 노출하지 않음). nginx가 DuckDNS 도메인 인증서로 TLS를 직접
  종단하므로 앞단 프록시가 필요 없습니다. HTTP Basic 자격증명은 base64 인코딩일 뿐 암호화가
  아니므로, 반드시 https(`18443`)로만 공개하고 평문 HTTP로 노출하지 마세요.

### 5. 업데이트

새 이미지를 push한 뒤, 배포 서버에서 최신 `latest`를 받아 교체합니다(볼륨 데이터는 유지).

```bash
docker compose pull           # 새 latest 이미지 받기
docker compose up -d          # 바뀐 이미지로 재생성
```

### 관측 (선택)

`LOGFIRE_TOKEN`을 채우면 Logfire로 로그·트레이스를 전송합니다. 비워두면 Docker 로그만
사용합니다.

- Logfire 대시보드: <https://logfire-us.pydantic.dev/dongju93/nudge-me-after-work>
