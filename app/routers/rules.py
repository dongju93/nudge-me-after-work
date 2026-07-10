"""규칙 CRUD 화면 + 처리 (F-03).

JSON API가 아니라 **HTML 폼 POST → 303 redirect**(Post/Redirect/Get) 패턴이다.
브라우저 폼은 GET/POST만 지원하므로 수정·삭제·토글도 전부 POST로 통일하고, 처리 후
목록으로 리다이렉트해 새로고침 시 폼 재전송(중복 생성)을 막는다.

검증 실패 시에는 리다이렉트하지 않고, **입력값을 유지한 채** 같은 폼을 에러 메시지와
함께 다시 렌더링한다(완료 조건). 이를 위해 신규/수정/에러 재렌더가 모두 같은 `draft`
뷰 모델 형태(`_blank_draft`/`_draft_from_rule`/`RuleForm.to_draft`)를 공유한다.
"""

import logging
from datetime import datetime, time
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session as DBSession, col, select

from app.config import Settings, get_settings
from app.db import get_db_session
from app.models import ActionType, Rule, RuleAction
from app.services.history import STATUS_LABELS, summarize_rule
from app.templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

# cspell:ignore ntfy

# --- UI 설계(manage.dc.html)와 동일한 상수 ---------------------------------
#
# 요일 정의: 코드(모델 CSV에 저장)와 한글 라벨(칩 표시). 튜플 순서가 곧 표시 순서이자
# CSV 저장 순서다 — 사용자가 체크한 순서와 무관하게 항상 월→일로 정규화해 저장한다.
WEEKDAYS: list[tuple[str, str]] = [
    ("mon", "월"),
    ("tue", "화"),
    ("wed", "수"),
    ("thu", "목"),
    ("fri", "금"),
    ("sat", "토"),
    ("sun", "일"),
]
_WEEKDAY_CODES = {code for code, _ in WEEKDAYS}

# 버튼(액션) 개수는 3개 고정 — ntfy가 액션 버튼을 최대 3개까지 지원(PRD §3.4)하므로
# 가변 개수 대신 고정으로 단순화한다. 폼 필드명은 인덱스 접미사(_1/_2/_3)로 받는다.
ACTION_COUNT = 3
RULE_FORM_TEMPLATE = "rule_form.html"
RULE_NOT_FOUND_DETAIL = "규칙을 찾을 수 없습니다."
RULE_NOT_FOUND_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": RULE_NOT_FOUND_DETAIL}
}

# 액션 타입 선택지: (값, 라벨). 값은 ActionType enum과 1:1. 폼의 3택 세그먼트 버튼용.
ACTION_TYPE_CHOICES: list[tuple[str, str]] = [
    ("complete", "완료 처리"),
    ("snooze", "지연(스누즈)"),
    ("abandon", "포기 처리"),
]

# 목록 화면 액션 칩용 라벨(UI 설계 ACTION_TYPE_META) — 폼과 달리 snooze는 짧게 "지연".
ACTION_TYPE_LABELS: dict[str, str] = {
    "complete": "완료 처리",
    "snooze": "지연",
    "abandon": "포기 처리",
}


# --- 폼 데이터 모델 ---------------------------------------------------------
#
# FastAPI의 Pydantic "Form 모델" 기능으로 multipart 폼 필드를 한 객체로 받는다
# (python-multipart 설치됨). 개별 Form() 파라미터 18개를 나열하는 것보다 간결하고,
# 사용자 가이드라인(IO 경계는 Pydantic BaseModel)에도 부합한다.
#
# 모든 필드에 기본값을 둬서 **필수 필드 누락으로 422가 먼저 터지지 않게** 한다 —
# 검증은 아래 validate()에서 직접 수행해 에러 메시지와 함께 폼을 재렌더링하기 위함이다.
class RuleForm(BaseModel):
    name: str = ""
    # 체크박스는 체크 시에만 "on"을 전송하고, 미체크 시 필드 자체가 누락된다.
    # Pydantic이 "on"→True로, 누락→기본 False로 처리한다.
    is_active: bool = False
    # 같은 name의 체크박스 여러 개 → list로 수집. 하나도 체크 안 하면 빈 리스트.
    weekdays: list[str] = []
    start_time: str = ""  # "<input type=time>"이 보내는 "HH:MM" 문자열
    cutoff_time: str = ""
    message: str = ""

    # 버튼 3개 × (라벨/타입/스누즈 분/스누즈 문구). snooze 타입일 때만 뒤 두 필드가 의미.
    action_label_1: str = ""
    action_type_1: str = "complete"
    action_snooze_minutes_1: str = ""
    action_snooze_message_1: str = ""
    action_label_2: str = ""
    action_type_2: str = "snooze"
    action_snooze_minutes_2: str = ""
    action_snooze_message_2: str = ""
    action_label_3: str = ""
    action_type_3: str = "abandon"
    action_snooze_minutes_3: str = ""
    action_snooze_message_3: str = ""

    def _action_raw(self, i: int) -> dict[str, str]:
        """i번째(1-base) 버튼의 원본 문자열 값을 draft 형태로 뽑는다."""
        return {
            "label": getattr(self, f"action_label_{i}"),
            "action_type": getattr(self, f"action_type_{i}"),
            "snooze_minutes": getattr(self, f"action_snooze_minutes_{i}"),
            "snooze_message": getattr(self, f"action_snooze_message_{i}"),
        }

    def to_draft(self) -> dict:
        """검증 실패 재렌더용 draft 뷰 모델로 변환(입력값 유지)."""
        return {
            "name": self.name,
            "is_active": self.is_active,
            "weekdays": self.weekdays,
            "start_time": self.start_time,
            "cutoff_time": self.cutoff_time,
            "message": self.message,
            "actions": [self._action_raw(i) for i in range(1, ACTION_COUNT + 1)],
        }


# --- 검증 결과(성공 시) 담을 파싱 값 ----------------------------------------
class _ParsedAction(BaseModel):
    label: str
    action_type: ActionType
    snooze_minutes: int | None
    snooze_message: str | None


class _ParsedRule(BaseModel):
    name: str
    weekdays_csv: str
    start_time: time
    cutoff_time: time
    message: str
    is_active: bool
    actions: list[_ParsedAction]


def _parse_time(value: str) -> time | None:
    """ "HH:MM" 문자열을 time으로. 형식 오류/빈 값은 None."""
    try:
        return time.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def _validate_required_text(draft: dict, errors: list[str]) -> tuple[str, str]:
    name = draft["name"].strip()
    if not name:
        errors.append("규칙 이름을 입력해 주세요.")

    message = draft["message"].strip()
    if not message:
        errors.append("최초 알림 메시지를 입력해 주세요.")

    return name, message


def _validate_weekdays(draft: dict, errors: list[str]) -> str:
    # 알 수 없는 코드는 버리고(폼 변조 방어), 최소 1개는 선택돼야 한다.
    selected = {w for w in draft["weekdays"] if w in _WEEKDAY_CODES}
    if not selected:
        errors.append("알림 요일을 하나 이상 선택해 주세요.")

    # 저장 순서를 월→일 canonical로 정규화.
    return ",".join(code for code, _ in WEEKDAYS if code in selected)


def _validate_time_range(
    draft: dict, errors: list[str]
) -> tuple[time | None, time | None]:
    start = _parse_time(draft["start_time"])
    cutoff = _parse_time(draft["cutoff_time"])
    if start is None:
        errors.append("시작 시각을 입력해 주세요.")
    if cutoff is None:
        errors.append("컷오프 시각을 입력해 주세요.")
    # 저녁 시간대 전제 — 자정을 넘는 컷오프(예: 23:00→01:00)는 v1 범위 밖(스펙 명시).
    if start is not None and cutoff is not None and cutoff <= start:
        errors.append(
            "컷오프 시각은 시작 시각보다 늦어야 합니다 (자정을 넘는 컷오프는 지원하지 않습니다)."
        )

    return start, cutoff


def _validate_action(
    idx: int, raw: dict[str, str], errors: list[str]
) -> _ParsedAction | None:
    label = raw["label"].strip()
    if not label:
        errors.append(f"버튼 {idx}의 라벨을 입력해 주세요.")

    type_value = raw["action_type"]
    try:
        action_type = ActionType(type_value)
    except ValueError:
        errors.append(f"버튼 {idx}의 액션 타입이 올바르지 않습니다.")
        return None

    snooze_minutes: int | None = None
    snooze_message: str | None = None
    if action_type is ActionType.SNOOZE:
        minutes = _parse_int(raw["snooze_minutes"])
        if minutes is None or minutes < 1:
            errors.append(f"버튼 {idx}(지연)의 지연 시간을 1분 이상으로 입력해 주세요.")
        else:
            snooze_minutes = minutes
        # 빈 문구는 None으로 저장 → 재알림 시 규칙 기본 메시지로 대체(F-06).
        snooze_message = raw["snooze_message"].strip() or None

    return _ParsedAction(
        label=label,
        action_type=action_type,
        snooze_minutes=snooze_minutes,
        snooze_message=snooze_message,
    )


def _validate(draft: dict) -> tuple[list[str], _ParsedRule | None]:
    """draft를 서버측 검증한다 (스펙 F-03 §3).

    반환: (에러 메시지 리스트, 성공 시 파싱된 규칙 | 실패 시 None).
    에러가 하나라도 있으면 두 번째 값은 None이며 호출부는 폼을 재렌더링한다.
    """
    errors: list[str] = []

    name, message = _validate_required_text(draft, errors)
    weekdays_csv = _validate_weekdays(draft, errors)
    start, cutoff = _validate_time_range(draft, errors)

    actions: list[_ParsedAction] = []
    for idx, raw in enumerate(draft["actions"], start=1):
        action = _validate_action(idx, raw, errors)
        if action is not None:
            actions.append(action)

    if errors:
        return errors, None

    # 여기 도달 = 위 필수 항목이 모두 통과 → start/cutoff는 not None 보장.
    assert start is not None and cutoff is not None
    return [], _ParsedRule(
        name=name,
        weekdays_csv=weekdays_csv,
        start_time=start,
        cutoff_time=cutoff,
        message=message,
        is_active=draft["is_active"],
        actions=actions,
    )


# --- draft 뷰 모델 빌더 (신규/수정) -----------------------------------------
def _blank_draft() -> dict:
    """새 규칙 폼의 기본값 (UI 설계 blankRule()과 동일)."""
    return {
        "name": "",
        "is_active": True,
        "weekdays": ["mon", "tue", "wed", "thu", "fri"],
        "start_time": "20:00",
        "cutoff_time": "23:00",
        "message": "",
        "actions": [
            {
                "label": "하는중",
                "action_type": "complete",
                "snooze_minutes": "5",
                "snooze_message": "",
            },
            {
                "label": "나중에",
                "action_type": "snooze",
                "snooze_minutes": "5",
                "snooze_message": "",
            },
            {
                "label": "안해",
                "action_type": "abandon",
                "snooze_minutes": "5",
                "snooze_message": "",
            },
        ],
    }


def _draft_from_rule(rule: Rule) -> dict:
    """기존 규칙을 폼 draft로 변환. 버튼이 3개 미만이어도 3칸을 채운다(고정 3버튼 UI)."""
    actions: list[dict] = []
    for action in rule.actions:  # 관계는 sort_order 오름차순 정렬됨
        actions.append(
            {
                "label": action.label,
                "action_type": action.action_type.value,
                "snooze_minutes": ""
                if action.snooze_minutes is None
                else str(action.snooze_minutes),
                "snooze_message": action.snooze_message or "",
            }
        )
    # 3칸 고정: 부족하면 빈 버튼으로 패딩(방어적 — 정상 데이터는 항상 3개).
    while len(actions) < ACTION_COUNT:
        actions.append(
            {
                "label": "",
                "action_type": "complete",
                "snooze_minutes": "",
                "snooze_message": "",
            }
        )

    return {
        "name": rule.name,
        "is_active": rule.is_active,
        "weekdays": rule.weekday_list,
        "start_time": rule.start_time.strftime("%H:%M"),
        "cutoff_time": rule.cutoff_time.strftime("%H:%M"),
        "message": rule.message,
        "actions": actions[:ACTION_COUNT],
    }


def _form_context(
    request: Request, *, draft: dict, rule_id: int | None, errors: list[str]
) -> dict:
    """규칙 폼 렌더 컨텍스트를 한 곳에서 구성 (신규/수정/에러 공통)."""
    return {
        "request": request,
        "draft": draft,
        "rule_id": rule_id,
        "errors": errors,
        "weekdays": WEEKDAYS,
        "action_type_choices": ACTION_TYPE_CHOICES,
        "action_count": ACTION_COUNT,
        "is_editing": rule_id is not None,
        "active_tab": "list",
    }


def _persist_actions(db: DBSession, rule_id: int, actions: list[_ParsedAction]) -> None:
    """파싱된 액션들을 sort_order 0,1,2로 삽입한다."""
    for order, action in enumerate(actions):
        db.add(
            RuleAction(
                rule_id=rule_id,
                sort_order=order,
                label=action.label,
                action_type=action.action_type,
                snooze_minutes=action.snooze_minutes,
                snooze_message=action.snooze_message,
            )
        )


# --- 라우트 -----------------------------------------------------------------
@router.get("/", response_class=HTMLResponse, name="index")
async def index(
    request: Request,
    db: Annotated[DBSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """규칙 목록 (rules_list.html). base.html이 url_for('index')로 참조."""
    # col()로 감싸 pyrefly가 Rule.id를 컬럼 표현식으로 인식하게 한다(SQLModel 관용).
    rules = list(db.exec(select(Rule).order_by(col(Rule.id))).all())
    # 카드의 14일 미니바·완료율은 이력 화면(F-07)과 **같은 집계 함수**로 채운다(스펙 §3).
    # "오늘"은 F-06과 동일한 기준 tz로 정해 두 화면의 윈도우가 어긋나지 않게 한다.
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    summaries = {rule.id: summarize_rule(db, rule, today=today) for rule in rules}
    return templates.TemplateResponse(
        request,
        "rules_list.html",
        {
            "rules": rules,
            "summaries": summaries,
            "status_labels": STATUS_LABELS,
            "weekdays": WEEKDAYS,
            "action_type_labels": ACTION_TYPE_LABELS,
            "active_tab": "list",
        },
    )


@router.get("/rules/new", response_class=HTMLResponse, name="new_rule")
async def new_rule(request: Request) -> HTMLResponse:
    """빈 규칙 폼. base.html의 '+ 새 규칙' 버튼이 url_for('new_rule')로 참조."""
    return templates.TemplateResponse(
        request,
        RULE_FORM_TEMPLATE,
        _form_context(request, draft=_blank_draft(), rule_id=None, errors=[]),
    )


# response_model=None: 반환 타입이 Response 유니온(HTML 재렌더 | 리다이렉트)이라
# FastAPI가 응답 모델을 자동 생성하지 못한다. 우리는 Response를 직접 반환하므로 끈다.
@router.post("/rules", name="create_rule", response_model=None)
async def create_rule(
    request: Request,
    form: Annotated[RuleForm, Form()],
    db: Annotated[DBSession, Depends(get_db_session)],
) -> HTMLResponse | RedirectResponse:
    """규칙 생성. 성공 시 목록으로 303 redirect, 실패 시 폼 재렌더."""
    draft = form.to_draft()
    errors, parsed = _validate(draft)
    if parsed is None:
        logger.info("규칙 생성 검증 실패 — error_count=%d", len(errors))
        return templates.TemplateResponse(
            request,
            RULE_FORM_TEMPLATE,
            _form_context(request, draft=draft, rule_id=None, errors=errors),
        )

    rule = Rule(
        name=parsed.name,
        weekdays=parsed.weekdays_csv,
        start_time=parsed.start_time,
        message=parsed.message,
        cutoff_time=parsed.cutoff_time,
        is_active=parsed.is_active,
    )
    db.add(rule)
    db.flush()  # rule.id 확보 후 액션에 FK로 사용
    assert rule.id is not None
    rule_id = rule.id
    _persist_actions(db, rule_id, parsed.actions)
    db.commit()
    logger.info(
        "규칙 생성 완료 — rule_id=%d is_active=%s weekdays=%s start_time=%s "
        "cutoff_time=%s action_count=%d",
        rule_id,
        parsed.is_active,
        parsed.weekdays_csv,
        parsed.start_time.isoformat(),
        parsed.cutoff_time.isoformat(),
        len(parsed.actions),
    )

    return RedirectResponse(url=str(request.url_for("index")), status_code=303)


@router.get(
    "/rules/{rule_id}/edit",
    response_class=HTMLResponse,
    name="edit_rule",
    responses=RULE_NOT_FOUND_RESPONSES,
)
async def edit_rule(
    request: Request,
    rule_id: int,
    db: Annotated[DBSession, Depends(get_db_session)],
) -> HTMLResponse:
    """기존 값을 채운 수정 폼."""
    rule = db.get(Rule, rule_id)
    if rule is None:
        logger.warning("규칙 수정 실패 — rule_id=%d reason=not_found", rule_id)
        raise HTTPException(status_code=404, detail=RULE_NOT_FOUND_DETAIL)
    return templates.TemplateResponse(
        request,
        RULE_FORM_TEMPLATE,
        _form_context(
            request, draft=_draft_from_rule(rule), rule_id=rule_id, errors=[]
        ),
    )


@router.post(
    "/rules/{rule_id}",
    name="update_rule",
    response_model=None,
    responses=RULE_NOT_FOUND_RESPONSES,
)
async def update_rule(
    request: Request,
    rule_id: int,
    form: Annotated[RuleForm, Form()],
    db: Annotated[DBSession, Depends(get_db_session)],
) -> HTMLResponse | RedirectResponse:
    """규칙 수정. RuleAction은 delete-then-insert로 전체 교체(스펙 F-03 §4)."""
    rule = db.get(Rule, rule_id)
    if rule is None:
        logger.warning("규칙 수정 실패 — rule_id=%d reason=not_found", rule_id)
        raise HTTPException(status_code=404, detail=RULE_NOT_FOUND_DETAIL)

    draft = form.to_draft()
    errors, parsed = _validate(draft)
    if parsed is None:
        logger.info(
            "규칙 수정 검증 실패 — rule_id=%d error_count=%d", rule_id, len(errors)
        )
        return templates.TemplateResponse(
            request,
            RULE_FORM_TEMPLATE,
            _form_context(request, draft=draft, rule_id=rule_id, errors=errors),
        )

    rule.name = parsed.name
    rule.weekdays = parsed.weekdays_csv
    rule.start_time = parsed.start_time
    rule.message = parsed.message
    rule.cutoff_time = parsed.cutoff_time
    rule.is_active = parsed.is_active

    # 액션 전체 교체: 부분 diff보다 단순하고 영향 범위가 명확하다(진행 세션은
    # session_id+action_id로 동작). 기존 3개 삭제 → flush → 새 3개 삽입.
    for action in rule.actions:
        db.delete(action)
    db.flush()
    _persist_actions(db, rule_id, parsed.actions)
    db.commit()
    logger.info(
        "규칙 수정 완료 — rule_id=%d is_active=%s weekdays=%s start_time=%s "
        "cutoff_time=%s action_count=%d",
        rule_id,
        parsed.is_active,
        parsed.weekdays_csv,
        parsed.start_time.isoformat(),
        parsed.cutoff_time.isoformat(),
        len(parsed.actions),
    )

    return RedirectResponse(url=str(request.url_for("index")), status_code=303)


@router.post(
    "/rules/{rule_id}/toggle",
    name="toggle_rule",
    responses=RULE_NOT_FOUND_RESPONSES,
)
async def toggle_rule(
    request: Request,
    rule_id: int,
    db: Annotated[DBSession, Depends(get_db_session)],
) -> RedirectResponse:
    """목록의 토글 스위치 — is_active 반전 후 목록으로 redirect (UC-03)."""
    rule = db.get(Rule, rule_id)
    if rule is None:
        logger.warning(
            "규칙 활성 상태 변경 실패 — rule_id=%d reason=not_found", rule_id
        )
        raise HTTPException(status_code=404, detail=RULE_NOT_FOUND_DETAIL)
    rule.is_active = not rule.is_active
    is_active = rule.is_active
    db.add(rule)
    db.commit()
    logger.info(
        "규칙 활성 상태 변경 완료 — rule_id=%d is_active=%s",
        rule_id,
        is_active,
    )
    return RedirectResponse(url=str(request.url_for("index")), status_code=303)


@router.post(
    "/rules/{rule_id}/delete",
    name="delete_rule",
    responses=RULE_NOT_FOUND_RESPONSES,
)
async def delete_rule(
    request: Request,
    rule_id: int,
    db: Annotated[DBSession, Depends(get_db_session)],
) -> RedirectResponse:
    """규칙 삭제. 연관 RuleAction/세션은 cascade_delete로 함께 제거된다."""
    rule = db.get(Rule, rule_id)
    if rule is None:
        logger.warning("규칙 삭제 실패 — rule_id=%d reason=not_found", rule_id)
        raise HTTPException(status_code=404, detail=RULE_NOT_FOUND_DETAIL)
    db.delete(rule)
    db.commit()
    logger.info("규칙 삭제 완료 — rule_id=%d", rule_id)
    return RedirectResponse(url=str(request.url_for("index")), status_code=303)
