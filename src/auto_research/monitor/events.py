"""Async-safe run monitor events and rendering."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from threading import Lock
from typing import Any, TextIO
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

    from auto_research.env import runtime_data_dir  # local import avoids circularity at module load

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return runtime_data_dir() / "monitors" / f"{timestamp}_{stem}.jsonl"


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
        stream: TextIO | None = None,
    ) -> None:
        self.chatter = validate_chatter(chatter)
        self.run_id = run_id or uuid4().hex
        self.jsonl_path = Path(jsonl_path).expanduser() if jsonl_path is not None else None
        self.stream = stream
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
        """Record an event.

        Always streams to JSONL at full fidelity (level 1–10).
        Only stores in memory and returns the event when level <= chatter,
        so chatter controls terminal/notebook output without affecting the log.
        """
        validate_chatter(level)
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
            if self.jsonl_path is not None:
                with self.jsonl_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(monitor_event.model_dump(mode="json"), sort_keys=True))
                    stream.write("\n")
            if level > self.chatter:
                return None
            self._events.append(monitor_event)
            if self.stream is not None:
                _write_stream_event(self.stream, monitor_event)
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

    def write_markdown(self, path: str | Path, *, grouped: bool = True) -> Path:
        """Write a Markdown monitor report and return the path."""

        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render_markdown(grouped=grouped), encoding="utf-8")
        return target

    @classmethod
    def from_jsonl(cls, path: str | Path, *, chatter: int = 10) -> "RunMonitor":
        """Reconstruct a RunMonitor from a previously written JSONL file."""

        source = Path(path).expanduser()
        events = [
            MonitorEvent.model_validate_json(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        run_id = events[0].run_id if events else uuid4().hex
        monitor = cls.__new__(cls)
        monitor.chatter = validate_chatter(chatter)
        monitor.run_id = run_id
        monitor.jsonl_path = None
        monitor.stream = None
        monitor._events = events
        monitor._sequence = len(events)
        monitor._lock = Lock()
        return monitor


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


def write_stream_event(stream: TextIO, event: MonitorEvent) -> None:
    """Write one concise, live-friendly monitor line (public: the detached-run follower in
    ``graph/detached.py`` reuses this so a tailed JSONL renders identically to a live run)."""
    _write_stream_event(stream, event)


def _write_stream_event(stream: TextIO, event: MonitorEvent) -> None:
    """Write one concise, live-friendly monitor line."""

    try:
        stream.write(_format_stream_event(event))
        stream.flush()
    except (OSError, ValueError):
        pass


def _format_stream_event(event: MonitorEvent) -> str:
    timestamp = event.created_at.strftime("%H:%M:%S")
    subject = _stream_subject(event)
    actor = f" {event.actor}" if event.actor else ""
    detail = _stream_detail(event.data)
    suffix = f" | {detail}" if detail else ""
    return (
        f"[{timestamp}] #{event.sequence:04d} {subject} L{event.level} "
        f"{event.event}{actor}: {event.summary}{suffix}\n"
    )


def _stream_subject(event: MonitorEvent) -> str:
    model = event.data.get("model")
    if isinstance(model, str) and model:
        return f"{event.team}({model})"
    return event.team


def _stream_detail(data: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if not data:
        return ""

    run_id = data.get("run_id")
    if isinstance(run_id, str):
        parts.append(f"run_id={run_id[:12]}")

    search_request = data.get("search_request")
    if isinstance(search_request, Mapping):
        query = search_request.get("query")
        sources = search_request.get("sources")
        if query:
            parts.append(f"query={_short(query)}")
        if sources:
            parts.append(f"sources={','.join(str(s) for s in sources)}")

    for key in ("results", "paths"):
        value = data.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            parts.append(f"{key}={len(value)}")

    calls = data.get("calls")
    if isinstance(calls, Sequence) and not isinstance(calls, str | bytes | bytearray):
        names: list[str] = []
        for call in calls[:5]:
            if isinstance(call, Mapping):
                name = str(call.get("name", "tool"))
                if call.get("is_error"):
                    name += "!"
                names.append(name)
        if len(calls) > 5:
            names.append(f"+{len(calls) - 5}")
        if names:
            parts.append(f"tools={','.join(names)}")

    tool = data.get("tool")
    if isinstance(tool, str) and tool:
        parts.append(f"tool={tool}")
        parts.append(f"status={'error' if data.get('is_error') else 'ok'}")

    chars = data.get("chars")
    if isinstance(chars, int):
        parts.append(f"chars={chars}")

    figure_count = data.get("figure_count")
    if isinstance(figure_count, int):
        parts.append(f"figures={figure_count}")

    verdict = data.get("verdict")
    if isinstance(verdict, Mapping):
        flags = [
            f"accepted={verdict.get('accepted')}",
            f"more_lit={verdict.get('needs_more_literature')}",
            f"analysis_rework={verdict.get('needs_analysis_rework')}",
        ]
        parts.append(" ".join(flags))

    path = data.get("path")
    if path:
        parts.append(f"path={_short(path, limit=120)}")

    error = data.get("error")
    if error:
        parts.append(f"error={_short(error, limit=160)}")

    return " ".join(parts)


def _short(value: Any, *, limit: int = 80) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "~"


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
