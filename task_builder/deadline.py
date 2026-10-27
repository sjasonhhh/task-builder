from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar

from .models import TaskTimeout


_deadline: ContextVar[float | None] = ContextVar("task_deadline", default=None)


def remaining_timeout(timeout: float) -> float:
    deadline = _deadline.get()
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TaskTimeout("造任务总时间已用完")
    return min(timeout, remaining)


def has_deadline() -> bool:
    return _deadline.get() is not None


@contextmanager
def task_deadline(seconds: float):
    started = time.monotonic()
    token = _deadline.set(started + seconds)
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(signum, frame):
        raise TaskTimeout(f"造任务超过总时限 {seconds:g} 秒；已停止本任务")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        _deadline.reset(token)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


def run_process(command, **kwargs):
    timeout = remaining_timeout(kwargs.pop("timeout"))
    kwargs.pop("check", None)
    input_text = kwargs.pop("input", None)
    if input_text is not None:
        kwargs["stdin"] = subprocess.PIPE
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL if isinstance(error, (TaskTimeout, subprocess.TimeoutExpired)) else signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            error.output = stdout
            error.stderr = stderr
            try:
                remaining_timeout(1)
            except TaskTimeout as expired:
                expired.stdout = stdout
                expired.stderr = stderr
                raise expired from error
        if isinstance(error, TaskTimeout):
            error.stdout = stdout
            error.stderr = stderr
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
