"""Async-safe run monitor events and rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


TEAM_ORDER = ("run", "reviewer", "librarian", "analyst", "tooling", "orchestration")
TEAM_LABELS = {
    "run": "Run",
    "reviewer": "Reviewer",
    "librarian": "Librarians",
    "analyst": "Analysts",
    "tooling": "Tools",
    "orchestration": "Orchestration",
}


def validate_chatter(chatter: int) -> int:
    """Return a validated 1-10 chatter level."""

    if not 1 <= chatter <= 10:
        raise ValueError("chatter must be between 1 and 10")
    return chatter


def default_monitor_jsonl_path(stem: str = "run") -> Path:
    """Return a timestamped monitor JSONL path in the runtime data dir."""

    root = Path(os.getenv("AUTO_RESEARCH_RUNTIME_DIR", "~/.auto_research")).expanduser()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root / "monitors" / f"{timestamp}_{stem}.jsonl"


class MonitorEvent(BaseModel):
    """One ordered monitor event from a research run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    sequence: int = Field(ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    team: str = Field(min_length=1)
    event: str = Field(min_length=1)
    level: int = Field(ge=1, le=10)
    summary: str = Field(min_length=1)
    actor: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class RunMonitor:
    """Collect and render run events at an XSPEC-like chatter level."""

    def __init__(
        self,
        *,
        chatter: int = 10,
        run_id: str | None = None,
        jsonl_path: str | Path | None = None,
    ) -> None:
        self.chatter = validate_chatter(chatter)
        self.run_id = run_id or uuid4().hex
        self.jsonl_path = Path(jsonl_path).expanduser() if jsonl_path is not None else None
        self._events: list[MonitorEvent] = []
        self._sequence = 0
        self._lock = Lock()
        if self.jsonl_path is not None:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            self.jsonl_path.write_text("", encoding="utf-8")

    @property
    def events(self) -> tuple[MonitorEvent, ...]:
        """Return a stable snapshot of recorded events."""

        with self._lock:
            return tuple(self._events)

    async def record(
        self,
        team: str,
        event: str,
        *,
        level: int,
        summary: str,
        actor: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> MonitorEvent | None:
        """Record an event if its level is within the configured chatter."""

        validate_chatter(level)
        if level > self.chatter:
            return None
        with self._lock:
            self._sequence += 1
            monitor_event = MonitorEvent(
                run_id=self.run_id,
                sequence=self._sequence,
                team=team,
                event=event,
                level=level,
                summary=summary,
                actor=actor,
                data=_jsonable_dict(data or {}),
            )
            self._events.append(monitor_event)
            if self.jsonl_path is not None:
                with self.jsonl_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(monitor_event.model_dump(mode="json"), sort_keys=True))
                    stream.write("\n")
            return monitor_event

    def events_by_team(self) -> OrderedDict[str, list[MonitorEvent]]:
        """Return events grouped by team, in report order."""

        snapshot = sorted(self.events, key=lambda event: event.sequence)
        grouped: dict[str, list[MonitorEvent]] = {}
        for event in snapshot:
            grouped.setdefault(event.team, []).append(event)

        ordered: OrderedDict[str, list[MonitorEvent]] = OrderedDict()
        for team in TEAM_ORDER:
            if team in grouped:
                ordered[team] = grouped.pop(team)
        for team in sorted(grouped):
            ordered[team] = grouped[team]
        return ordered

    def render_markdown(self, *, grouped: bool = True) -> str:
        """Render the recorded events as a human-readable Markdown report."""

        lines = [
            f"# auto_research monitor {self.run_id}",
            "",
            f"- Chatter: {self.chatter}",
            f"- Events: {len(self.events)}",
            "",
        ]
        if grouped:
            for team, events in self.events_by_team().items():
                lines.extend([f"## {TEAM_LABELS.get(team, team.title())}", ""])
                lines.extend(_render_events(events))
        else:
            lines.extend(_render_events(sorted(self.events, key=lambda event: event.sequence)))
        return "\n".join(lines).rstrip() + "\n"

    def write_jsonl(self, path: str | Path | None = None) -> Path:
        """Write all recorded events as JSONL and return the path."""

        target = Path(path).expanduser() if path is not None else self.jsonl_path
        if target is None:
            target = default_monitor_jsonl_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as stream:
            for event in sorted(self.events, key=lambda item: item.sequence):
                stream.write(json.dumps(event.model_dump(mode="json"), sort_keys=True))
                stream.write("\n")
        return target

    def write_markdown(self, path: str | Path) -> Path:
        """Write a grouped Markdown monitor report and return the path."""

        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render_markdown(grouped=True), encoding="utf-8")
        return target


def _render_events(events: Sequence[MonitorEvent]) -> list[str]:
    lines: list[str] = []
    for event in events:
        title = f"### {event.sequence}. {event.event}"
        if event.actor:
            title = f"{title} - {event.actor}"
        lines.extend(
            [
                title,
                "",
                f"- Time: {event.created_at.isoformat()}",
                f"- Level: {event.level}",
                f"- Summary: {event.summary}",
                "",
            ]
        )
        if event.data:
            lines.extend(
                [
                    "```json",
                    json.dumps(event.data, indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
    return lines


def _jsonable_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable(value) for key, value in data.items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)
