"""공유 Jinja2 템플릿 인스턴스.

`main.py`와 각 라우터(`routers/*.py`)가 같은 `Jinja2Templates`를 써야 하는데,
이를 `main.py`에 두면 `main → router → main` 순환 import가 생긴다. 그래서 템플릿
설정을 의존성이 없는 이 모듈로 분리해 양쪽에서 안전하게 import한다.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

# app/ 디렉터리 기준으로 경로를 해석 — 실행 CWD(개발 서버, systemd, 테스트)와 무관하게
# templates/·static/을 찾게 한다.
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
