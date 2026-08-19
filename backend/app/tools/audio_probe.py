"""Local, evidence-first audio-track observation for generated MP4 videos.

The MVP deliberately performs only the capability it can prove without a
third-party model: whether a locally retained MP4 has an audio track and the
track's container duration. ASR, beat alignment, and lip-sync stay separate
tools rather than becoming invented scores.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.models import BadCase, EvaluationReport, ProjectState, Severity
from ..domain.rubric import apply_release_decision, profile_from_state


class AudioPresenceProbe:
    """Read standard ISO-BMFF/MP4 metadata with no local binary dependency."""

    max_file_bytes = 128 * 1024 * 1024

    def enrich_report(self, state: ProjectState, report: EvaluationReport) -> EvaluationReport:
        contract = state.creative_spec.evaluation_contract or {}
        criteria = [
            item for item in contract.get("acceptance_criteria", [])
            if isinstance(item, dict) and item.get("evidence_mode") == "audio_probe"
        ]
        if not criteria:
            return report

        result = self.observe(state)
        checks = [self._check(criterion, result) for criterion in criteria]
        report.atomic_checks.extend(checks)
        state.history.append({
            "at": "local-demo",
            "phase": "observe",
            "event": "audio_presence_probe_completed",
            "payload": {
                "message": result["notice"],
                "tool": "audio_probe",
                "status": result["status"],
                "audio_track_count": result.get("audio_track_count"),
                "audio_duration_seconds": result.get("audio_duration_seconds"),
            },
        })
        for check in checks:
            if check["status"] == "pass":
                continue
            severity = Severity.BLOCKING if check.get("critical") and check["status"] == "fail" else Severity.MAJOR
            report.bad_cases.append(BadCase(
                issue_type="required_audio_missing" if check["status"] == "fail" else "audio_observation_unavailable",
                shot_id=str(check.get("shot_id", "S1")),
                severity=severity,
                evidence=check["observed"],
                root_cause_hypothesis=check.get("repair_hint") or "The required audio presence could not be verified.",
                affected_dimensions=["audio_presence_integrity"],
            ))
        report.observation_notice = f"{report.observation_notice} {result['notice']}".strip()
        return apply_release_decision(report, profile_from_state(state))

    def observe(self, state: ProjectState) -> dict[str, Any]:
        asset = state.current_video_asset or {}
        raw_path = asset.get("local_video_path")
        if not isinstance(raw_path, str) or not raw_path:
            return {
                "status": "unavailable",
                "notice": "Audio presence could not be verified because this version has no locally retained video file.",
            }
        path = Path(raw_path)
        if not path.is_file() or path.stat().st_size <= 0:
            return {"status": "unavailable", "notice": "Audio presence could not be verified because the generated video file is unavailable."}
        if path.stat().st_size > self.max_file_bytes:
            return {"status": "unavailable", "notice": "Audio presence could not be verified because the local video exceeds the probe limit."}
        try:
            metadata = self._mp4_audio_metadata(path.read_bytes())
        except (OSError, ValueError) as exc:
            return {"status": "unavailable", "notice": f"Audio presence probe could not read the generated MP4 metadata: {exc}"}
        if metadata["audio_track_count"] <= 0:
            return {
                "status": "missing",
                "notice": f"Audio probe found no audio track in the generated video ({metadata['video_duration_seconds']:.2f}s).",
                **metadata,
            }
        duration = metadata.get("audio_duration_seconds") or metadata.get("video_duration_seconds")
        return {
            "status": "present",
            "notice": f"Audio probe found {metadata['audio_track_count']} audio track(s) covering about {duration:.2f}s.",
            **metadata,
        }

    @staticmethod
    def _check(criterion: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        status = result["status"]
        if status == "present":
            end = round(float(result.get("audio_duration_seconds") or result.get("video_duration_seconds") or 0), 2)
            return {
                **criterion,
                "status": "pass",
                "observed": result["notice"],
                "evidence": [{"source": "audio_probe", "start_seconds": 0.0, "end_seconds": end, "description": result["notice"]}],
                "repair_hint": "",
            }
        if status == "missing":
            return {
                **criterion,
                "status": "fail",
                "observed": result["notice"],
                "evidence": [{"source": "audio_probe", "start_seconds": 0.0, "end_seconds": round(float(result.get("video_duration_seconds") or 0), 2), "description": result["notice"]}],
                "repair_hint": "Generate or attach the required audio track before changing approved visual shots.",
            }
        return {
            **criterion,
            "status": "insufficient_evidence",
            "observed": result["notice"],
            "evidence": [],
            "repair_hint": "Retain a locally accessible generated video, then run the audio-presence probe again.",
        }

    @classmethod
    def _mp4_audio_metadata(cls, blob: bytes) -> dict[str, Any]:
        moov = next((payload for atom_type, payload in cls._atoms(blob) if atom_type == b"moov"), None)
        if moov is None:
            raise ValueError("missing moov atom")
        tracks = []
        for atom_type, trak_payload in cls._atoms(moov):
            if atom_type != b"trak":
                continue
            media = next((payload for kind, payload in cls._atoms(trak_payload) if kind == b"mdia"), None)
            if media is None:
                continue
            handler = next((payload for kind, payload in cls._atoms(media) if kind == b"hdlr"), b"")
            mdhd = next((payload for kind, payload in cls._atoms(media) if kind == b"mdhd"), b"")
            if len(handler) < 12 or len(mdhd) < 20:
                continue
            tracks.append((handler[8:12], cls._mdhd_seconds(mdhd)))
        video_duration = next((duration for kind, duration in tracks if kind == b"vide" and duration > 0), 0.0)
        audio_durations = [duration for kind, duration in tracks if kind == b"soun" and duration > 0]
        return {
            "audio_track_count": sum(1 for kind, _ in tracks if kind == b"soun"),
            "audio_duration_seconds": round(max(audio_durations), 2) if audio_durations else 0.0,
            "video_duration_seconds": round(video_duration or (max(audio_durations) if audio_durations else 0.0), 2),
        }

    @staticmethod
    def _atoms(blob: bytes):
        offset = 0
        length = len(blob)
        while offset + 8 <= length:
            size = int.from_bytes(blob[offset:offset + 4], "big")
            atom_type = blob[offset + 4:offset + 8]
            header = 8
            if size == 1:
                if offset + 16 > length:
                    break
                size = int.from_bytes(blob[offset + 8:offset + 16], "big")
                header = 16
            elif size == 0:
                size = length - offset
            if size < header or offset + size > length:
                break
            yield atom_type, blob[offset + header:offset + size]
            offset += size

    @staticmethod
    def _mdhd_seconds(payload: bytes) -> float:
        version = payload[0]
        if version == 1 and len(payload) >= 32:
            timescale = int.from_bytes(payload[20:24], "big")
            duration = int.from_bytes(payload[24:32], "big")
        elif version == 0 and len(payload) >= 20:
            timescale = int.from_bytes(payload[12:16], "big")
            duration = int.from_bytes(payload[16:20], "big")
        else:
            return 0.0
        return duration / timescale if timescale else 0.0
