"""Persistent, evidence-based review of transcript and caption observations."""
from collections import Counter
from pathlib import Path
import re

from .captions import review_issue, validate


def prepare_evidence(audio, subtitle_path=None, *, threshold=0.5, compare_subtitles=True):
    """Local-only evidence. Missing VAD or captions is explicit, never a clean bill."""
    from app.core.bk_asr.mlx_workflow import LOCAL_SILERO_REPO, detect_speech_ranges
    evidence = {"version": 1, "speech_ranges": [], "source_cues": [], "checks": {}}
    checks = evidence["checks"]
    if (LOCAL_SILERO_REPO / "hubconf.py").is_file():
        try:
            ranges = detect_speech_ranges(audio, threshold=threshold, strict=True)
            evidence["speech_ranges"] = [{"start_ms": round(s * 1000), "end_ms": round(e * 1000)} for s, e in ranges]
            checks["speech_activity"] = {"status": "checked", "range_count": len(ranges)}
        except Exception as exc:
            checks["speech_activity"] = {"status": "unavailable", "reason": str(exc)}
    else:
        checks["speech_activity"] = {"status": "unavailable", "reason": "Local Silero VAD is not cached; no model was downloaded"}
    if not compare_subtitles:
        checks["source_subtitles"] = {"status": "skipped", "reason": "Source subtitle and detected transcript languages differ or are unknown"}
    elif subtitle_path and Path(subtitle_path).is_file():
        try:
            from app.core.bk_asr.asr_data import ASRData
            data = ASRData.from_subtitle_file(str(subtitle_path))
            evidence["source_cues"] = [{"start_ms": s.start_time, "end_ms": s.end_time, "text": s.text}
                                       for s in data.segments if s.end_time > s.start_time]
            checks["source_subtitles"] = {"status": "checked", "cue_count": len(evidence["source_cues"])}
        except Exception as exc:
            checks["source_subtitles"] = {"status": "unavailable", "reason": str(exc)}
    else:
        checks["source_subtitles"] = {"status": "unavailable", "reason": "No source subtitle file"}
    return evidence


def _tokens(text):
    return re.findall(r"[\u3400-\u9fff]|[^\W_\u3400-\u9fff]+", text.casefold())


def _anchors(words, start, end):
    intersecting = [w for w in words if w["end_ms"] >= start and w["start_ms"] <= end]
    if intersecting:
        return [intersecting[0], intersecting[-1]]
    before = [w for w in words if w["end_ms"] <= start]
    after = [w for w in words if w["start_ms"] >= end]
    return (before[-1:] + after[:1]) or words[:1]


def coverage_issues(state):
    """Speech without word coverage and lexical disagreement are review hints only."""
    words = state.get("words", [])
    evidence = state.get("review_evidence", {})
    issues = []
    coverage = sorted((max(0, w["start_ms"] - 150), w["end_ms"] + 150) for w in words if w["end_ms"] > w["start_ms"])
    for speech in evidence.get("speech_ranges", []):
        start, end = speech["start_ms"], speech["end_ms"]
        cursor = start
        gaps = []
        for first, last in coverage:
            if last <= cursor:
                continue
            if first >= end:
                break
            if first > cursor:
                gaps.append((cursor, min(first, end)))
            cursor = max(cursor, last)
            if cursor >= end:
                break
        if cursor < end:
            gaps.append((cursor, end))
        for first, last in gaps:
            if last - first < 900:
                continue
            # Keep each listening unit short, even for a long missing passage.
            for clip_start in range(first, last, 20000):
                clip_end = min(last, clip_start + 20000)
                anchors = _anchors(words, clip_start, clip_end)
                issues.append(review_issue("suspected_missing_speech", "warning",
                    "Speech activity has no ASR word coverage; inspect audio before translating",
                    anchors, start_ms=clip_start, end_ms=clip_end,
                    evidence={"source": "local_vad", "minimum_gap_ms": 900}))
    seen = set()
    for cue in evidence.get("source_cues", []):
        start, end, text = cue["start_ms"], cue["end_ms"], cue["text"]
        expected = Counter(_tokens(re.sub(r"\[[^\]]*\]|<[^>]*>", "", text)))
        if sum(expected.values()) < 4 or end - start < 500:
            continue
        overlapping = [w for w in words if w["end_ms"] > start - 250 and w["start_ms"] < end + 250]
        actual = Counter(_tokens(" ".join(w["text"] for w in overlapping)))
        recall = sum((expected & actual).values()) / sum(expected.values())
        key = (start, end, text)
        if recall >= .35 or key in seen:
            continue
        seen.add(key)
        issues.append(review_issue("source_caption_disagreement", "warning",
            "Source auto-caption and ASR differ substantially; neither is ground truth",
            _anchors(words, start, end), start_ms=start, end_ms=end,
            evidence={"source_text": text[:1500], "asr_text": " ".join(w["text"] for w in overlapping)[:1500],
                      "token_recall": round(recall, 3)}))
    return issues


def reconcile_review(state, *, batch_id=None):
    """Update observations after a mutation; unrelated reviews keep their decisions."""
    if "review_issues" not in state:
        batch_id = None
    phase = "batch" if batch_id else "delivery"
    observations = validate(state, limit=None, phase=phase, batch_id=batch_id)["issues"]
    records = state.setdefault("review_issues", {})
    if batch_id:
        batches = state["batches"]
        position = next(i for i, b in enumerate(batches) if b["id"] == batch_id)
        scope = {b["id"] for b in batches[max(0, position - 1):position + 2]}
        observations = [i for i in observations if i["stage"] == "caption"]
        obsolete = {key for key, issue in records.items() if issue["stage"] == "caption" and issue["batch_id"] in scope}
    else:
        observations = [i for i in observations if i["code"] not in {"untranslated_batch", "no_transcript"}]
        observations += coverage_issues(state)
        obsolete = set(records)
    for issue in observations:
        key = issue["id"]
        previous = records.get(key, {})
        # Changed evidence has a new ID; a previously disappeared issue reopens.
        decision = {k: previous[k] for k in ("status", "review_note", "review_method", "reviewed_revision") if k in previous}
        if decision.get("status") == "resolved":
            decision = {}
        records[key] = dict(issue, status="pending", **{k: v for k, v in decision.items() if k != "status"})
        records[key]["status"] = decision.get("status", "pending")
        records[key]["observed_revision"] = state["revision"]
        obsolete.discard(key)
    for key in obsolete:
        records[key].update(status="resolved", resolution="observation_no_longer_present")
    state["review_revision"] = state["revision"]
    return state


def review_counts(state):
    counts = {"pending": 0, "retained": 0, "resolved": 0, "pending_transcript": 0}
    for issue in state.get("review_issues", {}).values():
        counts[issue["status"]] += 1
        if issue["status"] == "pending" and issue["stage"] == "transcript":
            counts["pending_transcript"] += 1
    return counts


def checkpoint(state):
    """Computed from persisted data, never from conversation memory."""
    counts = review_counts(state)
    cover_ready = bool(state.get("thumbnail_path")) and Path(state["thumbnail_path"]).is_file()
    cover_done = bool(state.get("generated_cover_path")) and Path(state["generated_cover_path"]).is_file()
    pending = next((b for b in state.get("batches", []) if b["captions"] is None), None)
    status = state["status"]
    if status in {"failed", "interrupted", "cancelled"}:
        action = "resume_job"
    elif status == "completed" and state.get("artifacts"):
        action = "deliver_artifacts"
    elif status in {"starting", "downloading", "extracting", "waiting_for_mlx", "waiting_for_asr", "transcribing", "retranscribing", "checking_transcript"}:
        action = "prepare_cover" if cover_ready and not cover_done else "wait_job"
    elif counts["pending_transcript"]:
        action = "review_transcript"
    elif pending:
        action = "get_caption_batch"
    elif counts["pending"]:
        action = "review_captions"
    elif state.get("cover_required") and not cover_done:
        action = "prepare_cover" if cover_ready else "restore_original_cover"
    else:
        action = "validate_and_export"
    return {"next_action": action, "next_batch_id": pending["id"] if pending else None,
            "review": counts, "cover_ready": cover_ready, "cover_complete": cover_done,
            "review_checks": state.get("review_evidence", {}).get("checks", {}),
            "review_available": "review_issues" in state,
            "review_instruction": "Resolved means the detector no longer sees the observation, not proof of acoustic correctness. Retained issues keep explicit review evidence."}
