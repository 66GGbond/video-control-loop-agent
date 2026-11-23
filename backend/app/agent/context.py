"""Bounded pre-generation context enrichment for the Video Control Loop."""

from __future__ import annotations

import json
import os
from ..tools.ark_responses import configured_model, request_json, response_text, DEFAULT_ENDPOINT
from datetime import date
from typing import Any

from .video_capabilities import VideoCapabilityRegistry
from .asset_manifest import build_asset_manifest


SEASON_TERMS = (
    "seasonal", "in season", "current season", "holiday", "summer", "winter", "spring", "autumn",
    "应季", "当季", "当前季节", "节日", "夏季", "冬季", "春季", "秋季",
)
SHORT_FORM_TERMS = ("tiktok", "reel", "shorts", "抖音", "短视频", "种草", "爆款")
def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(term in lowered for term in terms)


def explicit_market(prompt):
    countries={'英国':('英国','united kingdom','britain','england'),'中国':('中国','china'),'美国':('美国','united states'),'新加坡':('新加坡','singapore'),'澳大利亚':('澳大利亚','australia'),'日本':('日本','japan')}
    matches=[name for name,words in countries.items() if _contains_any(prompt,words)]
    return matches[0] if len(matches)==1 else ''


def requires_market_confirmation(prompt,target_market=''):
    realtime=_contains_any(prompt,('seasonal','in season','current season','应季','当季','当前季节','本季营销','今年节日'))
    return realtime and not (target_market or explicit_market(prompt))


class ContextEnrichmentAgent:
    """Suggest and execute a small allow-listed set of enrichment tools.

    A live LLM may suggest optional tool calls, but hard safety conditions are
    enforced locally: only a current-season regional request may need a market;
    visual reference facts are never fabricated when no vision tool is present.
    """

    allowed_tools = {"calendar_context", "reference_asset_analysis", "capability_router", "short_form_pattern_library"}

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        vision_api_key: str | None = None,
        vision_model: str | None = None,
        text_video_model: str | None = None,
        reference_video_model: str | None = None,
        base_url: str | None = None,
    ):
        self.api_key = api_key or os.getenv("ARK_API_KEY")
        self.model = model or configured_model()
        self.endpoint = base_url or DEFAULT_ENDPOINT
        self.vision_api_key = vision_api_key or os.getenv("ARK_API_KEY")
        self.vision_model = vision_model or configured_model()
        self.text_video_model = text_video_model or os.getenv("ARK_VIDEO_TEXT_MODEL")
        self.reference_video_model = reference_video_model or os.getenv("ARK_VIDEO_REFERENCE_MODEL")
        self.capabilities = VideoCapabilityRegistry(self.text_video_model, self.reference_video_model)

    def enrich(
        self,
        prompt: str,
        reference_asset: dict[str, Any] | None = None,
        target_market: str = "",
        multimodal_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        target_market = (target_market or "").strip()
        asset_manifest = build_asset_manifest(multimodal_assets or ([reference_asset] if reference_asset else []))
        suggested = self._suggest_tools(prompt, bool(reference_asset), bool(target_market))
        # Story seasons are supplied creative facts, not a request for today's weather.
        seasonal_request = _contains_any(prompt, ('seasonal','in season','current season','应季','当季','当前季节','本季营销','今年节日'))
        if seasonal_request and not target_market:target_market=explicit_market(prompt)
        short_form_request = _contains_any(prompt, SHORT_FORM_TERMS)
        tool_names = {item.get("tool") for item in suggested if isinstance(item, dict)}
        # A model can only choose from the registered tools; code also verifies
        # that the chosen tool is relevant to observable request signals.
        if not seasonal_request:
            tool_names.discard("calendar_context")
        if not reference_asset:
            tool_names.discard("reference_asset_analysis")
        if not short_form_request:
            tool_names.discard("short_form_pattern_library")
        # The availability router is always useful and read-only. The other
        # tools are optional, but explicit seasonal language cannot be ignored.
        tool_names.add("capability_router")
        if reference_asset:
            tool_names.add("reference_asset_analysis")
        if seasonal_request:
            tool_names.add("calendar_context")

        open_questions: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        constraints: list[dict[str, Any]] = []
        if target_market:
            facts.append({"key": "target_market", "value": target_market, "provenance": "user_fact", "impact": "high"})
        if reference_asset:
            facts.append({
                "key": "reference_asset",
                "value": reference_asset.get("name", "reference image"),
                "provenance": "user_fact",
                "impact": "high",
            })
        if asset_manifest:
            facts.append({
                "key": "asset_manifest",
                "value": ", ".join(
                    f"{entry['asset_id']} {entry['media_type']} ({entry['binding_status']})"
                    for entry in asset_manifest
                ),
                "provenance": "user_fact",
                "impact": "high",
            })
            observations.append({
                "tool": "asset_manifest",
                "status": "completed",
                "result": {"summary": f"Retained {len(asset_manifest)} ordered user reference asset(s) with explicit @ bindings."},
            })

        if "calendar_context" in tool_names:
            if not target_market:
                open_questions.append({
                    "id": "target_market",
                    "question": "Which market or region should this seasonal video be made for?",
                    "why": "Seasonal and holiday context can differ by market and hemisphere.",
                    "blocking": True,
                })
            else:
                calendar = self._calendar_context(target_market)
                observations.append({"tool": "calendar_context", "status": "completed", "result": calendar})
                facts.append({"key": "season_context", "value": calendar["summary"], "provenance": "tool_fact", "impact": "medium"})

        if "reference_asset_analysis" in tool_names and reference_asset:
            analysis = self._reference_analysis(reference_asset)
            observations.append({"tool": "reference_asset_analysis", "status": analysis["status"], "result": analysis})
            for item in analysis.get("preserve_features", [])[:4]:
                constraints.append({"text": item, "strength": "hard", "source": "tool_fact"})

        capability = self._capability_router(reference_asset, multimodal_assets)
        observations.append({"tool": "capability_router", "status": "completed", "result": capability})
        if capability["route"] == "unavailable":
            open_questions.append({
                "id": "generation_path",
                "question": "No configured video-generation adapter is available for this local run. Please configure a verified adapter before generation.",
                "why": capability["reason"],
                "blocking": False,
            })

        # The short-form library remains a declared Adapter but is deliberately
        # not executed in this MVP; it must never be presented as live trend data.
        if short_form_request and "short_form_pattern_library" in tool_names:
            observations.append({
                "tool": "short_form_pattern_library",
                "status": "deferred",
                "result": {"summary": "Optional local pattern library is not enabled in this MVP; no live trend claim was used."},
            })

        status = "needs_user_confirmation" if any(item.get("blocking") for item in open_questions) else "ready_for_compile"
        completed = [item["tool"] for item in observations if item["status"] == "completed"]
        summary = (
            f"Agent checked {', '.join(completed)} before compiling the control plan."
            if completed else "No external context was required; the Agent will compile directly from the approved brief."
        )
        return {
            "context_version": "ctx_v1",
            "status": status,
            "facts": facts,
            "constraints": constraints,
            "tool_observations": observations,
            "open_questions": open_questions,
            "capability_decision": capability,
            "asset_manifest": asset_manifest,
            "summary": summary,
        }

    def _suggest_tools(self, prompt: str, has_reference: bool, has_market: bool) -> list[dict[str, str]]:
        fallback = self._fallback_suggestion(prompt, has_reference)
        if not self.api_key:
            return fallback
        system = """You are the context-planning module of an AI video agent. Return JSON only.
Choose zero or more registered tools that would materially improve generation controllability:
calendar_context, reference_asset_analysis, capability_router, short_form_pattern_library.
Do not claim live trend data. If seasonal or holiday context is central and market is missing, set needs_target_market=true.
Schema: {"tool_requests":[{"tool":"registered name","reason":"short reason"}],"needs_target_market":true|false}."""
        try:
            parsed = self._request_json(system, {"creative_brief": prompt, "has_reference_asset": has_reference, "has_target_market": has_market})
            raw = parsed.get("tool_requests", []) if isinstance(parsed, dict) else []
            requests = [item for item in raw if isinstance(item, dict) and item.get("tool") in self.allowed_tools]
            return requests or fallback
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _fallback_suggestion(prompt: str, has_reference: bool) -> list[dict[str, str]]:
        suggestions = [{"tool": "capability_router", "reason": "Choose a bounded generation path from available capabilities."}]
        if _contains_any(prompt, SEASON_TERMS):
            suggestions.append({"tool": "calendar_context", "reason": "Seasonal wording may require market-specific context."})
        if has_reference:
            suggestions.append({"tool": "reference_asset_analysis", "reason": "A visual reference may contain preservation constraints."})
        if _contains_any(prompt, SHORT_FORM_TERMS):
            suggestions.append({"tool": "short_form_pattern_library", "reason": "A short-form structure may be relevant, but no live trend claim is made."})
        return suggestions

    def _request_json(self, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        return request_json(self.api_key, self.model, system, payload, 700, timeout=60, endpoint=self.endpoint)

    @staticmethod
    def _calendar_context(market: str) -> dict[str, Any]:
        today = date.today()
        normalized = market.lower()
        southern = any(name in normalized for name in ("australia", "new zealand", "argentina", "chile", "south africa", "澳大利亚", "新西兰", "阿根廷", "智利", "南非"))
        tropical = any(name in normalized for name in ("singapore", "indonesia", "malaysia", "新加坡", "印度尼西亚", "马来西亚"))
        if tropical:
            season = "tropical / local seasonal pattern required"
            limitation = "This local MVP does not infer a four-season visual style for tropical markets."
        else:
            month = today.month
            month = ((month + 5) % 12) + 1 if southern else month
            season = "spring" if month in {3, 4, 5} else "summer" if month in {6, 7, 8} else "autumn" if month in {9, 10, 11} else "winter"
            limitation = "Local calendar context only; no live holiday campaign data is claimed."
        return {
            "effective_date": today.isoformat(), "region": market, "season": season,
            "source_type": "local_curated_calendar", "freshness": today.isoformat(),
            "summary": f"For {market} on {today.isoformat()}, the local calendar context is {season}.", "limitations": limitation,
        }

    def _reference_analysis(self, asset: dict[str, Any]) -> dict[str, Any]:
        preview = asset.get("preview_data_url")
        if not (self.vision_api_key and self.vision_model and isinstance(preview, str) and preview.startswith("data:image/")):
            return {"status": "unavailable", "summary": "Reference image retained for generation and later evaluation; visual pre-analysis is not configured.", "preserve_features": []}
        system = """Observe a user-provided reference image for an AI video control plan. Return JSON only:
{"observations":[{"attribute":"visible fact","value":"short visible description"}],"inferences":[{"attribute":"soft style inference","value":"description"}],"unknowns":["unverified detail"],"preserve_features":["2 to 4 visible features that should be preserved"]}.
Only report visible image facts as observations. Do not identify people, infer brands, or claim unreadable text."""
        try:
            parsed = request_json(self.vision_api_key, self.vision_model, system, None, 900, content=[{"type": "input_text", "text": "Observe the attached reference image. Return JSON."}, {"type": "input_image", "image_url": preview}])
            features = [str(item).strip() for item in parsed.get("preserve_features", []) if isinstance(item, str) and item.strip()][:4]
            return {"status": "completed", "observations": parsed.get("observations", [])[:4], "inferences": parsed.get("inferences", [])[:2], "unknowns": parsed.get("unknowns", [])[:4], "preserve_features": features, "summary": "Visual reference features were extracted with the configured observation model."}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return {"status": "unavailable", "summary": "Reference pre-analysis was unavailable; no visual detail was promoted to a hard constraint.", "preserve_features": []}

    @staticmethod
    def _response_text(raw: dict[str, Any]) -> str:
        return response_text(raw)

    def _capability_router(
        self,
        reference_asset: dict[str, Any] | None,
        multimodal_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.capabilities.route(reference_asset, multimodal_assets)
