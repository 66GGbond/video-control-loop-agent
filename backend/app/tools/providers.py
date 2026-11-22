"""Factory for the explicitly selected video-generation provider."""

from __future__ import annotations

import os

from .generator import ArkVideoGenerator
from .minimax_hailuo import MiniMaxHailuoGenerator
from .minimax_h3 import MiniMaxH3Generator


def create_video_generator():
    """Return one provider adapter; never silently switch a user's provider."""
    provider = os.getenv("VIDEO_PROVIDER", "ark").strip().lower()
    if provider == "auto":
        from ..agent.model_router import RoutingVideoGenerator
        return RoutingVideoGenerator()
    if provider in {"minimax", "minimax_hailuo", "hailuo"}:
        return MiniMaxHailuoGenerator()
    if provider in {"minimax_h3", "h3"}:
        return MiniMaxH3Generator()
    if provider in {"", "ark", "seedance"}:
        return ArkVideoGenerator()
    raise ValueError("VIDEO_PROVIDER must be 'ark', 'minimax_h3', or 'minimax_hailuo'")
