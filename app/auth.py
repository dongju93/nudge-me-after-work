# cspell:ignore ntfy
"""관리자 인증 (F-08).

로컬 네트워크 전제의 **최소 인증**(PRD §4, 단일 사용자 비밀번호 수준)이다. 별도
라이브러리 없이 FastAPI 표준 제공 `HTTPBasic`만으로 해결한다 — 브라우저 기본 인증
다이얼로그가 로그인 화면을 대신하므로, 1인용 서비스에서 세션 쿠키/로그인 폼은 과설계다.

적용 범위: `rules`/`history` 라우터에만 `dependencies=[Depends(require_admin)]`로 건다
(main.py). `/webhooks/*`는 **제외** — ntfy 서버가 액션 버튼을 호출할 때 Basic 자격증명을
실을 수 없으므로, 그쪽은 F-05의 `token` 쿼리 파라미터 검증만으로 인증한다.
"""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings, get_settings

# auto_error=True(기본): Authorization 헤더가 아예 없으면 이 의존성이 스스로 401 +
# `WWW-Authenticate: Basic`을 던진다 → 브라우저가 기본 로그인 다이얼로그를 띄운다.
# 따라서 require_admin 본문은 "비밀번호 불일치" 한 가지만 처리하면 된다.
security = HTTPBasic()


def require_admin(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """관리 화면 접근을 HTTP Basic 비밀번호로 보호하는 라우터 의존성.

    사용자명은 검사하지 않는다 — 단일 사용자 서비스라 식별할 계정이 없고, 비밀번호만
    비교하는 것이 스펙의 "비밀번호 수준" 인증이다(아무 사용자명이나 허용).

    비밀번호는 상수 시간 비교로 대조한다. FastAPI `HTTPBasic`은 자격증명을 ASCII로만
    디코드하므로 실질적으로 ADMIN_PASSWORD는 ASCII만 지원한다. 그럼에도 양쪽을 UTF-8
    바이트로 인코딩해 비교하는 것은, 비-ASCII `ADMIN_PASSWORD`가 설정됐을 때
    `compare_digest`가 `str`에서 `TypeError`(→500)를 내는 대신 조용히 401로 떨어지게
    하기 위함이다. 불일치 시 401 + `WWW-Authenticate`로 재인증을 유도한다.
    """
    correct = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.admin_password.encode("utf-8"),
    )
    if not correct:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증에 실패했습니다.",
            headers={"WWW-Authenticate": "Basic"},
        )
