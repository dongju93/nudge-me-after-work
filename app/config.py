"""애플리케이션 설정.

`.env` 파일 또는 환경 변수에서 값을 읽어 타입 검증된 `Settings` 객체로 노출한다.
pydantic-settings는 `fastapi[standard]`의 전이 의존성이므로 별도 설치가 필요 없다.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 설정 단일 소스.

    기본값이 없는 필드(ntfy/webhook/admin 관련)는 `.env`에 반드시 존재해야 하며,
    누락 시 앱 기동 시점에 `ValidationError`로 즉시 실패한다 — 잘못된 설정으로
    조용히 뜨는 것보다 부팅에서 터지는 편이 운영상 안전하다.
    """

    # env_file: 로컬/배포 공통으로 `.env`에서 로드. extra="ignore"는 선언하지 않은
    # 환경 변수(shell/systemd가 주입하는 것 포함)가 있어도 검증 에러 없이 무시한다.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Turso 원격 DB. 둘 다 설정되면 로컬 database_url보다 우선한다.
    turso_conn: str | None = None
    turso_token: str | None = None

    # 기본값이 있는 항목: 로컬 SQLite 파일. Turso 설정이 없을 때 이 URL로 엔진을 만든다.
    database_url: str = "sqlite:///./data/nudge.db"

    # ntfy 발행 대상 (F-04). base_url은 ntfy 서버 루트, topic은 구독 주제.
    ntfy_base_url: str  # 예: https://ntfy.example.com
    ntfy_topic: str
    ntfy_access_token: str  # ntfy publish용 Bearer 토큰

    # ntfy 액션 버튼이 호출할 외부 접근 가능 URL과 검증 토큰 (F-05).
    webhook_base_url: str  # 예: https://nudge.example.com
    webhook_token: str  # 버튼 URL의 ?token= 값과 상수 시간 비교

    # 관리 화면 HTTP Basic 인증 비밀번호 (F-08).
    admin_password: str

    # Logfire 관측 토큰. FastAPI Cloud가 Logfire 통합 연결 시 `LOGFIRE_TOKEN`으로 주입한다.
    # 로컬/CI에는 없는 게 정상이라 Optional로 둔다 — 토큰이 없으면 configure가 전송을
    # 하지 않도록(send_to_logfire="if-token-present") main.py에서 처리하므로, 부재해도
    # 기동은 실패하지 않는다.
    logfire_token: str | None = None

    # 시각 판단 기준 시간대. 모든 요일/시각 비교는 이 값으로 계산한다.
    timezone: str = "Asia/Seoul"

    # 최초 알림 트리거 유예 창(분). start_time 이후 이 시간 안에만 최초 발송한다 (F-06).
    trigger_grace_minutes: int = 10


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴 접근자.

    `@lru_cache`로 최초 호출 시 한 번만 `.env`를 파싱하고 이후엔 캐시된 인스턴스를
    반환한다. FastAPI `Depends(get_settings)`로도 그대로 주입 가능하며, 테스트에서는
    `get_settings.cache_clear()`로 재설정한다.
    """
    return Settings()  # pyright: ignore[reportCallIssue]  # 필수 필드는 .env에서 채워짐
