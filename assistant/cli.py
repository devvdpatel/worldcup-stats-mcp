"""Custom Claude-powered CLI assistant for the World Cup warehouse (an MCP client).

Launches the FastMCP server (mcp_server.server) over stdio, exposes its tools to
Claude via the Anthropic tool-use loop, and runs an interactive chat:

    python -m assistant.cli

Requires ANTHROPIC_API_KEY in .env (or the environment). Optional: WC_MODEL.
"""
from __future__ import annotations

import asyncio
import os
import sys

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import config

MODEL = os.environ.get("WC_MODEL", "claude-sonnet-4-6")

SYSTEM = (
    "You are a FIFA World Cup statistics assistant. Answer using ONLY the connected tools, "
    "which query a DuckDB warehouse of player and team stats for the 2010, 2014, 2018, and "
    "2022 World Cups. Prefer the specific tools (top_scorers, player_stats, compare_players, "
    "team_summary); use run_sql for anything they don't cover — call list_tables first to see "
    "the schema. Player-name matching is case-insensitive substring and accent-SENSITIVE "
    "(try 'Mbapp' rather than 'Mbappe'). Be concise, cite the actual numbers, and say so when "
    "data is unavailable (this dataset has no expected-goals/xG, and shot counts are missing "
    "for 2010 and 2014)."
)

SERVER = StdioServerParameters(
    command=sys.executable,
    args=["-m", "mcp_server.server"],
    cwd=str(config.PROJECT_ROOT),
    env=os.environ.copy(),
)


def _to_anthropic_tools(mcp_tools) -> list[dict]:
    tools = [
        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
        for t in mcp_tools
    ]
    if tools:  # cache the (static) tool definitions across turns
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


async def chat() -> None:
    load_dotenv(config.PROJECT_ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.\n"
              "Run:  cp .env.example .env  then paste your key into .env")
        return

    client = AsyncAnthropic()
    system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]

    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = _to_anthropic_tools((await session.list_tools()).tools)
            print(f"\n⚽  World Cup assistant ready — {len(tools)} tools, model {MODEL}.")
            print("    Ask about 2010/2014/2018/2022. Type 'quit' to exit.\n")

            messages: list[dict] = []
            while True:
                try:
                    user = input("you › ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nbye 👋")
                    return
                if user.lower() in {"quit", "exit", "q"}:
                    print("bye 👋")
                    return
                if not user:
                    continue
                messages.append({"role": "user", "content": user})

                try:
                    while True:  # tool-use loop
                        resp = await client.messages.create(
                            model=MODEL, max_tokens=2048,
                            system=system, tools=tools, messages=messages,
                        )
                        messages.append({"role": "assistant", "content": resp.content})

                        if resp.stop_reason != "tool_use":
                            text = "".join(b.text for b in resp.content if b.type == "text")
                            print(f"\nassistant › {text}\n")
                            break

                        results = []
                        for block in resp.content:
                            if block.type == "tool_use":
                                print(f"    ↳ {block.name}({block.input})")
                                res = await session.call_tool(block.name, block.input)
                                out = "\n".join(c.text for c in res.content if c.type == "text")
                                results.append({
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": out or "(no rows)",
                                })
                        messages.append({"role": "user", "content": results})
                except Exception as exc:  # keep the chat alive on API/tool errors
                    print(f"\n[error: {exc}]\n")
                    # drop the dangling user turn so the conversation stays valid
                    if messages and messages[-1]["role"] == "user":
                        messages.pop()


def main() -> None:
    asyncio.run(chat())


if __name__ == "__main__":
    main()
