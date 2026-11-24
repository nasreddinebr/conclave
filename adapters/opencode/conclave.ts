import type { Plugin } from "@opencode-ai/plugin"

// Wakes this OpenCode session whenever Conclave has events for CONCLAVE_AGENT_ID.
// All waiting logic lives in `server.py wait`; this file only glues it to the session.
export const Conclave: Plugin = async ({ client, directory }) => {
  const agent = process.env.CONCLAVE_AGENT_ID
  type Variant = "info" | "success" | "warning" | "error"
  const log = (level: "debug" | "info" | "warn" | "error", message: string) =>
    client.app.log({ body: { service: "conclave", level, message } }).catch(() => {})
  const notify = (variant: Variant, message: string, duration = 8000) => {
    const level = variant === "success" ? "info" : variant === "warning" ? "warn" : variant
    client.app.log({ body: { service: "conclave", level, message } }).catch(() => {})
    client.tui.showToast({ body: { title: "Conclave", message, variant, duration } }).catch(() => {})
  }

  if (!agent) {
    let warned = false
    return {
      event: async () => {
        if (warned) return
        warned = true
        notify(
          "warning",
          "CONCLAVE_AGENT_ID is not set: this session will not be woken up. Quit and restart with CONCLAVE_AGENT_ID=<name> opencode",
          30000,
        )
      },
    }
  }

  // `server.py init` writes these into opencode.json; environment variables override them.
  const configured = await readConclaveConfig(directory)
  const python = process.env.CONCLAVE_PYTHON ?? configured.python
  const server = process.env.CONCLAVE_SERVER ?? configured.server
  const home = process.env.CONCLAVE_HOME ?? configured.home
  if (!python || !server || !home) {
    notify("error", `No "conclave" entry in ${directory}/opencode.json. Run: python <conclave>/server.py init --client opencode --agent ${agent}`, 30000)
    return {}
  }
  const env = { ...process.env, CONCLAVE_HOME: home }

  let waiting = false
  let announced = false

  const listen = async (sessionID: string) => {
    waiting = true
    if (!announced) {
      announced = true
      notify("info", `Listening as ${agent}`)
    }
    try {
      const proc = Bun.spawn([python, server, "wait", "--agent", agent, "--format", "prompt", "--parent", String(process.pid)], {
        env,
        stdout: "pipe",
        stderr: "pipe",
      })
      const [text, errors, code] = await Promise.all([
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
        proc.exited,
      ])
      if (code === 0) {
        await client.session.promptAsync({ path: { id: sessionID }, body: { parts: [{ type: "text", text }] } })
      } else if (code === 3) {
        announced = false
        notify("info", `${agent} stopped listening: idle timeout. Send any message to resume.`)
      } else {
        notify("error", `conclave wait exited ${code}: ${errors.trim().split("\n").pop()}`, 30000)
      }
    } catch (err) {
      notify("error", `conclave wait could not start: ${err}`, 30000)
    } finally {
      waiting = false
    }
  }

  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle" || waiting) return
      const sessionID = event.properties.sessionID
      const session = await client.session.get({ path: { id: sessionID } })
      if (session.data?.parentID) return
      void listen(sessionID)
    },
    // wait_for_work answering "idle" means the agent has nothing to do: end its turn for real (sleep), then listen.
    "tool.execute.after": async (input, output) => {
      if (!input.tool.endsWith("wait_for_work")) return
      const idle = isIdle(output)
      log("info", `after ${input.tool}: idle=${idle} output=${describe(output)}`)
      if (!idle) return
      try {
        const res = await client.session.abort({ path: { id: input.sessionID } })
        log("info", `put ${agent} to sleep: abort ${res.error ? "failed " + JSON.stringify(res.error) : "ok"}`)
      } catch (err) {
        notify("error", `could not put ${agent} to sleep: ${err}`, 30000)
      }
      if (!waiting) void listen(input.sessionID)
    },
  }
}

function describe(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value)
  return `${typeof value}:${(text ?? "").slice(0, 160).replace(/\s+/g, " ")}`
}

async function readConclaveConfig(directory: string): Promise<{ python?: string; server?: string; home?: string }> {
  try {
    const entry = (await Bun.file(`${directory}/opencode.json`).json())?.mcp?.conclave
    return { python: entry?.command?.[0], server: entry?.command?.[1], home: entry?.environment?.CONCLAVE_HOME }
  } catch {
    return {}
  }
}

// The tool result reaches plugins in different shapes depending on the OpenCode version: accept any of them.
function isIdle(output: unknown): boolean {
  const text = typeof output === "string" ? output : JSON.stringify(output) ?? ""
  return /\\?"status\\?"\s*:\s*\\?"idle/.test(text)
}
