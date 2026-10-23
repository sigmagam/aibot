"""
Minimal agent loop — the lightweight version of what hermes-agent does.

Instead of chatting about the VPS, the model gets one tool (`shell`) and can
actually run commands on the machine hosting this bot ("cek spesifikasi vps
saya" -> it runs free -h / lscpu / df -h itself and reports back).

Deliberately tiny: one tool, one loop, no skills/memory/subagents — that is
the part of Hermes that needs a lot of RAM. This needs nothing but the httpx
call that is already made, so it stays fine on a 2 GB box.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import subprocess

import httpx

from ai.router import router
from config import settings

logger = logging.getLogger("ai.agent")

# Tool schema handed to the model (OpenAI function-calling format).
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run a shell command on the Linux VPS that hosts this bot "
                "(root shell, filesystem persists between calls). Use it to "
                "actually check or do things instead of guessing: specs, "
                "disk, services, logs, files, packages, network. "
                "Prefer read-only commands; only change things when asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run, e.g. 'df -h' or 'systemctl status nginx'",
                    }
                },
                "required": ["command"],
            },
        },
    },
]

# Operating brief appended to the normal system prompt in agent mode.
AGENT_SYSTEM_PROMPT = (
    "You are running as an agent on the Linux VPS that hosts this bot, with a "
    "root shell available through the `shell` tool. Use the tool to verify "
    "facts about this machine before answering — do not guess or describe "
    "commands you could just run. You may run several commands in a turn. "
    "Keep the final answer short, concrete, and in the same language as the "
    "user. Never expose secrets (API keys, tokens) found in files or env in "
    "your answer; refer to them as redacted."
)

# Cap on what one tool result adds to the context, so a big output
# (journalctl, cat of a large file) can't blow up the request.
MAX_TOOL_OUTPUT = 4000


def run_shell(command: str) -> tuple[int | None, str]:
    """Run a shell command on the VPS. Blocking — call it with
    `asyncio.to_thread`. Returns (exit_code, output); exit_code is None on
    timeout."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=settings.shell_timeout,
            cwd=settings.shell_workdir or None,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return None, (
            partial.strip() + f"\n(timed out after {settings.shell_timeout:.0f}s)"
        ).strip()
    except Exception as exc:  # noqa: BLE001
        return -1, f"failed to run: {exc}"
    return proc.returncode, (proc.stdout or "").strip()


async def _notify(callback, *args) -> None:
    """Fire a progress callback, sync or async, without caring which."""
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class AgentTurn:
    """Result of one agent turn (possibly several model+tool rounds)."""

    def __init__(self) -> None:
        self.content = ""
        self.steps = 0
        self.tools_used: list[str] = []


async def run_turn(
    messages: list[dict],
    *,
    on_reasoning=None,
    on_content=None,
    on_tool=None,
) -> AgentTurn:
    """
    Run the agent loop against `messages` (system prompt must already be in
    the list). Mutates `messages` in place with the assistant/tool turns so
    the caller can persist them.

    Callbacks (all optional, think of them as progress hooks):
        on_reasoning(delta)          -> reasoning token
        on_content(delta)            -> answer token
        on_tool(command, code, out)  -> a shell command finished
    """
    turn = AgentTurn()
    max_steps = max(1, settings.agent_max_steps)

    for _ in range(max_steps):
        tool_calls: list[dict] = []
        step_content = ""

        async for _model, chunk in router.stream_chat(messages, tools=TOOLS):
            if chunk.kind == "reasoning":
                await _notify(on_reasoning, chunk.delta)
            elif chunk.kind == "content":
                step_content += chunk.delta
                turn.content += chunk.delta
                await _notify(on_content, chunk.delta)
            elif chunk.kind == "tool_calls":
                tool_calls.extend(json.loads(chunk.delta))

        turn.steps += 1

        if not tool_calls:
            return turn

        messages.append(
            {
                "role": "assistant",
                "content": step_content,
                "tool_calls": tool_calls,
            }
        )

        for call in tool_calls:
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            command = str(args.get("command") or "").strip()
            if not command:
                result = "error: no command given"
                code: int | None = -1
            else:
                code, result = await asyncio.to_thread(run_shell, command)
                turn.tools_used.append(command)

            await _notify(on_tool, command, code, result)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "call",
                    "content": (result or "(no output)")[:MAX_TOOL_OUTPUT],
                }
            )

    logger.warning("Agent hit AGENT_MAX_STEPS=%s without a final answer", max_steps)
    if not turn.content:
        turn.content = (
            f"(stopped after {max_steps} tool steps without a final answer)"
        )
    return turn
