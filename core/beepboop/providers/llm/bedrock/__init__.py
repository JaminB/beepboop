"""Amazon Bedrock ConverseStream provider (docs/LLM_PROVIDERS.md §3).

``provider.BedrockProvider`` implements ``LLMProvider``; ``mapping`` holds the
pure request/stream translation; ``probe`` is the M0 G3 TTFT probe.
"""

from beepboop.providers.llm.bedrock.provider import BedrockProvider, BedrockSettings, make_client

__all__ = ["BedrockProvider", "BedrockSettings", "make_client"]
