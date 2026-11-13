"""Lifetime and ownership of one authoring sandbox (no Harbor dependency)."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sandbox_runtime import (LIFETIME_MINUTES, RENEW_INTERVAL, STARTUP_TIMEOUT,
                              SandboxClient, SandboxError, author_lifetime_minutes, verified_expiry)
from .sandbox_transfer import checked


class AuthoringSandbox:
    def __init__(self, config, run_root: Path, slug: str, cancelled: threading.Event):
        self.client = SandboxClient()
        self.cancelled = cancelled
        self.stop_renewing = threading.Event()
        self.renew_error = None
        self.renew_thread = None
        self.id = None
        self.name = "tb-author-" + uuid.uuid4().hex
        self.path = run_root / "sandbox-workers" / (slug + ".json")
        self.lock = threading.Lock()
        self.state = {"name": self.name, "role": "author", "source": slug,
                      "batch": run_root.name, "base_url": self.client.base_url,
                      "resource": self.client.resource, "retained": False,
                      "image": config.extra["sandbox_author_image"],
                      "cpu": config.cpu, "memory_mb": config.memory_mb,
                      "task_timeout_sec": config.task_timeout_sec,
                      "lifecycle_minutes": author_lifetime_minutes(config.task_timeout_sec)}
        self.save(phase="prepared")

    def save(self, **changes):
        with self.lock:
            self.state.update(changes, updated_at=datetime.now(timezone.utc).isoformat())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))
            temporary.replace(self.path)

    def check(self):
        if self.cancelled.is_set():
            raise SandboxError("用户中断了本轮任务")
        if self.renew_error:
            raise SandboxError(self.renew_error)

    def command(self, command, **kwargs):
        return checked(self.client, self.id, command, **kwargs)

    def start(self):
        self.check()
        end = time.monotonic() + STARTUP_TIMEOUT
        lifetime = self.state["lifecycle_minutes"]
        requested_expiry = datetime.now(timezone.utc) + timedelta(minutes=lifetime)
        payload = {
            "name": self.name, "resource": self.client.resource,
            "metadata": {"task_builder_run": self.name, "task_builder_role": "author",
                         "task_builder_batch": self.state["batch"], "task_builder_source": self.state["source"]},
            "image": {"uri": self.state["image"]}, "entrypoint": ["sleep", "inf"],
            "resources": {"cpu": str(self.state["cpu"]), "memory": f"{self.state['memory_mb']}Mi"},
            "ports": [{"containerPort": 44772, "name": "execd", "protocol": "tcp", "purpose": "system service"}],
            "lifecycleMinutes": lifetime, "networkPolicy": {"defaultAction": "allow", "egress": []},
        }
        self.save(phase="creating")
        try:
            result = self.client.json("POST", "/api/v1/sandboxes", payload, timeout=1800, retry=False, deadline=end)
        except SandboxError as exc:
            if exc.status is not None and 400 <= exc.status < 500 and exc.status not in {408, 429}:
                raise
            self.save(phase="create-uncertain")
            while time.monotonic() < end:
                found = self.client.find_created(self.name, end)
                if len(found) == 1:
                    result = found[0]
                    break
                if len(found) > 1:
                    raise SandboxError("创建结果不唯一，请按本轮记录中的名称检查；未重复创建")
                if self.cancelled.wait(10):
                    raise SandboxError("创建已中断，实例状态尚未确认；请按本轮记录中的名称检查")
            else:
                raise SandboxError("创建状态尚未确认；请按本轮记录中的名称检查")
        self.id = result.get("id") or (result.get("sandbox") or {}).get("id")
        if not self.id:
            raise SandboxError("创建响应缺少沙盒编号")
        self.save(phase="starting", sandbox_id=self.id,
                  requested_expires_at=requested_expiry.isoformat())
        self.renew_thread = threading.Thread(target=self._renew, daemon=True)
        self.renew_thread.start()
        notice = 0
        while time.monotonic() < end:
            self.check()
            detail = self.client.detail(self.id, deadline=end)
            state = (detail.get("status") or {}).get("state") or detail.get("state")
            if time.monotonic() - notice >= 60:
                print(f"{self.state['source']}：造任务沙盒 {self.id}，状态 {state}", flush=True)
                notice = time.monotonic()
            if state in {"Failed", "Error", "Deleted", "Terminated"}:
                raise SandboxError(f"造任务沙盒启动失败：{state}")
            if state == "Running":
                self.save(expires_at=verified_expiry(detail, requested_expiry))
                try:
                    self.command("true", deadline=end)
                    self.save(phase="running")
                    return
                except SandboxError as exc:
                    if exc.status in {401, 403}:
                        raise
            self.cancelled.wait(10)
        raise SandboxError("造任务沙盒启动超过 60 分钟")

    def renew_once(self):
        checked_at = datetime.now(timezone.utc)
        expires = checked_at + timedelta(minutes=LIFETIME_MINUTES)
        # Long jobs already receive their full lifetime at creation. Sending the
        # same expiry again is unnecessary and can be rejected by the platform.
        current = self.state.get("expires_at")
        if current and datetime.fromisoformat(current.replace("Z", "+00:00")) >= expires:
            self.save(lifetime_checked_at=checked_at.isoformat(), renewal_skipped="existing_lifetime_sufficient")
            return
        confirmed = self.client.renew(self.id, expires, deadline=time.monotonic() + 900)
        self.save(expires_at=confirmed, last_renewed_at=datetime.now(timezone.utc).isoformat(),
                  renewal_skipped=None, renew_warning=None)

    def _renew(self):
        while not self.stop_renewing.wait(RENEW_INTERVAL):
            try:
                self.renew_once()
            except Exception as exc:
                current = self.state.get("expires_at")
                if current and datetime.fromisoformat(current.replace("Z", "+00:00")) > datetime.now(timezone.utc) + timedelta(seconds=RENEW_INTERVAL + 600):
                    # A failed extension does not cancel the already confirmed
                    # allocation. Retry next hour while there is time to recover.
                    self.save(renew_warning=str(exc), last_renew_attempt_at=datetime.now(timezone.utc).isoformat())
                    continue
                self.renew_error = "造任务沙盒续期失败，正在回传已有结果：" + str(exc)
                self.save(renew_failed=True, renew_error=str(exc))
                return

    def clean_children(self):
        failures = []
        if not self.id:
            return failures
        for child in self.client.children(self.id):
            child_id = child.get("id")
            if not child_id:
                continue
            try:
                self.client.delete(child_id)
            except Exception:
                failures.append(child_id)
        self.save(remaining_children=failures)
        return failures

    def close(self, *, retain=False):
        self.stop_renewing.set()
        failures = []
        try:
            failures = self.clean_children()
        except Exception:
            failures = ["子沙盒查询失败"]
        if self.id and not retain:
            try:
                self.client.delete(self.id)
                self.save(phase="deleted", retained=False)
            except Exception:
                failures.append(self.id)
                self.save(phase="cleanup-failed", retained=True)
        elif self.id:
            try:
                detail = self.client.detail(self.id)
            except SandboxError as exc:
                if exc.status != 404:
                    self.save(phase="retention-unconfirmed", retained=False, retention_error=str(exc))
                    raise
                detail = {"state": "Deleted"}
            state = (detail.get("status") or {}).get("state") or detail.get("state")
            if state in {"Deleted", "Terminated", "Failed", "Error"}:
                self.save(phase="missing", retained=False, platform_state=state)
            else:
                self.save(phase="retained-for-recovery", retained=True, platform_state=state)
        self.save(cleanup_failures=failures)
        if failures:
            raise SandboxError("部分沙盒尚未确认删除，详见 " + str(self.path))
