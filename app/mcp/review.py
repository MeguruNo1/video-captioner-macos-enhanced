"""Persistent, evidence-based review of transcript and caption observations."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import re

from .captions import issue_summary, review_issue, validate


# Increment when detector semantics change, even if word/caption revision does not.
REVIEW_POLICY_VERSION = 2


def review_is_current(state):
    return ("review_issues" in state
            and state.get("review_revision") == state.get("revision")
            and state.get("review_policy_version") == REVIEW_POLICY_VERSION)


def prepare_evidence(audio, subtitle_path=None, *, threshold=0.5, compare_subtitles=True, local_silero_dir=None):
    """Local-only evidence. Missing VAD or captions is explicit, never a clean bill."""
    from app.core.bk_asr.mlx_workflow import find_local_silero_repository, detect_speech_ranges
    evidence = {"version": 1, "speech_ranges": [], "source_cues": [], "checks": {}}
    checks = evidence["checks"]
    repository = find_local_silero_repository(local_silero_dir)
    if repository:
        try:
            ranges = detect_speech_ranges(audio, threshold=threshold, strict=True, local_repo=repository)
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
            "Source subtitle and ASR differ substantially; neither is ground truth",
            _anchors(words, start, end), start_ms=start, end_ms=end,
            evidence={"source_text": text[:1500], "asr_text": " ".join(w["text"] for w in overlapping)[:1500],
                      "token_recall": round(recall, 3)}))
    return issues


def reconcile_review(state, *, batch_id=None):
    """Update observations after a mutation; unrelated reviews keep their decisions."""
    if "review_issues" not in state or state.get("review_policy_version") != REVIEW_POLICY_VERSION:
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
    state["review_policy_version"] = REVIEW_POLICY_VERSION
    return state


def review_counts(state):
    counts = {"pending": 0, "retained": 0, "resolved": 0, "pending_transcript": 0}
    for issue in state.get("review_issues", {}).values():
        counts[issue["status"]] += 1
        if issue["status"] == "pending" and issue["stage"] == "transcript":
            counts["pending_transcript"] += 1
    return counts


def quality_status(state):
    """Issue decisions never establish whole-video acoustic acceptance."""
    records = list(state.get("review_issues", {}).values())
    active = [i for i in records if i["status"] != "resolved"]
    available = review_is_current(state)
    complete = bool(available and state.get("words") and state.get("batches")
                    and all(b.get("captions") for b in state["batches"])
                    and not any(i["status"] == "pending" or i["severity"] == "error" for i in active))
    return {"exported": state.get("status") == "completed" and bool(state.get("artifacts")),
            "review_complete": complete, "review_scope": "detected_observations_only",
            "review_current": available, "review_policy_version": state.get("review_policy_version"),
            "full_audio_review": "not_recorded",
            "audio_reviewed_issue_count": sum(i["status"] == "retained" and i.get("review_method") == "audio" for i in active),
            "active_issue_summary": issue_summary(active),
            "pending_issue_summary": issue_summary([i for i in active if i["status"] == "pending"]),
            "instruction": "Export success and resolved observations do not prove acoustic correctness. Full-video listening is not recorded; report pending and retained observations with the delivery."}


def review_checks(state):
    saved = state.get("review_evidence", {}).get("checks", {})
    return {name: saved.get(name, {"status": "unavailable", "reason": "No saved evidence for this check; legacy or unchecked task"})
            for name in ("speech_activity", "source_subtitles")}


def checkpoint(state):
    """Computed from persisted data, never from conversation memory."""
    if state.get("words") and not review_is_current(state):
        state = reconcile_review(deepcopy(state))
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
            "review_checks": review_checks(state), "quality_status": quality_status(state),
            "review_available": "review_issues" in state,
            "review_instruction": "Resolved means the detector no longer sees the observation, not proof of acoustic correctness. Retained issues keep explicit review evidence."}
