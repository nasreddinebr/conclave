# Conclave

Conclave is an MCP server that turns several AI agents into one team that works on its own. A **chief** splits the project and hands out tasks. **Workers** do them. A **reviewer** checks the work before the chief signs off. Agents wake each other up when there is something to do, so nobody has to copy prompts between windows.

It works with any MCP client (Claude Code, Claude Desktop, OpenCode, OpenHands, Codex, OpenAI agents). The main use is saving tokens. A strong paid model acts as chief, and cheaper, free or local models do the routine work. The chief only verifies results, and takes over a task only when it gets escalated.

## How it works

```
 architecte (chief)          dev (worker)              leadtech (reviewer)
  Claude Code                 OpenCode                  OpenCode
      |                          |                          |
  conclave process          conclave process          conclave process
      \__________________________|__________________________/
                                 |
                CONCLAVE_HOME/
                  team.json          who is on the team, and the safeguards
                  conclave.db        jobs and messages (SQLite, WAL)
                  conclave.log       structured JSON log
                  workspace/         board.md + one workspace_<name>.md per agent
```

Over stdio, every client starts its own copy of the server. They share state because they all point at the same `CONCLAVE_HOME`.

### Job lifecycle

```
pending --start--> in_progress --submit--> in_review --approve--> approved
pending ------------------------submit--> in_review   (start is optional)
                        ^                      |
                        +------- reject -------+   (3rd rejection: escalated to the chief)
pending / in_progress --fail--> failed
```

| Action | Who | From | To | Note |
| --- | --- | --- | --- | --- |
| `start` | assignee | pending | in_progress | |
| `submit` | assignee | pending, in_progress | in_review | required: the result |
| `fail` | assignee | pending, in_progress | failed | required: why |
| `approve` | reviewer | in_review | approved | required: what was checked |
| `reject` | reviewer | in_review | in_progress, or `escalated` at `max_rejections` | required: what to fix |

Who reviews a job:
- `delegate_task` can name a reviewer explicitly.
- Otherwise, in a two-agent team, the agent that delegated reviews.
- Otherwise, the first agent with the role `reviewer` reviews (falling back to the delegator if that agent is doing the task).

### Who gets woken up

Every event is an inbox message. Some wake the agent, and some are quiet. Quiet ones are delivered with the next wake-up, so they cost no extra model turn.

| Event | Wakes | Quiet notice to |
| --- | --- | --- |
| New task | assignee | |
| Submitted for review | reviewer | |
| Rejected | assignee | |
| Approved | delegator and chief | assignee |
| Escalated | chief | assignee, delegator |
| Failed | delegator | |
| `send_message` | recipient | |
| Project closed | everyone | |
| Workspace file written | | everyone else |
| Task started | | delegator |

### Tools

| Tool | Purpose |
| --- | --- |
| `delegate_task(from_agent_id, target_agent_id, task_description, context, reviewer_agent_id)` | Queue a job. Returns `job_id` and the chosen reviewer immediately. |
| `check_task_status(job_id)` | Full job record: status, result, feedback, rejections. |
| `update_task(agent_id, job_id, action, note)` | Move a job through the lifecycle above. |
| `send_message(from_agent_id, target_agent_id, message_type, content)` | `question`, `review_request`, `contradiction` or `info`. Wakes the recipient. |
| `read_inbox(my_agent_id)` | Unread messages, your open work (to do, to review, delegated) and the team roster. |
| `wait_for_work(agent_id, timeout_seconds)` | Block until something needs you. Returns `events` or `idle`. Waiting costs no tokens. With `CONCLAVE_WAIT_TOOL=instant` it answers at once. |
| `close_project(agent_id, summary)` | Only shown to the chief. Every agent gets a `closed` event with the summary and stops working. Listeners keep waiting until the idle timeout, so a new `delegate_task` reopens the project and wakes them. |
| `write_shared_workspace(agent_id, file_name, content)` | Write your own `workspace_<name>.md`. |
| `read_shared_workspace(file_name)` | Read any workspace file, including `board.md`, or list them. |

`board.md` is regenerated on every job change and lists the team and every job. Open `CONCLAVE_HOME/workspace` as an Obsidian vault to watch the team live.

## Installation

Python 3.11 or newer.

```bash
cd conclave
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python verify.py      # 41 end-to-end checks across real server processes
```

If `.venv/bin/pip` does not exist (`No such file or directory`), the venv was created without pip. On Debian and Ubuntu this happens when `python3.X-venv` is missing. Running `python3 -m venv .venv` again does not add pip to an existing venv. Install the package (`sudo apt install python3.13-venv`), then run `rm -rf .venv` and repeat the steps above.

`requirements.txt` pins `mcp<2`, because MCP SDK 2.x renamed FastMCP to `MCPServer`.

## Configuring the team

On first start, Conclave writes a default `CONCLAVE_HOME/team.json`:

```json
{
  "agents": {
    "architecte": { "role": "chief",    "description": "Plans, delegates, gives final approval." },
    "dev":        { "role": "worker",   "description": "Implements tasks." },
    "leadtech":   { "role": "reviewer", "description": "Reviews submitted work." }
  },
  "max_rejections": 3,
  "idle_timeout_minutes": 30
}
```

Edit it freely:
- **Names** are any lowercase id with letters, digits, `-` or `_`, up to 32 characters.
- **Roles** are `chief`, `worker` or `reviewer`. A team needs exactly one `chief`.
- **Team size** is 2 to 8 agents. Add as many workers or reviewers as you want within that limit.
- `max_rejections` is the number of rejections before a job is escalated to the chief.
- `idle_timeout_minutes` is how long an agent keeps listening with nothing happening.

Restart the agents after editing. An invalid file stops the server with a message that says what is wrong. Set `CONCLAVE_TEAM=/path/team.json` to keep the file somewhere else.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONCLAVE_HOME` | `./conclave_data` | Shared folder. Every agent must use the same absolute path. |
| `CONCLAVE_AGENT_ID` | unset | Binds this process to one agent name. Calls made as anyone else are refused. Set it for every agent. |
| `CONCLAVE_TEAM` | `$CONCLAVE_HOME/team.json` | Team file location. |
| `CONCLAVE_WAIT_TOOL` | `block` | Set to `instant` for clients that have an adapter. `wait_for_work` then answers at once, either with the pending events or with "nothing to do, end your turn", instead of blocking. See [Small models](#small-models-and-idle-agents). |
| `CONCLAVE_LOG_LEVEL` | `INFO` | Log level. |

## Autonomous mode: waking agents up

MCP only works in one direction. A server can't wake up a client. Conclave solves this in two layers.

**1. A universal core.** All the waiting logic is in the server, and it is exposed twice:
- as the MCP tool `wait_for_work`, which works in every client;
- as a command, which adapters call:
  ```bash
  python server.py wait --agent dev [--timeout SECONDS] [--format json|prompt|claude-hook] [--only-if-busy]
  ```
  The exit code is `0` when there are events (printed on stdout) and `3` on idle timeout.

**2. Thin adapters.** An adapter only runs the command and injects its output into the session. The model doesn't run while it waits, so waiting costs no tokens, and it doesn't depend on the model remembering to loop.

| Client | How it wakes up | Files |
| --- | --- | --- |
| OpenCode | Plugin. Puts the agent to sleep (`session.abort`) when `wait_for_work` answers idle, listens while the session is idle, and injects events with `session.promptAsync`. | `adapters/opencode/conclave.ts` |
| Claude Code | `Stop` hook. When the agent is waiting on someone, the hook waits too and returns `decision: block` with the events, so the agent continues. When nothing is in flight, it lets the agent stop immediately. | `adapters/claude-code/settings.json` |
| Any other MCP client | No adapter. Leave `CONCLAVE_WAIT_TOOL` at `block`. The prompt asks the agent to call `wait_for_work` in a loop. This costs a model turn on each timeout and relies on the model to keep looping. | none |

To support a new client, write an adapter that runs `server.py wait --agent <name> --format prompt` when the session goes idle, and sends stdout back into the session when the exit code is 0.

### OpenCode

`opencode.json` in the project root (the same file works for every OpenCode agent):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "conclave": {
      "type": "local",
      "command": ["/abs/path/conclave/.venv/bin/python", "/abs/path/conclave/server.py"],
      "enabled": true,
      "environment": {
        "CONCLAVE_HOME": "/abs/path/conclave_data",
        "CONCLAVE_AGENT_ID": "{env:CONCLAVE_AGENT_ID}",
        "CONCLAVE_WAIT_TOOL": "instant"
      }
    }
  }
}
```

Install the plugin and start one terminal per agent:

```bash
mkdir -p .opencode/plugin
ln -s ../../conclave/adapters/opencode/conclave.ts .opencode/plugin/conclave.ts
CONCLAVE_AGENT_ID=dev opencode
CONCLAVE_AGENT_ID=leadtech opencode      # in a second terminal
```

The plugin reads `CONCLAVE_SERVER`, `CONCLAVE_PYTHON` and `CONCLAVE_HOME`. By default these are `<project>/conclave/server.py`, the venv next to it, and `<project>/conclave_data`. Put `CONCLAVE_AGENT_ID=<name>` on the same line as `opencode`. Without it, the plugin can't wake the session, and it shows a warning in OpenCode saying how to restart. The plugin also shows a notification when it starts listening, when it stops (idle timeout or project closed), and on any error.

### Claude Code

`.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "conclave": {
      "command": "/abs/path/conclave/.venv/bin/python",
      "args": ["/abs/path/conclave/server.py"],
      "env": { "CONCLAVE_HOME": "/abs/path/conclave_data", "CONCLAVE_AGENT_ID": "architecte", "CONCLAVE_WAIT_TOOL": "instant" }
    }
  }
}
```

Then merge `adapters/claude-code/settings.json` into `.claude/settings.local.json`, with your paths and agent name. The hook timeout (1860 s) must be longer than `idle_timeout_minutes`. While the hook waits, the session looks busy. Press Esc to take back control.

### Claude Desktop and others

Use the same `command`, `args` and `env` as above in the client's MCP config, and use the "no adapter" prompt below.

## Agent prompts

Send each agent its prompt once, at the start of its session. With an adapter, that's the only manual step. Replace `<NAME>`.

### Chief

```text
You are <NAME>, the chief of a Conclave team. Call read_inbox(my_agent_id="<NAME>") to see the team roster and roles.

Project: <PROJECT>

1. Plan. Split the project into small tasks. Write the plan in workspace_<NAME>.md.
2. Delegate. One delegate_task per task, with a self-contained description and acceptance criteria (including the test command). Prefer workers for routine tasks to save your own tokens.
3. End your turn after delegating. You will be woken up when a job is approved, escalated or failed, or when someone writes to you.
4. Verify cheaply. When a job is approved, run its tests and read the result. Do not re-read the whole code. If tests fail, either fix it yourself when the fix is quick, or delegate a fix task with the failing output.
5. Escalations: a task rejected max_rejections times comes back to you. Do it yourself, or hand it to a stronger model.
6. When everything is approved and verified, call close_project with a summary.
Ask the human before anything irreversible (deleting data, deploying, pushing).
```

### Worker or reviewer

The same prompt works for every worker and reviewer, with or without an adapter.

```text
You are <NAME>, a <ROLE> in a Conclave team. Call read_inbox(my_agent_id="<NAME>") now to see the team and any open work.

Whenever you receive a Conclave event:
- New task: do it, run its tests, write a short report in workspace_<NAME>.md, then update_task(action="submit", note=<what you did, how to check it, test output>).
- Rejected: read the feedback, fix, submit again.
- Review request: check the work against the task's acceptance criteria and run the tests yourself. Then update_task(action="approve", note=<what you checked: criteria, tests run, their output>) or update_task(action="reject", note=<exactly what to fix>). Be strict: reject if any criterion is not met.
- Question: answer with send_message.
- Project closed: nothing to do.
If you cannot do a task, update_task(action="fail", note=<why>). Disagree with send_message(message_type="contradiction") and evidence. The chief has the final word.
Whenever you have nothing (left) to do, call wait_for_work(agent_id="<NAME>") to go to sleep. You will be woken up when new work arrives. Never invent a job id, and never send status-only messages.
```

With an adapter, the client puts the agent to sleep as soon as `wait_for_work` answers "idle". Without an adapter, `wait_for_work` blocks until there is work. When it answers "idle" after its timeout, the agent simply calls it again.

## Small models and idle agents

Live tests with a small free model (Nemotron 3.5 Lightning) showed four things.

- **Small models call any tool they see, whatever the instructions say.** With a blocking `wait_for_work`, the model called it before ending each turn and wasted 50 seconds.
- **Small models can ignore the hint and loop.** Given the same answer every time, one model called `wait_for_work` 62 times in a minute before stopping.
- **"Project closed" is read as "call close_project".** On receiving the closing message, both agents tried `close_project`, which only the chief may call. Every attempt was refused.
- **A model may never end its turn by itself.** After finishing its task, one reviewer kept calling `wait_for_work` instead of stopping. Only something outside the model can put it to sleep.
- **Removing the tool is worse.** With nothing to do and no "do nothing" option, the model invented work. In 36 seconds it made about 30 calls: `update_task` on a made-up job id, `close_project` (chief only), and "inbox empty" messages to the other agents. Every call was refused, so the safeguards held. But each message woke its recipient, so the chatter spread.

Conclave handles this in five ways. The first two are structural and do not depend on the model obeying:

1. **The adapter enforces sleep.** `wait_for_work` is the "go to sleep" button. As soon as it answers "idle", the OpenCode plugin aborts the session turn (`session.abort`), so the model stops running, and the plugin starts listening. The agent is woken up with the next event. Small models like calling tools, so the instruction is a tool call rather than "stop".
2. **Tools an agent may not use are hidden.** `close_project` is only shown to the chief. Before this, workers kept trying it after the closing message, even when told not to.
3. **`CONCLAVE_WAIT_TOOL=instant` for clients with an adapter.** The tool stays available as a safe "do nothing" move, and it answers at once with "nothing to do, end your turn". After 3 "nothing to do" answers in a row, it blocks before answering, for `timeout_seconds` the first time and then twice as long on each further call (50 s, 100 s, 200 s and so on), up to `idle_timeout_minutes`. A model that ignores the hint and keeps calling drops from about one call per second to a few calls per hour. A new event resets the count. A long block can exceed a client's own tool-call timeout. The model then gets an error instead of an answer, which is still a pause.
4. **`info` messages never wake anyone.** Only `question`, `review_request` and `contradiction` do, along with the server's own `task`, `review_request`, `review_result`, `approved`, `failed`, `escalation` and `closed` notices.
5. **The prompts, server instructions and closing event all say the same thing.** "Whenever you have nothing (left) to do, call wait_for_work to go to sleep. Never invent a job id."

## Security

- **Strict input validation.** Agent names, job ids, actions, message types and file names all follow strict patterns. Text is capped at 200,000 characters. `team.json` is validated at startup.
- **No path traversal.** File names must be bare `*.md` names that resolve directly inside `workspace/`.
- **One writer per file.** An agent may only write its own `workspace_<name>.md`, and `board.md` is written only by the server. Writes are atomic (a temp file, then a rename).
- **Lifecycle enforcement.** Only the assignee can start, submit or fail a job, and only its reviewer can approve or reject it. An assignee can never review its own work. Only the chief can close the project.
- **No string-built SQL from inputs.** Every value is a bound parameter.
- **Known limit.** MCP does not authenticate callers. `CONCLAVE_AGENT_ID` binds each process to one agent, which is enough for agents on one machine. Untrusted agents need real authentication in front of the server.

## Logging and debugging

Every tool call writes one JSON line to stderr and to `CONCLAVE_HOME/conclave.log`. Each line has the timestamp, level, origin, pid, agent, tool and arguments, with long values shortened. Rejected requests return a clear reason to the agent. Unexpected exceptions return the type, message, file and line, plus an incident id that matches the full traceback in the log.

```bash
tail -f conclave_data/conclave.log | jq .
sqlite3 conclave_data/conclave.db "SELECT substr(id,1,8), assignee, reviewer, status, rejections FROM jobs;"
```

## License

MIT. See [LICENSE](LICENSE).
