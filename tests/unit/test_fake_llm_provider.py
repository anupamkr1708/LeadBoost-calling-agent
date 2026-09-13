"""Mechanically proves intelligence/fake_llm.py's own docstring promise:
`FakeLLMProvider` never inspects transcript/text content to decide what
to return. This is checked by parsing the module's AST and asserting no
attribute access on `.transcript` (or any string containment check
against it) exists anywhere in the file — not by convention, and not by
just re-reading the docstring, per master prompt §19 and §42's "do not
let 'all tests pass' actually mean 'the API key happened to work'" (the
adjacent, equally real risk for a fake provider: "the fake happened to
return the right fixture because it silently classified the input").
"""
from __future__ import annotations

import ast
import inspect

import intelligence.fake_llm as fake_llm_module


def test_fake_llm_provider_source_never_inspects_transcript_content():
    source = inspect.getsource(fake_llm_module)
    tree = ast.parse(source)

    suspicious_names = {"transcript", "text", "speaker"}
    violations: list[str] = []

    for node in ast.walk(tree):
        # `in`/`not in` comparisons against string literals are the
        # textbook keyword-matching pattern the spec prohibits
        # ("if 'Salesforce' in text:") — flag any Compare node using
        # `In`/`NotIn` anywhere in this file at all, full stop.
        if isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            violations.append(f"line {node.lineno}: 'in'/'not in' comparison found (possible keyword matching)")
        # Any attribute access matching a suspicious name (turn_input.transcript,
        # turn_input.speaker, etc.) flags for manual review — the ONLY
        # legitimate uses in this file are passing turn_input through
        # unexamined to a caller-supplied source function, never reading
        # a field off it directly within fake_llm.py's own logic.
        if isinstance(node, ast.Attribute) and node.attr in suspicious_names:
            violations.append(f"line {node.lineno}: attribute access '.{node.attr}' found")

    assert not violations, (
        "intelligence/fake_llm.py must never inspect transcript/text content — "
        "found suspicious constructs:\n" + "\n".join(violations)
    )


def test_fake_llm_provider_default_sources_ignore_their_input_entirely():
    """The strongest possible proof for the default (unconfigured) case:
    the SAME default result comes back regardless of what's asked —
    demonstrated empirically, not just by reading the code."""
    import asyncio
    import uuid

    from intelligence.contracts import ConversationInput, Speaker
    from intelligence.fake_llm import FakeLLMProvider

    provider = FakeLLMProvider()

    async def _interpret_with(text: str):
        turn_input = ConversationInput(
            session_id=uuid.uuid4(),
            call_attempt_id=uuid.uuid4(),
            organization_id=1,
            lead_id=1,
            turn_number=1,
            speaker=Speaker.PROSPECT,
            transcript=text,
            prior_state=None,
            recent_turns=(),
            objective="book_meeting",
            context_version=1,
        )
        return await provider.interpret(turn_input)

    async def _run():
        r1 = await _interpret_with("We already use Salesforce and love it")
        r2 = await _interpret_with("Get lost, never call again")
        return r1, r2

    r1, r2 = asyncio.run(_run())
    assert r1.interpretation == r2.interpretation, (
        "the default (unconfigured) FakeLLMProvider must return the identical "
        "'unknown' result regardless of transcript content — any difference "
        "here would mean it's silently classifying text"
    )
