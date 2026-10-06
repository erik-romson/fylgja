// Fylgja inbox writer for OpenCode (optional; PLAN-final-plan.md WP3.2).
//
// Copy this file by hand to ~/.config/opencode/plugins/fylgja.ts. It forwards `session.status`,
// `permission.asked` and `todo.updated` to Fylgja as `fylgja.hook/1` envelopes
// (docs/api/inbox-envelope.md), written into hook-inbox/opencode/ under the Fylgja data directory.
// The daemon treats each envelope as a wake-up and reads the real state from opencode.db, so the
// plugin is only a speed-up: without it the state is at most one poll interval old.
//
// It follows the rules of `fylgja hook`: the envelope is written to an O_EXCL temporary file
// (mode 0600), fsynced and renamed to `<received_at_ns>-<id>.json`; the inbox is created with mode
// 0700 and nothing is written when it is a symlink, owned by someone else, or has another mode, or
// while `.backlog-stamp` says `full`. It never throws into OpenCode, never prints, never touches
// the network, and sends only ids, event names and the few fields below: no prompt, no reply text,
// no tool input. The only environment variables it copies are the terminal identifiers.
//
// Only the plugin function is exported: OpenCode calls every export of a plugin file.

import { randomUUID } from "node:crypto"
import {
  closeSync,
  constants,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeSync,
} from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

const SCHEMA = "fylgja.hook/1"
const SOURCE = "opencode"
const PLUGIN_VERSION = "0.1.0"
const EVENTS = ["session.status", "permission.asked", "todo.updated"]
const ENV_ALLOWLIST = [
  "CMUX_WORKSPACE_ID",
  "CMUX_SURFACE_ID",
  "CMUX_TAB_ID",
  "CMUX_PANEL_ID",
  "ITERM_SESSION_ID",
  "TERM_SESSION_ID",
  "TERM_PROGRAM",
]
const ENV_VALUE_MAX = 256
const FIELD_MAX = 512
const EVENT_ARG_MAX = 64
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/

type Json = string | number | boolean | null | Json[] | { [key: string]: Json }
type Properties = Record<string, unknown>
type OpenCodeEvent = { type?: string; properties?: Properties }
type PluginInput = { directory?: string }

let lastNs = 0n

// Nanoseconds since the epoch as a BigInt: strictly increasing within this process, so envelopes
// of one session sort in the order they were produced.
function receivedAtNs(): bigint {
  const now = BigInt(Date.now()) * 1_000_000n
  lastNs = now > lastNs ? now : lastNs + 1n
  return lastNs
}

function dataDir(): string {
  const override = process.env.FYLGJA_DATA_DIR
  if (override) return override.startsWith("~") ? join(homedir(), override.slice(1)) : override
  return join(homedir(), ".local", "share", "fylgja")
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value.slice(0, FIELD_MAX) : undefined
}

// Compact JSON with every non-ASCII character escaped, like the Python writer.
function ascii(value: Json): string {
  return JSON.stringify(value).replace(
    /[\u007f-￿]/g,
    (char) => "\\u" + char.charCodeAt(0).toString(16).padStart(4, "0"),
  )
}

function capturedEnv(): Record<string, Json> {
  const env: Record<string, Json> = {}
  for (const name of ENV_ALLOWLIST) {
    const value = process.env[name]
    if (value) env[name] = value.slice(0, ENV_VALUE_MAX)
  }
  return env
}

// The payload keeps the field names Fylgja's hook minimizer reads: session_id, hook_event_name, cwd,
// tool_name and reason. `reason` carries the status type of `session.status` (busy, idle, retry).
function payloadOf(event: OpenCodeEvent, directory: string | undefined): Record<string, Json> | undefined {
  const type = event.type
  if (type === undefined || !EVENTS.includes(type)) return undefined
  const properties = event.properties ?? {}
  const sessionId = text(properties.sessionID) ?? text(properties.sessionId)
  if (sessionId === undefined || !SESSION_ID.test(sessionId)) return undefined
  const payload: Record<string, Json> = { session_id: sessionId, hook_event_name: type }
  const cwd = text(directory)
  if (cwd !== undefined) payload.cwd = cwd
  if (type === "session.status") {
    const status = properties.status
    const kind = typeof status === "string" ? status : text((status as Properties | undefined)?.type)
    if (kind !== undefined) payload.reason = kind.slice(0, FIELD_MAX)
  } else if (type === "permission.asked") {
    const tool = text(properties.permission)
    if (tool !== undefined) payload.tool_name = tool
  }
  return payload
}

// The envelope as one line of JSON, keys in the order of docs/api/inbox-envelope.md. The
// nanosecond stamp is an integer literal; JSON.stringify cannot write a BigInt.
function encode(id: string, ns: bigint, eventArg: string, payload: Record<string, Json>): string {
  return (
    "{" +
    [
      `"schema":${ascii(SCHEMA)}`,
      `"id":${ascii(id)}`,
      `"received_at_ns":${ns.toString()}`,
      `"source":${ascii(SOURCE)}`,
      `"event_arg":${ascii(eventArg.slice(0, EVENT_ARG_MAX))}`,
      `"ppid":${ascii(process.ppid)}`,
      `"env":${ascii(capturedEnv())}`,
      `"payload_text":${ascii(ascii(payload))}`,
      `"truncated":false`,
      `"fylgja_version":${ascii(PLUGIN_VERSION)}`,
    ].join(",") +
    "}\n"
  )
}

// Create the inbox (and missing parents) with mode 0700, then check it as the hook does.
function privateDir(path: string): boolean {
  try {
    mkdirSync(path, { recursive: true, mode: 0o700 })
    const info = lstatSync(path)
    return (
      info.isDirectory() &&
      !info.isSymbolicLink() &&
      info.uid === process.getuid?.() &&
      (info.mode & 0o777) === 0o700
    )
  } catch {
    return false
  }
}

function backlogFull(inbox: string): boolean {
  try {
    return readFileSync(join(inbox, ".backlog-stamp"), "utf8").startsWith("full")
  } catch {
    return false
  }
}

function deliver(inbox: string, id: string, ns: bigint, line: string): void {
  if (!privateDir(inbox) || backlogFull(inbox)) return
  const tmp = join(inbox, `.tmp-${id}`)
  const flags = constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW
  let fd: number
  try {
    fd = openSync(tmp, flags, 0o600)
  } catch {
    return
  }
  try {
    try {
      writeSync(fd, line)
      fsyncSync(fd)
    } finally {
      closeSync(fd)
    }
    renameSync(tmp, join(inbox, `${ns.toString()}-${id}.json`))
  } catch {
    try {
      unlinkSync(tmp)
    } catch {
      // nothing left to clean up
    }
  }
}

function forward(event: OpenCodeEvent, directory: string | undefined): void {
  const payload = payloadOf(event, directory)
  if (payload === undefined) return
  const id = randomUUID()
  const ns = receivedAtNs()
  deliver(join(dataDir(), "hook-inbox", SOURCE), id, ns, encode(id, ns, String(event.type), payload))
}

export const FylgjaPlugin = async ({ directory }: PluginInput) => ({
  event: async ({ event }: { event: OpenCodeEvent }) => {
    try {
      forward(event, directory)
    } catch {
      // an event that cannot be forwarded is lost; OpenCode never sees an error
    }
  },
})
