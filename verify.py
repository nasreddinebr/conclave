"""End-to-end check: one stdio server process per agent, all sharing one CONCLAVE_HOME."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name("server.py")


async def connect(stack: AsyncExitStack, home: str, agent: str, **extra: str) -> ClientSession:
    params = StdioServerParameters(
        command=sys.executable, args=[str(SERVER)], env={"CONCLAVE_HOME": home, "CONCLAVE_AGENT_ID": agent, **extra}
    )
    read, write = await stack.enter_async_context(stdio_client(params, errlog=open(os.devnull, "w")))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def call(session: ClientSession, name: str, **args: Any) -> tuple[bool, Any]:
    res = await session.call_tool(name, args)
    text = res.content[0].text if res.content else ""
    return (not res.isError, text if res.isError else json.loads(text))


def cli(home: str, *args: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SERVER), *args], capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "CONCLAVE_HOME": home},
    )


def check(label: str, cond: bool, detail: Any = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  -> {detail}" if not cond else ""))
    if not cond:
        raise SystemExit(1)


def types(result: dict[str, Any]) -> list[str]:
    return [e["type"] for e in result["events"]]


async def three_agent_flow(home: str) -> None:
    async with AsyncExitStack() as stack:
        chief, dev, lead = [await connect(stack, home, a) for a in ("architecte", "dev", "leadtech")]
        team = json.loads((Path(home) / "team.json").read_text())
        check("default team.json written", list(team["agents"]) == ["architecte", "dev", "leadtech"], team)

        tools = {t.name for t in (await chief.list_tools()).tools}
        check("chief sees all nine tools", len(tools) == 9, tools)

        ok, job = await call(chief, "delegate_task", from_agent_id="architecte", target_agent_id="dev",
                             task_description="Write the parser")
        check("delegate returns at once, reviewer defaults to leadtech",
              ok and job["status"] == "pending" and job["reviewer"] == "leadtech", job)
        jid = job["job_id"]

        ok, w = await call(dev, "wait_for_work", agent_id="dev", timeout_seconds=5)
        check("dev is woken with the task", ok and w["status"] == "events" and types(w) == ["task"], w)
        check("task text is inside the event", "Write the parser" in w["events"][0]["content"], w)

        await call(lead, "write_shared_workspace", agent_id="leadtech", file_name="workspace_leadtech.md", content="# lt")
        ok, w = await call(dev, "wait_for_work", agent_id="dev", timeout_seconds=2)
        check("workspace notice does not wake anyone", ok and w["status"] == "idle", w)

        await call(dev, "update_task", agent_id="dev", job_id=jid, action="start")
        ok, err = await call(dev, "update_task", agent_id="dev", job_id=jid, action="submit")
        check("submit needs a result", not ok and "needs a note" in err, err)
        ok, r = await call(dev, "update_task", agent_id="dev", job_id=jid, action="submit", note="parser.py done")
        check("submit moves to in_review", ok and r["status"] == "in_review", r)

        ok, w = await call(lead, "wait_for_work", agent_id="leadtech", timeout_seconds=5)
        check("reviewer woken with review_request", ok and "review_request" in types(w), w)
        ok, err = await call(dev, "update_task", agent_id="dev", job_id=jid, action="approve")
        check("assignee cannot approve own work", not ok and "Only the reviewer" in err, err)

        ok, r = await call(lead, "update_task", agent_id="leadtech", job_id=jid, action="reject", note="add tests")
        check("reject sends job back to in_progress", ok and r["status"] == "in_progress" and r["rejections"] == 1, r)
        ok, w = await call(dev, "wait_for_work", agent_id="dev", timeout_seconds=5)
        check("dev woken with the feedback", "review_result" in types(w) and any("add tests" in e["content"] for e in w["events"]), w)

        await call(dev, "update_task", agent_id="dev", job_id=jid, action="submit", note="tests added")
        await call(lead, "wait_for_work", agent_id="leadtech", timeout_seconds=5)
        ok, err = await call(lead, "update_task", agent_id="leadtech", job_id=jid, action="approve")
        check("approve needs a note saying what was checked", not ok and "what you checked" in err, err)
        ok, r = await call(lead, "update_task", agent_id="leadtech", job_id=jid, action="approve", note="ran tests: OK")
        check("approve", ok and r["status"] == "approved", r)
        ok, w = await call(chief, "wait_for_work", agent_id="architecte", timeout_seconds=5)
        check("chief woken by approval", ok and "approved" in types(w) and any("approved" in e["content"] for e in w["events"]), w)
        ok, w = await call(dev, "wait_for_work", agent_id="dev", timeout_seconds=2)
        check("approval does not wake the assignee", ok and w["status"] == "idle", w)

        ok, job2 = await call(chief, "delegate_task", from_agent_id="architecte", target_agent_id="dev",
                              task_description="Hard task")
        j2 = job2["job_id"]
        for n in range(3):
            ok, r = await call(dev, "update_task", agent_id="dev", job_id=j2, action="submit", note=f"try {n}")
            if n == 0:
                check("submit straight from pending skips start", ok and r["status"] == "in_review", r)
            ok, r = await call(lead, "update_task", agent_id="leadtech", job_id=j2, action="reject", note=f"no {n}")
        check("third rejection escalates", ok and r["status"] == "escalated" and r["rejections"] == 3, r)
        ok, w = await call(chief, "wait_for_work", agent_id="architecte", timeout_seconds=5)
        check("chief woken by escalation", ok and "escalation" in types(w), w)

        ok, r = await call(lead, "read_shared_workspace", file_name="board.md")
        check("board.md lists both jobs", ok and jid[:8] in r["content"] and "escalated" in r["content"], r)
        ok, err = await call(lead, "write_shared_workspace", agent_id="leadtech", file_name="board.md", content="x")
        check("nobody can write board.md", not ok and "may only write" in err, err)
        ok, err = await call(dev, "write_shared_workspace", agent_id="dev", file_name="workspace_leadtech.md", content="x")
        check("agent cannot write another agent's file", not ok, err)
        for bad in ("../conclave.db", "../team.md", "/etc/passwd.md", "a/b.md"):
            ok, err = await call(dev, "read_shared_workspace", file_name=bad)
            check(f"path traversal blocked: {bad}", not ok, err)
        ok, err = await call(dev, "read_inbox", my_agent_id="leadtech")
        check("bound connection cannot impersonate", not ok and "bound to" in err, err)

        await call(lead, "read_inbox", my_agent_id="leadtech")
        busy = cli(home, "wait", "--agent", "leadtech", "--only-if-busy")
        check("cli --only-if-busy returns idle at once when nothing is pending", busy.returncode == 3, busy)
        hook = cli(home, "wait", "--agent", "leadtech", "--only-if-busy", "--format", "claude-hook")
        check("claude hook allows stop when not busy", hook.returncode == 0 and hook.stdout == "", hook)

        await call(dev, "read_inbox", my_agent_id="dev")
        await call(lead, "send_message", from_agent_id="leadtech", target_agent_id="dev", message_type="info", content="fyi")
        ok, w = await call(dev, "wait_for_work", agent_id="dev", timeout_seconds=2)
        check("an info message does not wake anyone", ok and w["status"] == "idle", w)
        proc = subprocess.Popen(
            [sys.executable, str(SERVER), "wait", "--agent", "dev", "--format", "prompt", "--timeout", "20"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={**os.environ, "CONCLAVE_HOME": home},
        )
        await asyncio.sleep(1.5)
        await call(lead, "send_message", from_agent_id="leadtech", target_agent_id="dev",
                   message_type="question", content="Which parser library?")
        out, _ = proc.communicate(timeout=20)
        check("cli wait wakes on a message and prints a prompt",
              proc.returncode == 0 and out.startswith("[Conclave] ") and "Which parser" in out, out)

        await call(dev, "read_inbox", my_agent_id="dev")
        shell = subprocess.Popen(
            ["sh", "-c", f'"{sys.executable}" "{SERVER}" wait --agent dev --timeout 30 & echo $!; sleep 2'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={**os.environ, "CONCLAVE_HOME": home},
        )
        orphan = int(shell.stdout.readline())
        shell.wait()
        await asyncio.sleep(2.5)
        await call(lead, "send_message", from_agent_id="leadtech", target_agent_id="dev",
                   message_type="question", content="Still there?")
        await asyncio.sleep(1.5)
        ok, inbox = await call(dev, "read_inbox", my_agent_id="dev")
        gone = subprocess.run(["kill", "-0", str(orphan)], capture_output=True).returncode != 0
        check("an orphaned listener exits and never consumes events",
              gone and any("Still there?" in m["content"] for m in inbox["messages"]), (gone, inbox))
        await call(dev, "send_message", from_agent_id="dev", target_agent_id="architecte",
                   message_type="question", content="Done?")
        hook = cli(home, "wait", "--agent", "architecte", "--only-if-busy", "--format", "claude-hook")
        decision = json.loads(hook.stdout or "{}")
        check("claude hook blocks stop with the event and says to end the turn",
              decision.get("decision") == "block" and "Done?" in decision.get("reason", "")
              and "end your turn" in decision["reason"] and "wait_for_work" not in decision["reason"], hook)

        dev_tools = {t.name for t in (await dev.list_tools()).tools}
        check("close_project is only shown to the chief", "close_project" not in dev_tools and "close_project" in tools, dev_tools)
        ok, r = await call(chief, "close_project", agent_id="architecte", summary="All approved.")
        check("chief closes the project", ok and r["closed"], r)
        ok, w = await call(lead, "wait_for_work", agent_id="leadtech", timeout_seconds=5)
        check("agents receive the closing message with a do-nothing hint",
              ok and "closed" in types(w) and "go to sleep" in w.get("hint", ""), w)
        ok, w = await call(lead, "wait_for_work", agent_id="leadtech", timeout_seconds=2)
        check("after closing, waiting just stays idle", ok and w["status"] == "idle", w)
        await call(dev, "read_inbox", my_agent_id="dev")
        proc = subprocess.Popen(
            [sys.executable, str(SERVER), "wait", "--agent", "dev", "--format", "prompt", "--timeout", "20"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={**os.environ, "CONCLAVE_HOME": home},
        )
        await asyncio.sleep(1.5)
        ok, job3 = await call(chief, "delegate_task", from_agent_id="architecte", target_agent_id="dev",
                              task_description="Follow-up after closing")
        out, _ = proc.communicate(timeout=20)
        check("a new task after closing reopens the project and wakes the listener",
              ok and proc.returncode == 0 and "Follow-up after closing" in out, out)
        ok, r = await call(lead, "read_shared_workspace", file_name="board.md")
        check("board shows the project open again", ok and "Project: **open**" in r["content"], r)


async def two_agent_flow(home: str) -> None:
    (Path(home) / "team.json").write_text(json.dumps(
        {"agents": {"boss": {"role": "chief"}, "coder": {"role": "worker"}}, "max_rejections": 2}
    ))
    async with AsyncExitStack() as stack:
        boss = await connect(stack, home, "boss", CONCLAVE_WAIT_TOOL="instant")
        clock = asyncio.get_running_loop().time
        for n in range(3):
            started = clock()
            ok, w = await call(boss, "wait_for_work", agent_id="boss", timeout_seconds=3)
            fast = clock() - started < 2
            check(f"instant wait_for_work answer {n + 1} is immediate with a hint",
                  ok and fast and w["status"] == "idle" and "going to sleep" in w["hint"], w)
        started = clock()
        ok, w = await call(boss, "wait_for_work", agent_id="boss", timeout_seconds=3)
        check("4th idle answer in a row blocks for the timeout (loop brake)", ok and clock() - started >= 2.5, clock() - started)
        started = clock()
        ok, w = await call(boss, "wait_for_work", agent_id="boss", timeout_seconds=3)
        check("5th idle answer blocks twice as long (progressive brake)", ok and clock() - started >= 5.5, clock() - started)
        ok, job = await call(boss, "delegate_task", from_agent_id="boss", target_agent_id="coder", task_description="t")
        check("two agents: custom names work, reviewer is the delegator", ok and job["reviewer"] == "boss", job)


def bad_team(home: str) -> None:
    for team, needle in (
        ({"agents": {"a": {"role": "chief"}, "b": {"role": "chief"}}}, "exactly one agent"),
        ({"agents": {"Archi Tecte": {"role": "chief"}, "b": {"role": "worker"}}}, "lowercase"),
        ({"agents": {"a": {"role": "chief"}}}, "2 to 8 agents"),
    ):
        (Path(home) / "team.json").write_text(json.dumps(team))
        r = cli(home, "wait", "--agent", "a", "--timeout", "1")
        check(f"invalid team rejected: {needle}", r.returncode == 1 and needle in r.stderr, r.stderr)


async def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="conclave-verify-"))
    homes = [str(root / n) for n in ("three", "two", "bad")]
    for h in homes:
        Path(h).mkdir()
    await three_agent_flow(homes[0])
    await two_agent_flow(homes[1])
    bad_team(homes[2])
    log = (Path(homes[0]) / "conclave.log").read_text().splitlines()
    check("structured JSON log written", len(log) > 40 and all(json.loads(line)["level"] for line in log), len(log))
    print(f"\nAll checks passed. Data folders in {root}")


asyncio.run(main())
