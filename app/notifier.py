"""ntfy 발행 클라이언트 (F-04).

ntfy topic으로 규칙 알림을 발행한다. 알림 본문 + 버튼(최대 3개)을 ntfy의 JSON
publish 형식으로 보내고, 일시적 네트워크 장애나 5xx 응답에는 지수 백오프로
재시도한다 (PRD §3.4, §4 / tech-stack §4.1).

이 계층은 **HTTP 발행만** 책임진다. 발행 성공 후 `SessionEvent(SENT)`를 남기는 것은
호출자(F-06 스케줄러 / F-05 서비스)의 몫이다 — Notifier는 DB를 모른다. 그래야
스케줄러·webhook·수동 테스트가 같은 발행 경로를 공유하면서도 트랜잭션 경계를 각자
통제할 수 있다.
"""

import asyncio
import logging

import httpx2
from fastapi import Request

from app.config import Settings
from app.models import Rule

logger = logging.getLogger(__name__)

# 재시도 정책 (tech-stack §1: 의존성 최소화 — tenacity 등을 쓰지 않고 asyncio.sleep
# 루프로 직접 구현). "재시도 총 3회, 백오프 1s → 2s → 4s"를 다음과 같이 해석한다:
# 초기 1회 전송 + 재시도 3회 = 최대 4회 전송, 각 재시도 직전에 1s → 2s → 4s 대기.
# (백오프 값이 3개이므로 대기가 3번, 즉 전송이 4번이라야 셋 다 쓰인다.)
_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0  # 재시도 n회차 직전 대기 = _BASE * 2**n → 1s, 2s, 4s


class NtfyPublishError(RuntimeError):
    """ntfy 발행이 최종 실패했음을 알리는 예외.

    4xx(설정 오류)면 재시도 없이 즉시, `TransportError`/5xx면 재시도 소진 후 발생한다.
    호출자는 이를 잡아 "세션은 유지하되 tick은 계속 진행"(F-06) 같은 정책을 적용한다.
    성공 시엔 값 없이 정상 리턴하므로, 호출부의 `SENT` 기록 코드는 예외가 없을 때만
    실행되어 자연히 "발행 성공 시에만 기록"(F-04 §4)이 된다.
    """


class Notifier:
    """공유 httpx `AsyncClient`로 ntfy에 알림을 발행하는 순수 HTTP 계층."""

    def __init__(self, client: httpx2.AsyncClient, settings: Settings) -> None:
        # 클라이언트를 주입받는다(직접 생성하지 않는다): 커넥션 풀을 앱 전체에서 1개만
        # 유지하려고 lifespan이 만든 인스턴스를 그대로 재사용하고, 단위 테스트에서는
        # 가짜 transport를 물린 클라이언트를 끼워 넣을 수 있게 하려는 목적.
        self._client = client
        self._settings = settings

    async def publish(self, *, rule: Rule, session_id: int, message: str) -> None:
        """`rule`의 버튼셋을 붙여 ntfy로 `message`를 발행한다.

        `message`는 규칙 최초 메시지 또는 스누즈 문구 중 무엇을 쓸지 호출자가 골라
        넘긴다(Notifier는 규칙/스누즈 구분을 하지 않는다). 성공하면 값 없이 리턴,
        최종 실패하면 `NtfyPublishError`를 던진다.
        """
        payload = self._build_payload(rule=rule, session_id=session_id, message=message)
        headers = self._build_headers()
        logger.info(
            "ntfy 발행 시작 — rule_id=%s session_id=%d action_count=%d",
            rule.id,
            session_id,
            len(rule.actions),
        )

        # 재시도 소진 시 마지막 실패 원인을 예외에 실어 보내기 위한 추적 변수.
        last_error = "원인 불명"
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):  # 0..3 → 초기 1회 + 재시도 3회
            logger.info(
                "ntfy 발행 요청 — rule_id=%s session_id=%d attempt=%d/%d",
                rule.id,
                session_id,
                attempt + 1,
                _MAX_RETRIES + 1,
            )
            try:
                response = await self._client.post(
                    self._settings.ntfy_base_url, json=payload, headers=headers
                )
            except httpx2.TransportError as exc:
                # 연결 거부·DNS 실패·타임아웃 등 네트워크 계층 오류 → 일시 장애로 보고 재시도.
                last_exc = exc
                last_error = f"네트워크 오류: {exc!r}"
                logger.warning(
                    "ntfy 발행 시도 %d/%d 실패 — %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    last_error,
                )
            else:
                if response.is_success:
                    logger.info(
                        "ntfy 발행 성공 — rule_id=%s session_id=%d status=%d "
                        "attempt=%d/%d",
                        rule.id,
                        session_id,
                        response.status_code,
                        attempt + 1,
                        _MAX_RETRIES + 1,
                    )
                    return  # 정상 발행 완료. SENT 이벤트 기록은 호출자 몫.
                if response.is_client_error:
                    # 4xx는 잘못된 topic/토큰/권한 등 설정 오류라 재시도해도 그대로 실패한다.
                    # 헛된 백오프를 피하려 즉시 로그를 남기고 중단한다.
                    logger.error(
                        "ntfy 발행 실패(4xx, 재시도 안 함) — status=%d body=%s",
                        response.status_code,
                        response.text[:200],
                    )
                    raise NtfyPublishError(
                        f"ntfy가 {response.status_code}를 반환했다(설정 오류 추정)"
                    )
                # 5xx(및 그 외 비성공) → 서버 측 일시 장애로 보고 재시도.
                last_error = f"서버 오류 status={response.status_code}"
                logger.warning(
                    "ntfy 발행 시도 %d/%d 실패 — %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    last_error,
                )

            # 마지막 시도가 아니면 지수 백오프 후 재시도 (1s → 2s → 4s).
            if attempt < _MAX_RETRIES:
                delay_seconds = _BASE_BACKOFF_SECONDS * 2**attempt
                logger.info(
                    "ntfy 발행 재시도 대기 — rule_id=%s session_id=%d "
                    "delay_seconds=%.1f",
                    rule.id,
                    session_id,
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)

        logger.error(
            "ntfy 발행 최종 실패(재시도 %d회 소진) — %s", _MAX_RETRIES, last_error
        )
        raise NtfyPublishError(
            f"ntfy 발행이 재시도 {_MAX_RETRIES}회 후에도 실패했다: {last_error}"
        ) from last_exc

    def _build_payload(
        self, *, rule: Rule, session_id: int, message: str
    ) -> dict[str, object]:
        """ntfy JSON publish 페이로드를 만든다.

        헤더 방식(`X-Actions` 등) 대신 JSON 본문을 쓰는 이유: 한글 메시지·라벨과 버튼
        URL을 헤더 인코딩 제약 없이 안전하게 실을 수 있다. 각 버튼 URL에는
        `session_id` + `action_id`(RuleAction PK) + `token`을 담아, 클릭 수신(F-05)
        시점에 라벨 문자열 매칭 없이 액션 정의를 바로 조회하고 토큰으로 임의 호출을 막는다.
        """
        settings = self._settings
        return {
            "topic": settings.ntfy_topic,
            "message": message,
            "title": rule.name,
            "actions": [
                {
                    "action": "http",
                    "label": action.label,
                    "url": (
                        f"{settings.webhook_base_url}/webhooks/ntfy/actions"
                        f"?session_id={session_id}"
                        f"&action_id={action.id}"
                        f"&token={settings.webhook_token}"
                    ),
                    "method": "POST",
                    "clear": True,  # 클릭 시 ntfy 앱에서 해당 알림 자동 제거
                }
                # actions는 Relationship에서 sort_order 오름차순으로 이미 정렬됨(최대 3개).
                for action in rule.actions
            ],
        }

    def _build_headers(self) -> dict[str, str]:
        """ntfy publish용 인증 헤더를 만든다."""
        return {"Authorization": f"Bearer {self._settings.ntfy_access_token}"}


def get_notifier(request: Request) -> Notifier:
    """FastAPI 의존성: lifespan이 `app.state`에 심어둔 Notifier를 반환한다.

    라우터(F-05 webhook, 완료 조건의 임시 검증 라우트)에서 `Depends(get_notifier)`로
    주입해 쓴다. 스케줄러(F-06)는 요청 컨텍스트 밖에서 돌기 때문에 이 의존성 대신
    `app.state.notifier`를 직접 참조한다.
    """
    return request.app.state.notifier
