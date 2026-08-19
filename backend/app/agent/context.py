"""Bounded pre-generation context enrichment for the Video Control Loop."""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import date
from typing import Any

from .video_capabilities import VideoCapabilityRegistry
from .asset_manifest import build_asset_manifest


SEASON_TERMS = (
    "seasonal", "in season", "current season", "holiday", "summer", "winter", "spring", "autumn",
    "应季", "当季", "当前季节", "节日", "夏季", "冬季", "春季", "秋季",
)
SHORT_FORM_TERMS = ("tiktok", "reel", "shorts", "抖音", "短视频", "种草", "爆款")
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def _deepseek_chat_endpoint(base_url: str | None = None) -> str:
    configured = (base_url or os.getenv("DEEPSEEK_API_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL).strip().rstrip("/")
    return configured if configured.endswith("/chat/completions") else f"{configured}/chat/completions"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(term in lowered for term in terms)


class ContextEnrichmentAgent:
    """Suggest and execute a small allow-listed set of enrichment tools.

    A live LLM may suggest optional tool calls, but hard safety conditions are
    enforced locally: a seasonal claim without a market always asks the user;
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
        deepseek_base_url: str | None = None,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        self.endpoint = _deepseek_chat_endpoint(deepseek_base_url)
        self.vision_api_key = vision_api_key or os.getenv("ARK_API_KEY")
        self.vision_model = vision_model or os.getenv("ARK_VISION_MODEL")
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
        seasonal_request = _contains_any(prompt, SEASON_TERMS)
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
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0.0,
            "max_tokens": 350,
        }).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = json.loads(response.read().decode("utf-8"))
        return json.loads(raw["choices"][0]["message"]["content"])

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
        body = json.dumps({
            "model": self.vision_model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": system}, {"type": "input_image", "image_url": preview}]}],
            "max_output_tokens": 500, "thinking": {"type": "disabled"},
        }).encode("utf-8")
        try:
            request = urllib.request.Request("https://ark.cn-beijing.volces.com/api/v3/responses", data=body, headers={"Authorization": f"Bearer {self.vision_api_key}", "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = json.loads(response.read().decode("utf-8"))
            text = self._response_text(raw)
            parsed = json.loads(text[text.find("{"):text.rfind("}") + 1])
            features = [str(item).strip() for item in parsed.get("preserve_features", []) if isinstance(item, str) and item.strip()][:4]
            return {"status": "completed", "observations": parsed.get("observations", [])[:4], "inferences": parsed.get("inferences", [])[:2], "unknowns": parsed.get("unknowns", [])[:4], "preserve_features": features, "summary": "Visual reference features were extracted with the configured observation model."}
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return {"status": "unavailable", "summary": "Reference pre-analysis was unavailable; no visual detail was promoted to a hard constraint.", "preserve_features": []}

    @staticmethod
    def _response_text(raw: dict[str, Any]) -> str:
        if isinstance(raw.get("output_text"), str) and raw["output_text"].strip():
            return raw["output_text"].strip()
        parts = []
        for item in raw.get("output", []):
            if isinstance(item, dict):
                for content in item.get("content", []):
                    if isinstance(content, dict) and isinstance(content.get("text"), str):
                        parts.append(content["text"])
        result = "\n".join(parts).strip()
        if not result:
            raise ValueError("Reference analysis returned no text")
        return result

    def _capability_router(
        self,
        reference_asset: dict[str, Any] | None,
        multimodal_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.capabilities.route(reference_asset, multimodal_assets)
