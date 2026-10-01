"""Prompt versions for penalty-mitigation-summary generation."""

from app.agents.penalties.mitigation.prompts.v1 import (
    PROMPT_VERSION as V1_PROMPT_VERSION,
)
from app.agents.penalties.mitigation.prompts.v1 import (
    SYSTEM_PROMPT as V1_SYSTEM_PROMPT,
)
from app.agents.penalties.mitigation.prompts.v2 import (
    PROMPT_VERSION as V2_PROMPT_VERSION,
)
from app.agents.penalties.mitigation.prompts.v2 import (
    SYSTEM_PROMPT as V2_SYSTEM_PROMPT,
)
from app.agents.penalties.mitigation.prompts.v3 import (
    PROMPT_VERSION as V3_PROMPT_VERSION,
)
from app.agents.penalties.mitigation.prompts.v3 import (
    SYSTEM_PROMPT as V3_SYSTEM_PROMPT,
)

__all__ = [
    "V1_PROMPT_VERSION",
    "V1_SYSTEM_PROMPT",
    "V2_PROMPT_VERSION",
    "V2_SYSTEM_PROMPT",
    "V3_PROMPT_VERSION",
    "V3_SYSTEM_PROMPT",
]
