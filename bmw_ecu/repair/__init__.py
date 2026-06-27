"""AI Repair Assistant — a self-verifying repair-plan generator.

A curated knowledge base (knowledge.py) grounds a Generator+Verifier brain
(brain.py); the assistant orchestrator (assistant.py) fuses their two
confidence signals and a single threshold decides what is auto-accepted vs
flagged for review. Hardware-/LLM-free in tests and gated by the saleable
feature 'ai_repair_assistant'.
"""
from __future__ import annotations

from .assistant import (
    AI_REPAIR_FEATURE,
    DEFAULT_THRESHOLD,
    AssistantData,
    AssistantEvent,
    AssistantPrompt,
    AssistantState,
    IllegalAssistantTransition,
    Recommendation,
    RepairAssistantOrchestrator,
    RepairPlan,
    combine_confidence,
)
from .brain import (
    AbstractRepairBrain,
    Critique,
    Hypothesis,
    MockRepairBrain,
    RepairCase,
)
from .knowledge import (
    KNOWLEDGE_BASE,
    RepairEntry,
    all_entries,
    entries_for_dtc,
    entries_for_symptom,
    get_entry,
)

__all__ = [
    # knowledge
    "KNOWLEDGE_BASE", "RepairEntry", "all_entries", "entries_for_dtc",
    "entries_for_symptom", "get_entry",
    # brain
    "AbstractRepairBrain", "Critique", "Hypothesis", "MockRepairBrain",
    "RepairCase",
    # assistant
    "AI_REPAIR_FEATURE", "DEFAULT_THRESHOLD", "AssistantData",
    "AssistantEvent", "AssistantPrompt", "AssistantState",
    "IllegalAssistantTransition", "Recommendation",
    "RepairAssistantOrchestrator", "RepairPlan", "combine_confidence",
]
