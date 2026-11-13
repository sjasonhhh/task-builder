from __future__ import annotations

import os
import signal
import subprocess
import time
import queue
import threading
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
    on_stdout = kwargs.pop("on_stdout", None)
    on_poll = kwargs.pop("on_poll", None)
    timeout = remaining_timeout(kwargs.pop("timeout"))
    kwargs.pop("check", None)
    input_text = kwargs.pop("input", None)
    if input_text is not None:
        kwargs["stdin"] = subprocess.PIPE
    process = subprocess.Popen(command, start_new_session=True, **kwargs)
    if on_stdout is not None or on_poll is not None:
        return _observe_process(process, command, input_text, timeout, on_stdout, on_poll)
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


def _observe_process(process, command, input_text, timeout, on_stdout, on_poll):
    """Drain both pipes while delivering stdout lines and checking the task deadline."""
    messages = queue.Queue()
    collected = {"stdout": [], "stderr": []}
    readers = []

    def read_pipe(name):
        pipe = getattr(process, name)
        try:
            if pipe is not None:
                for line in iter(pipe.readline, "" if process.text_mode else b""):
                    messages.put((name, line))
        finally:
            if pipe is not None:
                pipe.close()

    def write_input():
        try:
            process.stdin.write(input_text)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    def drain():
        while True:
            try:
                name, line = messages.get_nowait()
            except queue.Empty:
                return
            collected[name].append(line)
            if name == "stdout" and on_stdout:
                on_stdout(line.decode(errors="replace") if isinstance(line, bytes) else line)

    for name in collected:
        thread = threading.Thread(target=read_pipe, args=(name,), daemon=True)
        thread.start()
        readers.append(thread)
    writer = None
    if input_text is not None:
        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
    started = time.monotonic()
    error = None
    try:
        while process.poll() is None or any(t.is_alive() for t in readers):
            drain()
            if on_poll:
                on_poll()
            remaining_timeout(1)
            if time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.05)
        drain()
        if on_poll:
            on_poll()
    except BaseException as exc:
        error = exc
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    finally:
        process.wait()
        for thread in readers + ([writer] if writer else []):
            thread.join(timeout=5)
        drain()
    empty = "" if process.text_mode else b""
    stdout, stderr = (empty.join(collected[name]) for name in ("stdout", "stderr"))
    if error is not None:
        if isinstance(error, subprocess.TimeoutExpired):
            try:
                remaining_timeout(1)
            except TaskTimeout as expired:
                error = expired
            else:
                error.output = stdout
        if isinstance(error, (subprocess.TimeoutExpired, TaskTimeout)):
            error.stdout, error.stderr = stdout, stderr
        raise error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
