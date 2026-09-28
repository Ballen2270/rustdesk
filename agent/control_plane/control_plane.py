"""Domain rules for the local control plane.

The browser never receives bridge credentials and never sends raw pointer input.
It creates versioned jobs; an edge runtime on the controller host is the only
component allowed to turn a job into RustDesk input.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def event(kind: str, message: str, **data: Any) -> dict[str, Any]:
    return {"time": now(), "kind": kind, "message": message, **data}


MODEL_PROFILES = [
    {"id": "local-deterministic", "label": "本地确定性运行时", "roles": ["execute"]},
    {"id": "openai-codex", "label": "Codex", "roles": ["design", "exception"]},
    {"id": "anthropic-claude", "label": "Claude", "roles": ["design", "exception"]},
]


@dataclass
class Run:
    id: str
    skill_id: str
    device_id: str
    surface_ids: list[str]
    objective: str
    target: int
    models: dict[str, str]
    status: str = "queued"
    created_at: str = field(default_factory=now)
    events: list[dict[str, Any]] = field(default_factory=list)


class ControlPlane:
    """Small, dependency-free job ledger with physical-host input leases."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._runs: dict[str, Run] = {}
        self._drafts: list[dict[str, Any]] = []
        self._host_leases: dict[str, str] = {}
        self._devices = [
            {
                "id": "pc1",
                "label": "PC 1",
                "physical_host_id": "pc1",
                "status": "ready",
                "surfaces": [
                    {"id": "pc1:display-1", "label": "显示器 1", "resolution": "1920×1080"},
                    {"id": "pc1:display-2", "label": "显示器 2", "resolution": "1920×1080"},
                ],
            }
        ]
        self._skills = [
            {
                "id": "rustdesk-jianhui",
                "name": "剑会自动匹配",
                "version": "0.1.0",
                "template": "two-team-match",
                "runtime": "local-ocr-fsm",
                "permissions": ["screenshot", "click", "keep_alive"],
                "evidence": "动作前后 PNG + 状态变化 + 关键帧",
                "status": "prototype",
            }
        ]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "devices": self._devices,
                "skills": self._skills,
                "models": MODEL_PROFILES,
                "runs": [asdict(run) for run in self._runs.values()],
                "drafts": self._drafts,
            }

    def _device(self, device_id: str) -> dict[str, Any]:
        return next((device for device in self._devices if device["id"] == device_id), {})

    def _skill(self, skill_id: str) -> dict[str, Any]:
        return next((skill for skill in self._skills if skill["id"] == skill_id), {})

    def create_run(self, request: dict[str, Any]) -> Run:
        skill_id = str(request.get("skill_id", ""))
        device_id = str(request.get("device_id", ""))
        surface_ids = request.get("surface_ids")
        target = request.get("target", 10)
        models = request.get("models", {})
        if not self._skill(skill_id):
            raise ValueError("unknown skill")
        device = self._device(device_id)
        if not device:
            raise ValueError("unknown device")
        known_surfaces = {surface["id"] for surface in device["surfaces"]}
        if not isinstance(surface_ids, list) or not surface_ids or set(surface_ids) - known_surfaces:
            raise ValueError("invalid surfaces for device")
        if len(surface_ids) != len(set(surface_ids)):
            raise ValueError("surfaces must be distinct")
        if type(target) is not int or not 1 <= target <= 10:
            raise ValueError("target must be an integer from 1 to 10")
        if not isinstance(models, dict):
            raise ValueError("models must be an object")
        allowed_roles = {profile["id"]: set(profile["roles"]) for profile in MODEL_PROFILES}
        if models.get("execute", "local-deterministic") != "local-deterministic":
            raise ValueError("model cannot fill execute role")
        for role in ("design", "exception"):
            profile = models.get(role, "local-deterministic")
            if profile != "local-deterministic" and role not in allowed_roles.get(profile, set()):
                raise ValueError(f"model cannot fill {role} role")
        run = Run(
            id=f"run-{uuid4().hex[:10]}",
            skill_id=skill_id,
            device_id=device_id,
            surface_ids=surface_ids,
            objective=str(request.get("objective", ""))[:240],
            target=target,
            models={"execute": "local-deterministic", **models},
        )
        run.events.append(event("queued", "任务已排队；等待 edge runtime 领取", physical_host_id=device["physical_host_id"]))
        with self._lock:
            self._runs[run.id] = run
        return run

    def control(self, run_id: str, command: str) -> Run:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ValueError("unknown run")
            device = self._device(run.device_id)
            host = device["physical_host_id"]
            if command == "start":
                holder = self._host_leases.get(host)
                if holder and holder != run.id:
                    raise ValueError(f"physical host is leased by {holder}")
                if run.status not in ("queued", "paused"):
                    raise ValueError("run cannot be started from its current state")
                self._host_leases[host] = run.id
                run.status = "running"
                run.events.append(event("started", "已授予物理设备输入租约", physical_host_id=host))
            elif command == "pause":
                if run.status != "running":
                    raise ValueError("only running jobs can pause")
                run.status = "paused"
                run.events.append(event("paused", "自动输入已停止；edge 应继续低频观察"))
            elif command in ("stop", "complete"):
                if run.status not in ("queued", "running", "paused"):
                    raise ValueError("run is already terminal")
                run.status = "stopped" if command == "stop" else "complete"
                if self._host_leases.get(host) == run.id:
                    self._host_leases.pop(host, None)
                run.events.append(event(run.status, "输入租约已释放；证据保留"))
            else:
                raise ValueError("unknown control command")
            return run

    def create_draft(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("name", "")).strip()
        logic = str(request.get("logic", "")).strip()
        operation = str(request.get("operation", "")).strip()
        images = request.get("images", [])
        if not name or not logic or not operation:
            raise ValueError("name, logic and operation are required")
        if not isinstance(images, list) or any(not isinstance(image, str) for image in images):
            raise ValueError("images must be a string list")
        draft = {
            "id": f"draft-{uuid4().hex[:8]}",
            "name": name[:80],
            "logic": logic[:4000],
            "operation": operation[:4000],
            "images": images[:20],
            "status": "needs-review",
            "created_at": now(),
            "next": "模型可生成 Skill Bundle 草稿；人工批准后才可发布。",
        }
        with self._lock:
            self._drafts.append(draft)
        return draft
