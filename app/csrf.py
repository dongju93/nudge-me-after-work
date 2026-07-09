# cspell:ignore ntfy
"""CSRF 방어: 상태 변경 요청의 출처(Origin) 검증 (보안 리뷰 #2, CWE-352).

관리 화면은 F-08의 HTTP Basic으로 인증되지만, 브라우저는 Basic 자격증명을 **오리진
단위로 캐시해 교차 사이트 요청에도 자동 재전송**한다. 그래서 Basic만으로는 CSRF를
막지 못한다 — 공격자 페이지의 자동 제출 폼이 관리자의 캐시된 자격증명을 태워
`POST /rules/*`(생성/수정/토글/삭제)를 유발할 수 있다. Basic 자격증명은 쿠키가 아니라
`SameSite`로도 차단되지 않으므로, 별도의 출처 검증이 필요하다.

방어(OWASP "verify origin matches target"): 상태 변경 요청의 `Origin`(없으면 `Referer`)
헤더 호스트가 앱 자신의 호스트와 일치하는지 확인한다. 교차 사이트 POST에서 브라우저는
`Origin`을 공격자 페이지 오리진으로 채우지만 `Host`는 실제 목표(우리 앱)로 채우므로
둘이 어긋난다 → 403으로 거절. 토큰·쿠키·템플릿 수정 없이 의존성 하나로 끝난다.

읽기(GET/HEAD/OPTIONS)는 상태를 바꾸지 않고 Origin 없이도 일어나므로(주소창 직접 접근,
링크 내비게이션) 검사에서 제외한다 — 안 그러면 관리 화면 자체가 열리지 않는다.
"""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings

# 상태를 바꾸지 않는 안전 메서드 — CSRF 대상이 아니므로 검증을 건너뛴다(RFC 9110).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _trusted_hosts(request: Request, settings: Settings) -> set[str]:
    """이 앱으로 인정할 목표 호스트(`host[:port]`) 후보 집합.

    세 출처를 합친다:
      - `webhook_base_url`의 호스트: 프록시(Caddy/Cloudflare Tunnel)가 `Host`를 내부
        주소로 재작성해도 흔들리지 않는 **외부 공개 URL의 권위 있는 앵커**. 관리 UI와
        webhook은 같은 앱이라 외부 오리진이 동일하다.
      - 요청 `Host`: 프록시 없이 LAN IP 등으로 직접 접근하는 경우를 커버.
      - `X-Forwarded-Host`: 프록시가 원 호스트를 이 헤더로 전달하는 경우를 커버.

    비교는 대소문자 무시를 위해 전부 소문자로 정규화한다. CSRF 관점에서 `Host` 신뢰는
    안전하다 — 위조 POST에서도 `Host`는 브라우저가 실제 목표로 채우므로 공격자가
    `Origin`과 일치시킬 수 없다.
    """
    hosts = {urlsplit(settings.webhook_base_url).netloc.lower()}
    host = request.headers.get("host")
    if host:
        hosts.add(host.lower())
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        # 프록시 체인에서 쉼표로 여러 개가 올 수 있다 — 클라이언트에 가장 가까운 첫 값만.
        hosts.add(forwarded.split(",")[0].strip().lower())
    hosts.discard("")  # 설정/헤더 누락으로 빈 문자열이 섞이면 제거
    return hosts


def verify_origin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """상태 변경 요청의 출처가 앱 자신인지 검증하는 라우터 의존성.

    `Origin`(교차 오리진 POST에 브라우저가 항상 첨부)을 우선 검사하고, 없으면 같은
    목적의 `Referer`로 폴백한다. 둘 다 없는 상태 변경 요청은 정상 폼 제출이 아니므로
    **fail-closed**(403)로 거절한다 — 현대 브라우저는 동일 출처 POST에도 `Origin`을
    보내므로 정상 관리 UI는 영향받지 않는다.
    """
    if request.method in _SAFE_METHODS:
        return

    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="요청 출처를 확인할 수 없습니다.",
        )

    # urlsplit로 netloc(host[:port])만 뽑아 스킴(프록시 http/https 모호성)·경로를 배제하고
    # 호스트 단위로만 비교한다.
    source_host = urlsplit(source).netloc.lower()
    if not source_host or source_host not in _trusted_hosts(request, settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="교차 사이트 요청이 차단되었습니다.",
        )
