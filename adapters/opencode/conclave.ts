import type { Plugin } from "@opencode-ai/plugin"

// Wakes this OpenCode session whenever Conclave has events for CONCLAVE_AGENT_ID.
// All waiting logic lives in `server.py wait`; this file only glues it to the session.
export const Conclave: Plugin = async ({ client, directory }) => {
  const agent = process.env.CONCLAVE_AGENT_ID
  type Variant = "info" | "success" | "warning" | "error"
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

  const server = process.env.CONCLAVE_SERVER ?? `${directory}/conclave/server.py`
  const python = process.env.CONCLAVE_PYTHON ?? server.replace(/server\.py$/, ".venv/bin/python")
  const env = { ...process.env, CONCLAVE_HOME: process.env.CONCLAVE_HOME ?? `${directory}/conclave_data` }

  let waiting = false
  let announced = false

  const listen = async (sessionID: string) => {
    waiting = true
    if (!announced) {
      announced = true
      notify("info", `Listening as ${agent}`)
    }
    try {
      const proc = Bun.spawn([python, server, "wait", "--agent", agent, "--format", "prompt"], {
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
      if (!input.tool.endsWith("wait_for_work") || !isIdle(output.output)) return
      await client.session.abort({ path: { id: input.sessionID } }).catch(() => {})
      if (!waiting) void listen(input.sessionID)
    },
  }
}

function isIdle(output: string): boolean {
  try {
    return JSON.parse(output).status === "idle"
  } catch {
    return /"status"\s*:\s*"idle"/.test(output)
  }
}
