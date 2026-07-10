"""ntfy 액션 버튼 webhook (F-05).

ntfy 서버가 알림 버튼 클릭 시 호출하는 단일 엔드포인트다. 세션 상태 전이 자체는
`services/sessions.py`가 담당하고, 이 라우터는 **인증·조회·검증·이벤트 기록**과
트랜잭션 경계만 책임진다(스펙 F-05 §2, §3).

인증 예외: 나머지 관리 화면(F-08)은 HTTP Basic으로 보호되지만, 이 엔드포인트는 ntfy
서버가 호출하므로 Basic 자격증명을 실을 수 없다. 대신 버튼 URL에 심어둔 `token` 쿼리
파라미터를 `settings.webhook_token`과 **상수 시간 비교**해 인증한다.
"""

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session as DBSession

from app.config import Settings, get_settings
from app.db import get_db_session
from app.models import (
    EventType,
    NudgeSession,
    RuleAction,
    SessionEvent,
    SessionStatus,
)
from app.services.sessions import apply_action

# cspell:ignore ntfy

# prefix로 /webhooks를 붙여 F-08 관리자 인증에서 이 라우터 전체를 예외 처리하기 쉽게 한다
# (main.py에서 rules/history에만 require_admin을 걸고 webhooks는 제외).
router = APIRouter(prefix="/webhooks")
logger = logging.getLogger(__name__)


@router.post("/ntfy/actions", name="ntfy_action")
async def ntfy_action(
    session_id: int,
    action_id: int,
    token: str,
    db: Annotated[DBSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """ntfy 버튼 클릭을 수신해 세션 상태를 전이한다(UC-05~08, UC-10).

    쿼리 파라미터 `session_id`/`action_id`/`token`은 발행 시 Notifier가 버튼 URL에
    심어둔 값이다(F-04 `_build_payload`). 반환은 정상 처리 시 200, 인증/조회 실패 시
    4xx이며, 특히 **이미 종료된 세션의 재클릭은 200으로 무시**한다 — 200이 아니면 ntfy
    앱이 버튼 실패 에러를 표시하기 때문이다(UC-10).
    """
    # 1) 토큰 상수 시간 비교. 불일치 → 403. compare_digest는 early-return 타이밍으로
    #    토큰 길이/내용이 새는 것을 막는다. 양쪽을 UTF-8 바이트로 인코딩해 비교하는 것은,
    #    `token`이 공격자 제어 쿼리 파라미터라 비-ASCII 값이 들어오면 str 비교가
    #    `TypeError`(→처리되지 않은 500)를 내기 때문이다 — 바이트 비교로 조용히 403에
    #    떨어뜨린다(보안 리뷰 #4, CWE-754).
    logger.info(
        "ntfy 액션 webhook 수신 — session_id=%d action_id=%d",
        session_id,
        action_id,
    )
    if not secrets.compare_digest(
        token.encode("utf-8"), settings.webhook_token.encode("utf-8")
    ):
        logger.warning(
            "ntfy 액션 webhook 거부 — session_id=%d action_id=%d reason=invalid_token",
            session_id,
            action_id,
        )
        raise HTTPException(status_code=403, detail="유효하지 않은 토큰입니다.")

    # 2) 세션 조회. 없으면 404(잘못된 session_id, 또는 규칙 삭제로 cascade 제거된 세션).
    session = db.get(NudgeSession, session_id)
    if session is None:
        logger.warning(
            "ntfy 액션 webhook 실패 — session_id=%d action_id=%d reason=session_not_found",
            session_id,
            action_id,
        )
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    # 3) 이미 종료된 세션이면 아무 것도 바꾸지 않고 200으로 무시(UC-10 중복 클릭).
    #    action 검증보다 먼저 short-circuit한다 — 종료 세션에는 적용할 것이 없다.
    if session.status != SessionStatus.IN_PROGRESS:
        logger.info(
            "ntfy 액션 webhook 무시 — session_id=%d action_id=%d status=%s",
            session_id,
            action_id,
            session.status.value,
        )
        return {"status": "ignored"}

    # 4) action_id가 이 세션의 규칙에 속하는지 검증(타 규칙 action_id 조합/변조 방어).
    #    PK 조회 + 소유 규칙 대조를 한 번에 — 미존재/불일치 모두 404로 통일한다.
    action = db.get(RuleAction, action_id)
    if action is None or action.rule_id != session.rule_id:
        logger.warning(
            "ntfy 액션 webhook 실패 — session_id=%d action_id=%d rule_id=%d "
            "reason=action_not_found_or_mismatch",
            session_id,
            action_id,
            session.rule_id,
        )
        raise HTTPException(status_code=404, detail="액션을 찾을 수 없습니다.")

    # 5) 전이 적용 + CLICKED 이벤트 기록을 한 커밋으로 묶는다(스펙 F-05 §3). SQLite
    #    단일 프로세스이므로 이 단일 트랜잭션이 동시 클릭 경합을 충분히 방어한다.
    apply_action(db, session, action)
    db.add(
        SessionEvent(
            session_id=session_id,
            event_type=EventType.CLICKED,
            action_label=action.label,
        )
    )
    action_type = action.action_type.value
    status = session.status.value
    next_notify_at = (
        session.next_notify_at.isoformat()
        if session.next_notify_at is not None
        else None
    )
    db.commit()
    logger.info(
        "ntfy 액션 처리 완료 — session_id=%d action_id=%d action_type=%s "
        "status=%s next_notify_at=%s",
        session_id,
        action_id,
        action_type,
        status,
        next_notify_at,
    )

    return {"status": "ok", "action": action.label}
