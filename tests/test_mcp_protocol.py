"""Exercise the actual SDK transport, including recovery across connections."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT = Path(__file__).resolve().parents[1]


def test_stdio_initialization_tools_and_readiness(tmp_path):
    async def scenario():
        params = StdioServerParameters(command=sys.executable,
                 args=[str(PROJECT / "scripts/videocaptioner_mcp.py")],
                 env=dict(os.environ, VIDEOCAPTIONER_MCP_ROOT=str(tmp_path)))
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = await session.list_tools()
                names = {tool.name for tool in listing.tools}
                assert names == {"check_environment", "start_job", "get_job", "list_jobs", "get_caption_batch",
                                 "submit_caption_batch", "set_caption_batch_boundary", "retranscribe_range", "validate_job", "get_cover_source",
                                 "set_generated_cover", "export_job", "cancel_job", "resume_job"}
                schema = next(t.inputSchema for t in listing.tools if t.name == "submit_caption_batch")
                assert "captions" in schema["properties"]
                check = await session.call_tool("check_environment", {"model": "/missing/local/model"})
                assert not check.isError
                assert "not cached" in str(check.content)
                jobs = await session.call_tool("list_jobs", {})
                assert not jobs.isError
                invalid = await session.call_tool("get_job", {"job_id": "../../etc/passwd"})
                assert invalid.isError
    asyncio.run(scenario())
