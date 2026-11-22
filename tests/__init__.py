"""Keep fixture tests offline even when the checkout has real local API keys."""
import os

for _key in ("ARK_API_KEY", "MINIMAX_API_KEY"):
    os.environ[_key] = ""

# Model selection must also stay deterministic instead of inheriting .env.
os.environ["VIDEO_AGENT_SKIP_LOCAL_ENV"] = "1"
for _key in ("VIDEO_PROVIDER",
             "ARK_MODEL", "ARK_VIDEO_TEXT_MODEL", "ARK_VIDEO_REFERENCE_MODEL",
             "MINIMAX_VIDEO_MODEL", "MINIMAX_VIDEO_RESOLUTION", "MINIMAX_API_BASE_URL"):
    os.environ.pop(_key, None)
