"""Shared runtime for rustdesk automation skills (extracted from jianhui).

Modules:
    vision    — Apple Vision OCR, region zoom OCR, green-label fusion,
                battle anchors, text-hit helper
    artifacts — atomic state.json, evidence shot names, JSONL loggers
    session   — MCP stdio session lifecycle (spec loading, keep-alive lease)
    humanize  — human-like click/scroll planning (jitter, no-repeat, delays)
    workflow  — declarative Screen signatures + workflow Node engine
    mock      — scenario-driven offline MCP mock for end-to-end tests
"""
