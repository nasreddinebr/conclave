"""Conclave: a multi-agent collaboration hub served over MCP."""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import re
import shlex
import sqlite3
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, ParamSpec, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

HOME = Path(os.environ.get("CONCLAVE_HOME", Path.cwd() / "conclave_data")).resolve()
WORKSPACE = HOME / "workspace"
DB_PATH = HOME / "conclave.db"
TEAM_PATH = Path(os.environ.get("CONCLAVE_TEAM", HOME / "team.json")).resolve()
BOUND_AGENT = os.environ.get("CONCLAVE_AGENT_ID") or None
INSTANT_WAIT = os.environ.get("CONCLAVE_WAIT_TOOL", "block").lower() == "instant"
IDLE_ANSWERS_BEFORE_BLOCKING = 3
CLOSED_HINT = "The project is closed. Call wait_for_work to go to sleep."
MAX_TEXT = 200_000
NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"

AgentId = Annotated[str, Field(pattern=NAME_PATTERN, description="Agent name from team.json")]
JobId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]
FileName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}\.md$", description="Bare Markdown file name")]
MessageType = Literal["question", "review_request", "contradiction", "info"]
Role = Literal["chief", "worker", "reviewer"]
Action = Literal["start", "submit", "fail", "approve", "reject"]


class AgentSpec(BaseModel):
    role: Role
    description: str = ""


class Team(BaseModel):
    agents: dict[str, AgentSpec]
    max_rejections: int = Field(default=3, ge=1, le=20)
    idle_timeout_minutes: int = Field(default=30, ge=1, le=24 * 60)

    @field_validator("agents")
    @classmethod
    def _check_agents(cls, agents: dict[str, AgentSpec]) -> dict[str, AgentSpec]:
        bad = [name for name in agents if not re.fullmatch(NAME_PATTERN, name)]
        if bad:
            raise ValueError(f"agent names must be lowercase letters, digits, - or _ (max 32): {bad}")
        if not 2 <= len(agents) <= 8:
            raise ValueError(f"a team needs 2 to 8 agents, got {len(agents)}")
        return agents

    @model_validator(mode="after")
    def _one_chief(self) -> Team:
        chiefs = [name for name, spec in self.agents.items() if spec.role == "chief"]
        if len(chiefs) != 1:
            raise ValueError(f"exactly one agent must have role 'chief', got {chiefs}")
        return self

    @property
    def chief(self) -> str:
        return next(name for name, spec in self.agents.items() if spec.role == "chief")

    def default_reviewer(self, delegator: str, assignee: str) -> str:
        if len(self.agents) > 2:
            for name, spec in self.agents.items():
                if spec.role == "reviewer" and name != assignee:
                    return name
        return delegator


DEFAULT_TEAM = Team(
    agents={
        "architecte": AgentSpec(role="chief", description="Plans, delegates, gives final approval."),
        "dev": AgentSpec(role="worker", description="Implements tasks."),
        "leadtech": AgentSpec(role="reviewer", description="Reviews submitted work."),
    }
)


@dataclass(frozen=True)
class Rule:
    actor: Literal["assignee", "reviewer"]
    sources: frozenset[str]
    target: str
    needs_note: bool


RULES: dict[str, Rule] = {
    "start": Rule("assignee", frozenset({"pending"}), "in_progress", needs_note=False),
    "submit": Rule("assignee", frozenset({"pending", "in_progress"}), "in_review", needs_note=True),
    "fail": Rule("assignee", frozenset({"pending", "in_progress"}), "failed", needs_note=True),
    "approve": Rule("reviewer", frozenset({"in_review"}), "approved", needs_note=True),
    "reject": Rule("reviewer", frozenset({"in_review"}), "in_progress", needs_note=True),
}
NOTE_HINTS = {
    "submit": "the result: what you did, how to check it, test output",
    "fail": "why it failed",
    "approve": "what you checked: criteria, tests run and their output",
    "reject": "exactly what to fix",
}
OPEN_STATES = ("pending", "in_progress", "in_review")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, delegator TEXT NOT NULL, assignee TEXT NOT NULL, reviewer TEXT NOT NULL,
    description TEXT NOT NULL, context TEXT NOT NULL, status TEXT NOT NULL, result TEXT, feedback TEXT,
    rejections INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, recipient TEXT NOT NULL, type TEXT NOT NULL,
    content TEXT NOT NULL, wakes INTEGER NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_inbox ON messages (recipient, read);
CREATE INDEX IF NOT EXISTS jobs_assignee ON jobs (assignee, status);
"""


class ConclaveError(Exception):
    """A rejected request whose message is safe and useful to show the calling agent."""


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
            "origin": f"{record.module}:{record.funcName}:{record.lineno}",
            "pid": record.process,
            "agent": BOUND_AGENT,
        }
        entry.update(getattr(record, "ctx", {}))
        if record.exc_info:
            entry["trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def _build_logger() -> logging.Logger:
    HOME.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("conclave")
    logger.setLevel(os.environ.get("CONCLAVE_LOG_LEVEL", "INFO").upper())
    for handler in (logging.StreamHandler(sys.stderr), logging.FileHandler(HOME / "conclave.log", encoding="utf-8")):
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def _load_team() -> Team:
    if not TEAM_PATH.exists():
        TEAM_PATH.parent.mkdir(parents=True, exist_ok=True)
        TEAM_PATH.write_text(DEFAULT_TEAM.model_dump_json(indent=2) + "\n", encoding="utf-8")
    try:
        team = Team.model_validate_json(TEAM_PATH.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise SystemExit(f"Invalid team file {TEAM_PATH}:\n{exc}") from exc
    if BOUND_AGENT is not None and BOUND_AGENT not in team.agents:
        raise SystemExit(f"CONCLAVE_AGENT_ID={BOUND_AGENT!r} is not in {TEAM_PATH} ({list(team.agents)})")
    return team


log = _build_logger()
TEAM = _load_team()
mcp = FastMCP(
    "conclave",
    instructions=(
        f"Conclave team: {', '.join(f'{n} ({s.role})' for n, s in TEAM.agents.items())}. "
        + (
            f"You are {BOUND_AGENT}. "
            if BOUND_AGENT
            else "WARNING: CONCLAVE_AGENT_ID is not set, so this connection is not bound to an agent and will not "
            "be woken up automatically. Tell the user to restart the client as CONCLAVE_AGENT_ID=<name> <client>. "
        )
        + "Jobs go pending -> in_progress -> in_review -> approved. Use update_task to start, submit, fail, "
        "approve or reject. Whenever you have nothing (left) to do, call wait_for_work to go to sleep: you are woken "
        "up when new work arrives. Never invent job ids or send status-only messages."
    ),
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _init_storage() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _caller(agent_id: str) -> str:
    _member(agent_id)
    if BOUND_AGENT is not None and agent_id != BOUND_AGENT:
        raise ConclaveError(f"This connection is bound to {BOUND_AGENT!r} and cannot act as {agent_id!r}.")
    return agent_id


def _member(agent_id: str) -> str:
    if agent_id not in TEAM.agents:
        raise ConclaveError(f"Unknown agent {agent_id!r}. Team is {list(TEAM.agents)}.")
    return agent_id


def _workspace_path(file_name: str) -> Path:
    path = (WORKSPACE / file_name).resolve()
    if path.parent != WORKSPACE:
        raise ConclaveError(f"{file_name!r} resolves outside the shared workspace.")
    return path


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _post(conn: sqlite3.Connection, sender: str, recipient: str, kind: str, content: str, wakes: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO messages (sender, recipient, type, content, wakes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (sender, recipient, kind, content, int(wakes), _now()),
    )
    return int(cur.lastrowid or 0)


def _render_board(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
    closed = conn.execute("SELECT value FROM meta WHERE key = 'closed'").fetchone()
    lines = [
        "# Conclave board",
        "",
        "Generated by the server on every job change. Read only.",
        "",
        f"Project: **{'closed: ' + closed['value'] if closed else 'open'}**",
        "",
        "## Team",
        "",
        *(f"- [[workspace_{n}]] `{n}` ({s.role}) {s.description}".rstrip() for n, s in TEAM.agents.items()),
        "",
        "## Jobs",
        "",
        "| Job | From | To | Reviewer | Status | Rejections | Updated | Task |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        task = r["description"].splitlines()[0][:80].replace("|", "\\|")
        lines.append(
            f"| `{r['id'][:8]}` | {r['delegator']} | {r['assignee']} | {r['reviewer']} | {r['status']} "
            f"| {r['rejections']} | {r['updated_at']} | {task} |"
        )
    _atomic_write(WORKSPACE / "board.md", "\n".join(lines) + "\n")


P = ParamSpec("P")
R = TypeVar("R")


def _ctx(name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": name,
        "args": {k: (v if len(str(v)) <= 200 else f"<{len(str(v))} chars>") for k, v in kwargs.items()},
    }


def _fail(name: str, ctx: dict[str, Any], exc: Exception) -> ToolError:
    if isinstance(exc, ConclaveError):
        log.warning("rejected: %s", exc, extra={"ctx": ctx})
        return ToolError(f"{name} rejected: {exc}")
    incident = uuid.uuid4().hex[:12]
    log.exception("unexpected failure", extra={"ctx": {**ctx, "incident": incident}})
    origin = traceback.extract_tb(exc.__traceback__)[-1]
    return ToolError(
        f"{name} failed ({type(exc).__name__}: {exc}) at {Path(origin.filename).name}:{origin.lineno}. "
        f"Incident {incident}, full trace in {HOME / 'conclave.log'}."
    )


def logged(fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap a tool so every call is logged and no failure is silent."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        ctx = _ctx(fn.__name__, kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            raise _fail(fn.__name__, ctx, exc) from exc
        log.info("ok", extra={"ctx": ctx})
        return result

    return wrapper


def tool(fn: Callable[P, R]) -> Callable[P, R]:
    """Register a logged MCP tool."""
    return mcp.tool()(logged(fn))


@tool
def delegate_task(
    from_agent_id: AgentId,
    target_agent_id: AgentId,
    task_description: Text,
    context: str = "",
    reviewer_agent_id: AgentId | None = None,
) -> dict[str, str]:
    """Queue a task for another agent and return its job_id immediately. The target is woken up.

    The reviewer defaults to the delegator in a two-agent team, otherwise to the team's reviewer.
    """
    sender, target = _caller(from_agent_id), _member(target_agent_id)
    if sender == target:
        raise ConclaveError("An agent cannot delegate a task to itself.")
    reviewer = _member(reviewer_agent_id) if reviewer_agent_id else TEAM.default_reviewer(sender, target)
    if reviewer == target:
        raise ConclaveError("The reviewer cannot be the agent doing the task.")
    if len(context) > MAX_TEXT:
        raise ConclaveError(f"context exceeds {MAX_TEXT} characters.")
    job_id, now = uuid.uuid4().hex, _now()
    with _db() as conn:
        conn.execute("DELETE FROM meta WHERE key = 'closed'")
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0, ?, ?)",
            (job_id, sender, target, reviewer, task_description, context, now, now),
        )
        _post(
            conn,
            sender,
            target,
            "task",
            f"New job {job_id} from {sender}. Reviewer: {reviewer}.\n\n{task_description}"
            + (f"\n\nContext:\n{context}" if context else ""),
        )
        _render_board(conn)
    return {"job_id": job_id, "status": "pending", "reviewer": reviewer}


@tool
def check_task_status(job_id: JobId) -> dict[str, Any]:
    """Return a job: status, assignee, reviewer, result, reviewer feedback and rejection count."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ConclaveError(f"No job {job_id}.")
    return dict(row)


@tool
def update_task(agent_id: AgentId, job_id: JobId, action: Action, note: str = "") -> dict[str, Any]:
    """Move a job through its lifecycle.

    Assignee: start (pending -> in_progress), submit (pending or in_progress -> in_review, note = result),
    fail (note = why).
    Reviewer: approve (in_review -> approved, note = what you checked), reject (-> in_progress, note = what to fix).
    Too many rejections escalate the job to the chief.
    """
    me, rule = _caller(agent_id), RULES[action]
    if len(note) > MAX_TEXT:
        raise ConclaveError(f"note exceeds {MAX_TEXT} characters.")
    with _db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise ConclaveError(f"No job {job_id}.")
        if job[rule.actor] != me:
            raise ConclaveError(f"Only the {rule.actor} ({job[rule.actor]}) can {action} job {job_id}.")
        if job["status"] not in rule.sources:
            raise ConclaveError(f"Cannot {action} a job that is {job['status']}.")
        if rule.needs_note and not note.strip():
            raise ConclaveError(f"{action} needs a note ({NOTE_HINTS[action]}).")
        status, rejections = rule.target, job["rejections"]
        if action == "reject":
            rejections += 1
            if rejections >= TEAM.max_rejections:
                status = "escalated"
        fields = {"status": status, "rejections": rejections, "updated_at": _now()}
        if action in ("submit", "fail"):
            fields["result"] = note
        if action in ("approve", "reject"):
            fields["feedback"] = note or None
        conn.execute(
            f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
            (*fields.values(), job_id),
        )
        _notify(conn, me, dict(job), action, status, note)
        _render_board(conn)
    return {"job_id": job_id, "status": status, "rejections": rejections}


def _notify(conn: sqlite3.Connection, me: str, job: dict[str, Any], action: str, status: str, note: str) -> None:
    jid, assignee, reviewer, delegator = job["id"], job["assignee"], job["reviewer"], job["delegator"]
    if action == "start":
        _post(conn, me, delegator, "info", f"{me} started job {jid}.", wakes=False)
    elif action == "submit":
        _post(
            conn,
            me,
            reviewer,
            "review_request",
            f"Job {jid} from {assignee} is ready for your review.\n\nTask:\n{job['description']}\n\nResult:\n{note}\n\n"
            "Check it, then call update_task with action approve, or reject with a note saying what to fix.",
        )
    elif action == "fail":
        _post(conn, me, delegator, "failed", f"Job {jid} failed: {note}")
    elif action == "approve":
        for who in dict.fromkeys((assignee, delegator, TEAM.chief)):
            if who != me:
                _post(
                    conn,
                    me,
                    who,
                    "approved",
                    f"Job {jid} approved by {me}." + (f" {note}" if note else ""),
                    wakes=who != assignee,
                )
    elif status == "escalated":
        for who in dict.fromkeys((TEAM.chief, assignee, delegator)):
            if who != me:
                _post(
                    conn,
                    me,
                    who,
                    "escalation",
                    f"Job {jid} was rejected {job['rejections'] + 1} times and is escalated to {TEAM.chief}. "
                    f"Last feedback from {me}: {note}",
                    wakes=who == TEAM.chief,
                )
    else:
        _post(
            conn,
            me,
            assignee,
            "review_result",
            f"Job {jid} was rejected by {me} (rejection {job['rejections'] + 1}/{TEAM.max_rejections}). "
            f"Fix this, then submit again:\n\n{note}",
        )


@tool
def send_message(
    from_agent_id: AgentId, target_agent_id: AgentId, message_type: MessageType, content: Text
) -> dict[str, int]:
    """Send a question, review_request or contradiction (wakes the recipient) or an info note (does not wake)."""
    sender, target = _caller(from_agent_id), _member(target_agent_id)
    with _db() as conn:
        return {"message_id": _post(conn, sender, target, message_type, content, wakes=message_type != "info")}


def _take_unread(conn: sqlite3.Connection, me: str) -> list[dict[str, Any]]:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, sender, type, content, created_at FROM messages WHERE recipient = ? AND read = 0 ORDER BY id",
            (me,),
        )
    ]
    conn.execute("UPDATE messages SET read = 1 WHERE recipient = ? AND read = 0", (me,))
    return rows


def _open_work(conn: sqlite3.Connection, me: str) -> dict[str, list[dict[str, Any]]]:
    def q(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, (me,))]

    return {
        "jobs_to_do": q(
            "SELECT id, delegator, reviewer, status, rejections, feedback, description FROM jobs "
            "WHERE assignee = ? AND status IN ('pending', 'in_progress') ORDER BY created_at"
        ),
        "jobs_to_review": q(
            "SELECT id, assignee, result, description FROM jobs "
            "WHERE reviewer = ? AND status = 'in_review' ORDER BY created_at"
        ),
        "jobs_delegated_open": q(
            "SELECT id, assignee, reviewer, status FROM jobs "
            "WHERE delegator = ? AND status IN ('pending', 'in_progress', 'in_review')"
        ),
    }


@tool
def read_inbox(my_agent_id: AgentId) -> dict[str, Any]:
    """Return your unread messages (then mark them read), your open work, and the team roster."""
    me = _caller(my_agent_id)
    with _db() as conn:
        messages = _take_unread(conn, me)
        work = _open_work(conn, me)
    return {
        "agent_id": me,
        "role": TEAM.agents[me].role,
        "team": {n: s.role for n, s in TEAM.agents.items()},
        "messages": messages,
        **work,
    }


WaitStatus = Literal["events", "idle"]


def _poll(me: str) -> tuple[WaitStatus, list[dict[str, Any]]] | None:
    with _db() as conn:
        if conn.execute(
            "SELECT 1 FROM messages WHERE recipient = ? AND read = 0 AND wakes = 1 AND type != 'info' LIMIT 1", (me,)
        ).fetchone():
            return "events", _take_unread(conn, me)
    return None


def _busy(me: str) -> bool:
    """True when the agent is waiting on someone: open jobs it delegated, or wake-up messages already queued."""
    with _db() as conn:
        work = _open_work(conn, me)
        unread = conn.execute(
            "SELECT 1 FROM messages WHERE recipient = ? AND read = 0 AND wakes = 1 AND type != 'info'", (me,)
        ).fetchone()
    return bool(unread or any(work.values()))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait(me: str, timeout: float, parent: int | None = None) -> dict[str, Any]:
    """Poll until events or timeout. With `parent`, stop without consuming anything once that process is gone."""
    deadline = time.monotonic() + timeout
    while True:
        if parent is not None and not _alive(parent):
            log.warning("parent process exited, listener stops", extra={"ctx": {"agent": me, "parent": parent}})
            return {"status": "idle", "events": []}
        found = await asyncio.to_thread(_poll, me)
        if found is not None:
            return {"status": found[0], "events": found[1]}
        if time.monotonic() >= deadline:
            return {"status": "idle", "events": []}
        await asyncio.sleep(1)


_idle_streak: dict[str, int] = {}


def _wait_seconds(streak: int, timeout_seconds: int) -> float:
    """Instant mode answers at once, then brakes a looping model: each further idle call blocks twice as long."""
    if not INSTANT_WAIT:
        return timeout_seconds
    if streak < IDLE_ANSWERS_BEFORE_BLOCKING:
        return 0
    doubled: int = timeout_seconds * 2 ** (streak - IDLE_ANSWERS_BEFORE_BLOCKING)
    return float(min(doubled, TEAM.idle_timeout_minutes * 60))


async def wait_for_work(
    agent_id: AgentId, timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 50
) -> dict[str, Any]:
    """Go to sleep: call this whenever you have nothing (left) to do. It returns the events that need you, if any.

    Status "events": handle them, then call wait_for_work again.
    Status "idle": nothing to do. Your client puts you to sleep and wakes you up when new work arrives.
    Never invent work or job ids.
    """
    ctx = _ctx("wait_for_work", {"agent_id": agent_id, "timeout_seconds": timeout_seconds})
    try:
        me = _caller(agent_id)
        streak = _idle_streak.get(me, 0)
        result = await _wait(me, _wait_seconds(streak, timeout_seconds))
        _idle_streak[me] = streak + 1 if result["status"] == "idle" else 0
        if result["status"] == "idle" and INSTANT_WAIT:
            result["hint"] = "Nothing to do. You are going to sleep and will be woken up when new work arrives."
        if any(e["type"] == "closed" for e in result["events"]):
            result["hint"] = CLOSED_HINT
    except Exception as exc:
        raise _fail("wait_for_work", ctx, exc) from exc
    log.info("ok", extra={"ctx": {**ctx, "status": result["status"], "events": len(result["events"])}})
    return result


mcp.tool()(wait_for_work)


@logged
def close_project(agent_id: AgentId, summary: Text) -> dict[str, Any]:
    """Chief only. Close the project: every agent gets the summary and stops working. A new delegate_task reopens it."""
    me = _caller(agent_id)
    if me != TEAM.chief:
        raise ConclaveError(f"Only the chief ({TEAM.chief}) can close the project.")
    with _db() as conn:
        open_jobs = [
            r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status IN ('pending', 'in_progress', 'in_review')")
        ]
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('closed', ?)", (_now(),))
        for other in TEAM.agents:
            if other != me:
                _post(conn, me, other, "closed", f"Project closed by {me}.\n\n{summary}\n\n{CLOSED_HINT}")
        _render_board(conn)
    return {"closed": True, "open_jobs_left": open_jobs}


if BOUND_AGENT in (None, TEAM.chief):
    mcp.tool()(close_project)


@tool
def write_shared_workspace(agent_id: AgentId, file_name: FileName, content: Text) -> dict[str, Any]:
    """Overwrite your own workspace_<agent_id>.md. Others get a quiet notice that does not wake them."""
    me = _caller(agent_id)
    if file_name != f"workspace_{me}.md":
        raise ConclaveError(f"{me!r} may only write workspace_{me}.md.")
    path = _workspace_path(file_name)
    verb = "updated" if path.exists() else "created"
    _atomic_write(path, content)
    others = [a for a in TEAM.agents if a != me]
    with _db() as conn:
        for other in others:
            _post(conn, me, other, "info", f"{me} {verb} {file_name}.", wakes=False)
    return {"file_name": file_name, "action": verb, "bytes": path.stat().st_size, "notified": others}


@tool
def read_shared_workspace(file_name: FileName | None = None) -> dict[str, Any]:
    """Read one Markdown file (board.md lists every job), or list all files when file_name is omitted."""
    if file_name is None:
        return {"files": sorted(p.name for p in WORKSPACE.glob("*.md"))}
    path = _workspace_path(file_name)
    if not path.is_file():
        raise ConclaveError(f"{file_name} does not exist yet.")
    return {"file_name": file_name, "content": path.read_text(encoding="utf-8")}


def format_prompt(me: str, events: list[dict[str, Any]], sleep: str = "call wait_for_work to go to sleep") -> str:
    """Turn wake-up events into the prompt an adapter injects into the agent's session."""
    blocks = [f"### {e['type']} from {e['sender']} ({e['created_at']})\n{e['content']}" for e in events]
    if events and events[-1]["type"] == "closed":
        closing = CLOSED_HINT if sleep.startswith("call") else "The project is closed. End your turn."
    else:
        closing = (
            "Handle them with the conclave tools (update_task, send_message, write_shared_workspace), "
            f"then {sleep} until something new arrives."
        )
    return f"[Conclave] {len(events)} new event(s) for {me}.\n\n" + "\n\n".join(blocks) + "\n\n" + closing


EXIT = {"events": 0, "idle": 3}


def _cli_wait(args: argparse.Namespace) -> int:
    me = _member(args.agent)
    if args.only_if_busy and not _busy(me):
        return 0 if args.format == "claude-hook" else EXIT["idle"]
    timeout = args.timeout if args.timeout is not None else TEAM.idle_timeout_minutes * 60
    result = asyncio.run(_wait(me, timeout, parent=args.parent or os.getppid()))
    log.info("cli wait", extra={"ctx": {"agent": me, "status": result["status"], "events": len(result["events"])}})
    if args.format == "claude-hook":
        if result["status"] == "events":
            reason = format_prompt(me, result["events"], sleep="end your turn: the hook keeps you asleep")
            print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    if result["status"] == "events":
        print(format_prompt(me, result["events"]) if args.format == "prompt" else json.dumps(result))
    return EXIT[result["status"]]


SERVER_PATH = Path(__file__).resolve()
ADAPTERS = SERVER_PATH.parent / "adapters"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConclaveError(f"{path} is not valid JSON ({exc}). Fix it or move it away, then run init again.") from exc
    if not isinstance(data, dict):
        raise ConclaveError(f"{path} must contain a JSON object.")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _ensure_member(agent: str, role: str | None) -> str:
    """Return the agent's role, adding the agent to team.json when --role is given and it is missing."""
    team = Team.model_validate_json(TEAM_PATH.read_text(encoding="utf-8"))
    if agent in team.agents:
        if role and team.agents[agent].role != role:
            raise ConclaveError(f"{agent!r} is already a {team.agents[agent].role} in {TEAM_PATH}. Edit it there.")
        return team.agents[agent].role
    if role is None:
        raise ConclaveError(f"{agent!r} is not in {TEAM_PATH} ({list(team.agents)}). Add it there or pass --role.")
    team.agents[agent] = AgentSpec.model_validate({"role": role})
    team = Team.model_validate(team.model_dump())
    _atomic_write(TEAM_PATH, team.model_dump_json(indent=2) + "\n")
    return role


def _install_opencode(project: Path, agent: str, role: str) -> list[str]:
    config = project / "opencode.json"
    if (project / "opencode.jsonc").exists():
        raise ConclaveError(f"{project / 'opencode.jsonc'} exists. Move its content to opencode.json, then run init.")
    data = _read_json(config)
    data.setdefault("$schema", "https://opencode.ai/config.json")
    data.setdefault("mcp", {})["conclave"] = {
        "type": "local",
        "command": [sys.executable, str(SERVER_PATH)],
        "enabled": True,
        "environment": {
            "CONCLAVE_HOME": str(HOME),
            "CONCLAVE_AGENT_ID": "{env:CONCLAVE_AGENT_ID}",
            "CONCLAVE_WAIT_TOOL": "instant",
        },
    }
    _write_json(config, data)
    link = project / ".opencode" / "plugin" / "conclave.ts"
    link.parent.mkdir(parents=True, exist_ok=True)
    target = ADAPTERS / "opencode" / "conclave.ts"
    if link.is_symlink() or link.exists():
        if link.resolve() != target:
            raise ConclaveError(f"{link} already exists and is not Conclave's plugin. Move it away, then run init.")
    else:
        link.symlink_to(target)
    return [str(config), f"{link} -> {target}"]


def _install_claude_code(project: Path, agent: str, role: str) -> list[str]:
    mcp_json = project / ".mcp.json"
    data = _read_json(mcp_json)
    data.setdefault("mcpServers", {})["conclave"] = {
        "command": sys.executable,
        "args": [str(SERVER_PATH)],
        "env": {"CONCLAVE_HOME": str(HOME), "CONCLAVE_AGENT_ID": agent, "CONCLAVE_WAIT_TOOL": "instant"},
    }
    _write_json(mcp_json, data)
    busy = " --only-if-busy" if role == "chief" else ""
    command = (
        f"CONCLAVE_HOME={shlex.quote(str(HOME))} {shlex.quote(sys.executable)} {shlex.quote(str(SERVER_PATH))} "
        f"wait --agent {agent}{busy} --format claude-hook"
    )
    settings_path = project / ".claude" / "settings.local.json"
    settings = _read_json(settings_path)
    stop = settings.setdefault("hooks", {}).setdefault("Stop", [])
    stop[:] = [
        g
        for g in stop
        if not any("server.py" in h.get("command", "") and " wait " in h.get("command", "") for h in g.get("hooks", []))
    ]
    stop.append({"hooks": [{"type": "command", "command": command, "timeout": TEAM.idle_timeout_minutes * 60 + 60}]})
    _write_json(settings_path, settings)
    return [str(mcp_json), str(settings_path)]


INSTALLERS: dict[str, Callable[[Path, str, str], list[str]]] = {
    "opencode": _install_opencode,
    "claude-code": _install_claude_code,
}
LAUNCH = {
    "opencode": "CONCLAVE_AGENT_ID={agent} opencode",
    "claude-code": "claude   (approve the 'conclave' MCP server when asked)",
}


def _cli_init(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    if not project.is_dir():
        raise ConclaveError(f"{project} is not a folder.")
    agent = _member_name(args.agent)
    role = _ensure_member(agent, args.role)
    written = INSTALLERS[args.client](project, agent, role)
    print(f"Conclave set up for {agent} ({role}) on {args.client} in {project}")
    print(f"Shared data: {HOME}  (every agent of this team must use it)")
    for item in written:
        print(f"  wrote {item}")
    print(f"\nStart the agent from {project}:\n  {LAUNCH[args.client].format(agent=agent)}")
    print(
        f"Then give it the {'chief' if role == 'chief' else 'worker or reviewer'} prompt "
        f"from {SERVER_PATH.parent / 'README.md'}."
    )
    return 0


def _member_name(agent: str) -> str:
    if not re.fullmatch(NAME_PATTERN, agent):
        raise ConclaveError(f"{agent!r} is not a valid agent name (lowercase letters, digits, - or _).")
    return agent


def main() -> None:
    parser = argparse.ArgumentParser(prog="conclave", description="Conclave MCP server, wait helper and installer.")
    sub = parser.add_subparsers(dest="command")
    wait = sub.add_parser("wait", help="Block until the agent has events. Exit 0 events, 3 idle timeout.")
    wait.add_argument("--agent", required=True)
    wait.add_argument("--timeout", type=float, help="Seconds. Defaults to idle_timeout_minutes from team.json.")
    wait.add_argument("--format", choices=["json", "prompt", "claude-hook"], default="json")
    wait.add_argument("--only-if-busy", action="store_true", help="Return at once unless the agent awaits something.")
    wait.add_argument("--parent", type=int, help="Stop without consuming events once this process is gone.")
    init = sub.add_parser(
        "init", help="Set up a client for one agent in a project. Uses CONCLAVE_HOME for shared data."
    )
    init.add_argument("--client", required=True, choices=sorted(INSTALLERS))
    init.add_argument("--agent", required=True)
    init.add_argument("--role", choices=["chief", "worker", "reviewer"], help="Add the agent to team.json if missing.")
    init.add_argument("--project", default=".", help="Project folder to configure. Defaults to the current folder.")
    args = parser.parse_args()
    _init_storage()
    commands = {"wait": _cli_wait, "init": _cli_init}
    if args.command in commands:
        try:
            raise SystemExit(commands[args.command](args))
        except ConclaveError as exc:
            raise SystemExit(f"conclave {args.command}: {exc}") from exc
    log.info("conclave started", extra={"ctx": {"home": str(HOME), "team": str(TEAM_PATH), "bound_agent": BOUND_AGENT}})
    mcp.run()


if __name__ == "__main__":
    main()
