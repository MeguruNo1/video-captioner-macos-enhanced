"""stdio MCP server: bounded tool responses and persistent background jobs."""
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from .jobs import JobManager, check_environment as inspect_environment

mcp = FastMCP("videocaptioner", instructions="Local media workflow. Call check_environment before starting to inspect CPU/GPU runtime support. Auto selects MLX Metal on Apple Silicon, otherwise usable WhisperX CUDA or CPU; honor explicit backend/device choices and report the selected device and fallback reason. Missing CUDA runtime is not proof the machine has no compatible GPU. Do not install drivers or change the backend silently. Transcription is local; the client must proofread, segment and translate caption batches. Do not use a translation API or computer control.")
manager = JobManager()


class Caption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_word_id: str
    end_word_id: str
    source: str = Field(min_length=1)
    translation: str = Field(min_length=1)


@mcp.tool()
def check_environment(model: str | None = None, backend: str = "auto", device: str = "auto", compute_type: str = "auto") -> dict:
    """Inspect CPU/GPU runtime, supported compute types, selected local ASR, model cache and dependencies without downloading. backend=auto|mlx|whisperx; device=auto|cuda|cpu (MLX uses Metal). Explicit unsupported choices fail. Probe success does not guarantee model fits GPU memory."""
    return inspect_environment(model, backend, device, compute_type)


@mcp.tool()
def start_job(url: str, source_language: str = "en", target_language: str = "zh-CN",
              output_dir: str | None = None, model: str | None = None, format_selector: str = "",
              proxy_url: str | None = None, cookie_file: str | None = None, initial_prompt: str = "",
              backend: str = "auto", device: str = "auto", compute_type: str = "auto") -> dict:
    """Start one video download and hardware-selected local transcription in a background process. Returns job ID immediately. Empty proxy_url disables proxy; None uses saved settings. Source auto detects language."""
    return manager.start_job(url, source_language, target_language, output_dir, model, format_selector, proxy_url, cookie_file, initial_prompt, backend, device, compute_type)


@mcp.tool()
def get_job(job_id: str) -> dict:
    """Read progress, errors, completed batch counts and output paths. Prefer completion notifications; otherwise poll after 10 seconds, back off to 60 seconds while unchanged, reset on phase change."""
    return manager.get_job(job_id)


@mcp.tool()
def list_jobs(limit: int = 20) -> list[dict]:
    """List recent persistent jobs to find and resume work after a conversation interruption."""
    return manager.list_jobs(limit)


@mcp.tool()
def get_caption_batch(job_id: str, batch_id: str | None = None) -> dict:
    """Read next untranslated batch (or a specific batch), immutable word IDs, original timestamps, context and glossary."""
    return manager.get_caption_batch(job_id, batch_id)


@mcp.tool()
def set_caption_batch_boundary(job_id: str, batch_id: str, revision: int, end_word_id: str) -> dict:
    """Before caption submission, move the boundary with the next unsubmitted batch to a semantic break. end_word_id is inclusive and must belong to either batch. Move an incomplete tail to the next batch or bring its continuation into this one. Both batches must be unsubmitted; each resulting batch is limited to 320 words. Word IDs/timestamps and other batches stay unchanged. Fetch the updated batch after success or a stale revision error."""
    return manager.set_caption_batch_boundary(job_id, batch_id, revision, end_word_id)


@mcp.tool()
def submit_caption_batch(job_id: str, batch_id: str, revision: int, captions: list[Caption],
                         glossary: dict[str, str] | None = None, notes: list[str] | None = None) -> dict:
    """Save Codex's proofreading, semantic segments and translations. Cover batch words exactly once with inclusive continuous ID ranges. Time is computed locally. Exact retries are idempotent; changed stale revisions are rejected. Notes record uncertain transcription."""
    return manager.submit_caption_batch(job_id, batch_id, revision, [c.model_dump() for c in captions], glossary, notes)


@mcp.tool()
def realign_job(job_id: str, revision: int) -> dict:
    """Explicitly migrate the entire saved transcript to independent acoustic alignment. Preserves word IDs, text and translations only if exact coverage and timing validation pass; otherwise retains originals and diagnostics. Invalidates exports on success; validate and export again. Never shifts times by a guessed offset."""
    return manager.realign_job(job_id, revision)


@mcp.tool()
def retranscribe_range(job_id: str, start_word_id: str, end_word_id: str, revision: int, initial_prompt: str = "") -> dict:
    """Start local re-transcription with the saved backend/device; expands to complete affected batches. Replaces their word IDs, invalidates their translations, and retains other completed batches. Fetch new batch IDs afterwards."""
    return manager.retranscribe_range(job_id, start_word_id, end_word_id, revision, initial_prompt)


@mcp.tool()
def validate_job(job_id: str) -> dict:
    """Check complete translation coverage, word timing, overlapping captions and reading speed. Structural errors block export; warnings require review."""
    return manager.validate_job(job_id)


@mcp.tool()
def get_cover_source(job_id: str) -> dict:
    """Return the downloaded original thumbnail path and the constrained GPT image-edit brief."""
    return manager.get_cover_source(job_id)


@mcp.tool()
def set_generated_cover(job_id: str, image_path: str) -> dict:
    """Validate a locally generated 4:3 cover and save it into this job for final export."""
    return manager.set_generated_cover(job_id, image_path)


@mcp.tool()
def export_job(job_id: str) -> dict:
    """Export named original/translated SRT, proofread source transcript, template description and final video into <title>/output."""
    return manager.export_job(job_id)


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """Stop only this job's worker and subprocesses, retaining checkpoints and downloaded partial files."""
    return manager.cancel_job(job_id)


@mcp.tool()
def resume_job(job_id: str) -> dict:
    """Resume failed/interrupted/cancelled work from completed stages without repeating accepted caption batches."""
    return manager.resume_job(job_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
