# cspell:ignore statm cgroup faulthandler getsignal
"""종료 원인 추적 프로브 (issue #1).

증상: 정상 동작하던 프로세스가 traceback 없이 lifespan 정리 코드로 진입해 종료된다.
기존 "앱 종료 시작" 로그로는 원인을 알 수 없다 — 그건 원인이 아니라 **결과**다.
uvicorn의 종료 경로는 `시그널 → handle_exit()가 should_exit=True → serve 루프 종료 →
ASGI lifespan shutdown` 이라서, 우리 로그가 찍힐 땐 방아쇠가 이미 당겨진 뒤다.
따라서 증거는 그 **이전**에 남겨야 한다. 세 갈래로 남긴다:

1. 시그널 — uvicorn이 `capture_signals()`에서 이미 건 SIGTERM/SIGINT 핸들러 **앞에**
   로깅 핸들러를 끼우고 원래 핸들러로 넘긴다(체이닝). 플랫폼이 보낸 시그널이면 여기
   반드시 걸리고, 어떤 코드를 실행하다 끊겼는지 프레임까지 남는다. 반드시 체이닝이어야
   한다 — 그냥 `signal.signal()`로 덮으면 uvicorn의 graceful shutdown을 없애버려서
   로깅 추가가 동작 변경이 되어버린다. uvicorn이 다루지 않는 SIGHUP/SIGQUIT/SIGABRT는
   지금 앱 로그를 한 줄도 남기지 않고 죽으므로, 로깅 후 기본 동작으로 되돌려준다.
2. 프로세스 스냅샷 — pid/ppid/가동시간/RSS/cgroup 메모리. ppid가 1로 바뀌었다면 감독
   프로세스가 먼저 죽어 재양육된 것이고, cgroup `memory.events`의 oom_kill/max가 0이
   아니면 메모리 압박이 원인이다. 둘 다 애플리케이션 로그만 봐서는 보이지 않는다.
3. 종료 시점 대조 — lifespan 정리에서 "직전에 받은 시그널"을 함께 남긴다. 시그널이
   **없었는데** 종료가 시작됐다면 원인은 플랫폼 시그널이 아니라 서버 내부(should_exit)
   나 런타임이라는 뜻이므로, 그 부재 자체가 가장 강한 단서다.
"""

import atexit
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from types import FrameType

logger = logging.getLogger(__name__)

# 가동 시간의 기준점. import 시점 = 프로세스 시작 직후로 봐도 무방하다.
_boot_monotonic = time.monotonic()
_boot_ppid = os.getppid()

# 마지막으로 받은 시그널. 종료 로그가 "시그널 때문인지"를 판정하는 유일한 근거다.
_last_signal: tuple[str, float] | None = None

# uvicorn이 직접 처리하는 시그널 → 로깅 후 원래 핸들러로 위임한다.
_CHAINED_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# uvicorn이 다루지 않는 시그널 → 로깅 후 기본 동작(대개 프로세스 종료)으로 되돌린다.
_LOG_ONLY_SIGNAL_NAMES = ("SIGHUP", "SIGQUIT", "SIGABRT", "SIGUSR1", "SIGUSR2")


def install_shutdown_probe() -> None:
    """시그널 로깅 핸들러와 exit 마커를 건다. lifespan 기동에서 1회 호출.

    호출 시점이 중요하다: uvicorn의 `capture_signals()`는 `_serve()` 전체를 감싸므로
    lifespan 기동이 도는 시점엔 uvicorn 핸들러가 **이미** 걸려 있다. 그래서 여기서
    `getsignal()`로 그걸 집어 우리 핸들러 뒤에 체인으로 매달 수 있다.
    """
    faulthandler.enable()  # SIGSEGV/SIGFPE/SIGABRT 등 하드 크래시 시 stderr에 스택 덤프

    if threading.current_thread() is not threading.main_thread():
        # 시그널 핸들러는 메인 스레드에서만 걸 수 있다(테스트 러너 등에서 발생 가능).
        logger.warning("종료 프로브 설치 건너뜀 — reason=not_main_thread")
        return

    for sig in _CHAINED_SIGNALS:
        previous = signal.getsignal(sig)
        signal.signal(sig, _make_chained_handler(previous))
        logger.info(
            "종료 프로브 설치 — signal=%s chained_to=%s",
            sig.name,
            getattr(previous, "__qualname__", repr(previous)),
        )

    for name in _LOG_ONLY_SIGNAL_NAMES:
        sig = getattr(signal, name, None)
        if sig is None:
            continue  # 플랫폼에 없는 시그널(Windows 등)
        signal.signal(sig, _handle_log_only_signal)

    atexit.register(_log_process_exit)
    logger.info("종료 프로브 설치 완료 — %s", snapshot_line())


def _make_chained_handler(previous: object):
    """로깅 후 uvicorn의 원래 핸들러로 넘기는 핸들러를 만든다."""

    def _handler(signum: int, frame: FrameType | None) -> None:
        _record_signal(signum, frame, disposition="uvicorn_graceful_shutdown")
        if callable(previous):
            previous(signum, frame)  # uvicorn handle_exit → should_exit=True
        elif previous is signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    return _handler


def _handle_log_only_signal(signum: int, frame: FrameType | None) -> None:
    """uvicorn이 안 보는 시그널을 기록만 하고 기본 동작을 그대로 재현한다.

    기본 동작으로 되돌린 뒤 자신에게 다시 쏘는 이유: 여기서 삼켜버리면 로깅을 추가하려다
    프로세스 동작을 바꾸게 된다. 로그만 남기고 원래 죽을 방식대로 죽어야 한다.
    """
    _record_signal(signum, frame, disposition="default_action")
    signal.signal(signum, signal.SIG_DFL)
    signal.raise_signal(signum)


def _record_signal(signum: int, frame: FrameType | None, *, disposition: str) -> None:
    """시그널 수신 사실 + 중단된 지점을 남긴다.

    프레임 스택을 함께 남기는 이유: 어떤 코드를 실행하다 끊겼는지(예: tick의 ntfy 발행
    await 중인지, idle 상태인지)가 "우리가 유발했는지 vs 밖에서 왔는지"를 가른다.
    """
    global _last_signal
    name = signal.Signals(signum).name
    _last_signal = (name, time.monotonic())
    line = f"시그널 수신 — signal={name}({signum}) disposition={disposition} {snapshot_line()}"

    # 표준 로거보다 먼저 fd 2에 직접 쓴다: 로그가 파일/파이프로 리다이렉트되면 stdout이
    # 블록 버퍼링되는데, 기본 동작으로 죽는 시그널은 프로세스를 즉시 끝내므로 버퍼에 남은
    # 줄이 통째로 유실된다 — 정작 진단이 필요한 순간에만 로그가 사라진다. os.write는
    # 버퍼를 거치지 않아 죽기 직전에도 남는다.
    _write_stderr(f"[shutdown-probe] {line}\n")
    logger.warning("%s", line)  # Logfire 등 정식 로깅 경로에도 싣는다
    if frame is not None:
        stack = "".join(traceback.format_stack(frame)).rstrip()
        _write_stderr(
            f"[shutdown-probe] 시그널 수신 지점 스택 — signal={name}\n{stack}\n"
        )
        logger.warning("시그널 수신 지점 스택 — signal=%s\n%s", name, stack)
    _flush_logs()


def _write_stderr(text: str) -> None:
    """버퍼를 우회해 fd 2에 직접 쓴다(시그널 핸들러 안에서도 안전)."""
    try:
        os.write(2, text.encode("utf-8", errors="replace"))
    except OSError:
        pass  # 진단 로깅이 앱 종료 경로를 깨뜨리면 안 된다


def _flush_logs() -> None:
    """죽기 전에 버퍼에 남은 로그를 밀어낸다. 실패해도 종료 경로를 막지 않는다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except (OSError, ValueError):
            pass


def shutdown_cause_line() -> str:
    """lifespan 종료 로그에 붙일 "왜 종료되는가" 근거 한 줄.

    시그널이 없었다면 그 자체가 결론이다 — 플랫폼이 보낸 종료 신호가 아니라 서버 내부
    또는 런타임에서 종료가 시작됐다는 뜻이므로 조사 방향이 완전히 갈린다.
    """
    if _last_signal is None:
        return (
            "shutdown_trigger=no_signal_received "
            "(시그널 없이 종료 시작 → uvicorn 내부/런타임 원인 의심)"
        )
    name, at = _last_signal
    return f"shutdown_trigger={name} seconds_since_signal={time.monotonic() - at:.3f}"


def _log_process_exit() -> None:
    """인터프리터가 **시그널 없이** 종료될 때만 찍히는 마커.

    시그널로 죽는 경로에선 이 줄이 나오지 않는다 — uvicorn은 graceful shutdown 후 원래
    핸들러를 복원하고 그 시그널을 자신에게 다시 쏘므로(종료 코드 128+N), 프로세스가
    시그널 기본 동작으로 죽어 atexit이 실행되지 않는다. 그러니 "이 줄의 부재"를 SIGKILL로
    읽으면 안 된다. 반대로 **이 줄이 보이면** 시그널이 아닌 경로(sys.exit/예외/런타임)로
    끝났다는 뜻이라, 이번 이슈에서 갈라야 할 바로 그 경우를 짚어준다.
    """
    line = f"프로세스 종료(atexit) — {shutdown_cause_line()} {snapshot_line()}"
    _write_stderr(f"[shutdown-probe] {line}\n")
    logger.warning("%s", line)
    _flush_logs()


def snapshot_line() -> str:
    """프로세스 상태 스냅샷을 로그용 key=value 한 줄로 만든다."""
    fields = [
        f"pid={os.getpid()}",
        f"ppid={os.getppid()}",
        f"boot_ppid={_boot_ppid}",
        f"uptime_seconds={time.monotonic() - _boot_monotonic:.1f}",
    ]
    rss_mb = _rss_mb()
    if rss_mb is not None:
        fields.append(f"rss_mb={rss_mb:.1f}")
    fields.extend(_cgroup_memory_fields())
    return " ".join(fields)


def _rss_mb() -> float | None:
    """현재 RSS(MB). 컨테이너(Linux)에서만 읽히고 macOS 로컬에선 None."""
    try:
        # statm 2번째 필드 = resident pages.
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return resident_pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024


def _cgroup_memory_fields() -> list[str]:
    """cgroup v2 메모리 사용/한도/이벤트.

    `memory.events`의 `oom_kill`/`max`가 0이 아니면 컨테이너가 메모리 한도에 부딪힌
    것이다. 이건 애플리케이션 예외로 나타나지 않기 때문에, 로그에 안 찍으면 "원인 없이
    죽었다"로만 보인다 — 이번 이슈에서 배제하거나 확정해야 할 가설이라 함께 남긴다.
    """
    fields: list[str] = []
    current = _read_int("/sys/fs/cgroup/memory.current")
    if current is not None:
        fields.append(f"cgroup_mem_mb={current / 1024 / 1024:.1f}")
    limit = _read_text("/sys/fs/cgroup/memory.max")
    if limit is not None:
        fields.append(
            "cgroup_mem_limit_mb="
            + ("max" if limit == "max" else f"{int(limit) / 1024 / 1024:.1f}")
        )
    events = _read_text("/sys/fs/cgroup/memory.events")
    if events is not None:
        counters = dict(
            line.split(maxsplit=1) for line in events.splitlines() if " " in line
        )
        for key in ("max", "oom", "oom_kill"):
            if key in counters:
                fields.append(f"cgroup_mem_{key}_events={counters[key]}")
    return fields


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None  # cgroup v1이거나 리눅스가 아님 → 조용히 생략


def _read_int(path: str) -> int | None:
    text = _read_text(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None
