"""The agent: turn loop, fast path, prompt assembly (ARCHITECTURE §5, LLM_PROVIDERS §7-§12).

``wiring`` and ``voice`` are imported explicitly (they pull in the database and the event
protocol); everything else is re-exported here.
"""

from beepboop.agent.chunking import SentenceChunker, clean_for_speech
from beepboop.agent.context import (
    CHILD_BLOCK,
    VOICE_CONTRACT,
    PromptInputs,
    assemble_request,
    build_prompt_inputs,
    prefix_hash,
    tool_defs,
)
from beepboop.agent.fastpath import FastPath, FastPathMatch, Grammar, GrammarError, load_grammar
from beepboop.agent.phrases import PHRASES, deny_speech, phrase
from beepboop.agent.turn import (
    ListSpeaker,
    MemoryConversation,
    NoLLM,
    NullTurnLog,
    StaticDirectory,
    TurnLoop,
    deny_phrase,
)
from beepboop.agent.types import (
    CONVERSATION_WINDOW_S,
    MAX_TOOL_CALLS_PER_TURN,
    REPLY_WINDOW_S,
    SPEAKER_FINAL_WAIT_S,
    STAGES,
    ConversationStore,
    PendingApproval,
    PinStore,
    ProfileDirectory,
    Speaker,
    Speech,
    ToolCallSummary,
    TurnLog,
    TurnPath,
    TurnResult,
    TurnStart,
    Utterance,
    VoiceControl,
)
from beepboop.agent.usage import TurnUsage, load_pricing

__all__ = [
    "CHILD_BLOCK",
    "CONVERSATION_WINDOW_S",
    "MAX_TOOL_CALLS_PER_TURN",
    "PHRASES",
    "REPLY_WINDOW_S",
    "SPEAKER_FINAL_WAIT_S",
    "STAGES",
    "VOICE_CONTRACT",
    "ConversationStore",
    "FastPath",
    "FastPathMatch",
    "Grammar",
    "GrammarError",
    "ListSpeaker",
    "MemoryConversation",
    "NoLLM",
    "NullTurnLog",
    "PendingApproval",
    "PinStore",
    "ProfileDirectory",
    "PromptInputs",
    "SentenceChunker",
    "Speaker",
    "Speech",
    "StaticDirectory",
    "ToolCallSummary",
    "TurnLog",
    "TurnLoop",
    "TurnPath",
    "TurnResult",
    "TurnStart",
    "TurnUsage",
    "Utterance",
    "VoiceControl",
    "assemble_request",
    "build_prompt_inputs",
    "clean_for_speech",
    "deny_phrase",
    "deny_speech",
    "load_grammar",
    "load_pricing",
    "phrase",
    "prefix_hash",
    "tool_defs",
]
