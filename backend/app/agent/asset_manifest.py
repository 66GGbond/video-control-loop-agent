"""Deterministic manifest for user-provided multimodal reference assets.

It preserves upload order and explicit ``@`` associations without claiming the
planner has understood pixels or audio semantics.  The same metadata is carried
through planning, generation, and evaluation contracts.
"""

from __future__ import annotations

from typing import Any, Iterable


MAX_EVALUATION_IMAGE_REFS = 3


def build_asset_manifest(assets: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Build a stable, safe-to-log manifest from normalized user assets."""
    ordered: list[tuple[int, int, dict[str, Any]]] = []
    for fallback_order, asset in enumerate(assets or [], start=1):
        if not isinstance(asset, dict):
            continue
        try:
            input_order = int(asset.get("input_order", fallback_order))
        except (TypeError, ValueError):
            input_order = fallback_order
        ordered.append((input_order, fallback_order, asset))

    entries: list[dict[str, Any]] = []
    for index, (input_order, _fallback_order, asset) in enumerate(sorted(ordered), start=1):
        media_type = str(asset.get("media_type", "")).lower().strip()
        if media_type not in {"image", "audio", "video"}:
            continue
        name = str(asset.get("name", f"reference-{index}")).strip() or f"reference-{index}"
        raw_mentions = asset.get("prompt_mentions", [])
        if not isinstance(raw_mentions, list):
            raw_mentions = []
        mentions = list(dict.fromkeys(
            str(item).strip()[:140] for item in raw_mentions if str(item).strip()
        ))[:3]
        explicitly_referenced = bool(mentions)
        entries.append({
            "asset_id": f"R{index}",
            "input_order": input_order,
            "name": name,
            "media_type": media_type,
            "declared_role": str(asset.get("role", "reference")).strip()[:80] or "reference",
            "prompt_mentions": mentions,
            "provider_role": f"reference_{media_type}",
            "binding_status": "explicitly_referenced" if explicitly_referenced else "conditioning_only",
            "planning_eligible": True,
            "evaluation_eligible": media_type == "image" and explicitly_referenced,
            "evaluation_note": (
                "Use only for acceptance criteria that name this asset's @ mention."
                if media_type == "image" and explicitly_referenced else
                "Retained for generation conditioning; no reference-specific visual judgement is authorised."
                if media_type == "image" else
                "Retained for generation conditioning; audio semantics require a declared audio observation tool."
            ),
        })
    return entries


def bind_criteria_to_assets(
    criteria: Iterable[dict[str, Any]], manifest: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach explicit, inspectable image-reference bindings to each criterion."""
    entries = [entry for entry in manifest if isinstance(entry, dict)]
    output: list[dict[str, Any]] = []
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        text = " ".join(str(criterion.get(key, "")) for key in ("requirement", "expected")).casefold()
        bindings: list[str] = []
        for entry in entries:
            if entry.get("media_type") != "image":
                continue
            aliases = [str(entry.get("name", "")).casefold()]
            aliases.extend(str(token).casefold() for token in entry.get("prompt_mentions", []) if str(token).strip())
            if any(alias and alias in text for alias in aliases):
                bindings.append(str(entry["asset_id"]))
        output.append({**criterion, "asset_bindings": bindings})
    return output


def evaluation_reference_entries(manifest: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Limit visual QA to explicitly-bound image references."""
    return [
        entry for entry in manifest
        if isinstance(entry, dict)
        and entry.get("media_type") == "image"
        and entry.get("evaluation_eligible")
    ][:MAX_EVALUATION_IMAGE_REFS]
