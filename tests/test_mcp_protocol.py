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
                assert names == {"check_environment", "start_job", "get_job", "wait_job", "list_jobs", "get_caption_batch", "get_job_context",
                                 "import_translation_reference", "clear_translation_reference", "submit_caption_batch", "set_caption_batch_boundary", "retranscribe_range", "realign_job", "validate_job", "get_cover_source",
                                 "get_review_issues", "review_issue", "get_review_clip", "set_generated_cover", "export_job", "cancel_job", "resume_job"}
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


def test_stdio_compact_caption_chain_and_persistent_review(tmp_path):
    from app.mcp.captions import make_batches
    from app.mcp.jobs import JobManager
    from app.mcp.review import reconcile_review
    from uuid import uuid4
    manager = JobManager(tmp_path)
    job_id = uuid4().hex
    words = [{'id': f'w{i}', 'text': text, 'start_ms': i*500, 'end_ms': i*500+400,
              'alignment_score': .2 if i == 0 else .9} for i, text in enumerate(['Hello', 'world.'])]
    state = {'job_id': job_id, 'directory': str(tmp_path), 'status': 'awaiting_captions', 'stage': 'awaiting_captions',
             'progress': 100, 'message': '', 'error': None, 'revision': 1, 'created_at': 0, 'artifacts': {},
             'words': words, 'batches': make_batches(words), 'glossary': {}, 'duration_ms': 1000,
             'options': {'source_language': 'en', 'target_language': 'zh-CN'}}
    reconcile_review(state)
    manager.store.save(state)
    params = StdioServerParameters(command=sys.executable,
             args=[str(PROJECT/'scripts/videocaptioner_mcp.py')],
             env=dict(os.environ, VIDEOCAPTIONER_MCP_ROOT=str(tmp_path)))
    async def call(session, name, args):
        result = await session.call_tool(name, args)
        assert not result.isError, result.content
        if result.structuredContent is not None:
            return result.structuredContent
        return json.loads(next(c.text for c in result.content if c.type == "text"))
    async def scenario():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                status = await call(session, 'get_job', {'job_id': job_id})
                wait = await call(session, 'wait_job', {'job_id': job_id, 'after_event_id': status['event_id'], 'timeout': 0})
                assert wait['changed'] is False
                review = await call(session, 'get_review_issues', {'job_id': job_id, 'stage': 'transcript'})
                await call(session, 'review_issue', {'job_id': job_id, 'issue_id': review['issues'][0]['id'],
                           'revision': 1, 'decision': 'retain', 'note': 'Protocol fixture; source comparison only', 'method': 'reference'})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                retained = await call(session, 'get_review_issues', {'job_id': job_id, 'status': 'retained'})
                assert len(retained['issues']) == 1
                context = await call(session, 'get_job_context', {'job_id': job_id})
                batch = await call(session, 'get_caption_batch', {'job_id': job_id, 'compact': True})
                assert batch['context_version'] == context['context_version']
                assert 'metadata' not in batch
                result = await call(session, 'submit_caption_batch', {'job_id': job_id, 'batch_id': batch['batch_id'],
                    'revision': 1, 'return_next_batch': True, 'compact': True,
                    'captions': [{'start_word_id': 'w0', 'end_word_id': 'w1', 'source': 'Hello world.', 'translation': '你好，世界。'}]})
                assert result['next_batch']['done'] and result['revision'] == 2
                assert result['validation']['valid']
    asyncio.run(scenario())
