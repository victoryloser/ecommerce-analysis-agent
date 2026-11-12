"""每轮事件与调用预算。记录后重抛异常，不改变执行结果。"""
import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from app.config import RUNS_DIR


@dataclass
class TurnTrace:
    session_id: str
    dataset_id: str
    question: str
    model: str
    turn_id: str = field(default_factory=lambda: uuid4().hex)
    steps: int = 0
    model_calls: int = 0
    sequence: int = 0
    stage: str = "start"

    @property
    def event_file(self):
        return RUNS_DIR / self.dataset_id / "events" / f"{self.session_id}.jsonl"

    def record(self, event: str, **payload):
        self.sequence += 1
        self.stage = event
        row = {"turn_id": self.turn_id, "session_id": self.session_id, "dataset_id": self.dataset_id,
               "sequence": self.sequence, "time": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
        self.event_file.parent.mkdir(parents=True, exist_ok=True)
        with self.event_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def consume(self, kind: str):
        if self.steps >= 8:
            raise ValueError("本轮已达到8次模型/元数据/查询调用上限")
        self.steps += 1
        if kind == "model":
            self.model_calls += 1
        self.record("budget", kind=kind, steps=self.steps, model_calls=self.model_calls)

    def __enter__(self):
        self.record("turn_started", question=self.question, model=self.model)
        return self

    def __exit__(self, error_type, error, tb):
        if error is not None:
            # HTTP只记状态与错误类型，不写Authorization、URL参数或响应正文。
            frames = [{"file": f.filename, "line": f.lineno, "function": f.name} for f in traceback.extract_tb(tb)]
            self.record("turn_failed", failed_stage=self.stage, error_type=error_type.__name__,
                        error_message=str(error) if isinstance(error, ValueError) else None,
                        http_status=error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None,
                        frames=frames, steps=self.steps, model_calls=self.model_calls)
        else:
            self.record("turn_finished", steps=self.steps, model_calls=self.model_calls)
        return False
