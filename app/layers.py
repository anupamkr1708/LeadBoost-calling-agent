"""The layer graph, as DATA — not enforced by convention, enforced by
`tests/layering/test_import_boundaries.py`, which parses the AST of every
module in this repo and fails CI if an import violates this graph.

Mirrors the technique in both source repos (Calling-Agent-'s foundation/layers
and LeadBoost-saas's api -> application -> core rule). See PRD/TRD Part 8's
table and roadmap Part G's "Architecture layering" row.

Rules encoded here (expanded as later phases add packages):

1. Vendor SDK confinement: ONLY `telephony/exotel/**` may import an Exotel
   SDK/socket module, and ONLY `telephony/deepgram/**` may import a Deepgram
   client. No other package may import either directly — they may only
   depend on `telephony.contracts` (the Protocol definitions).
2. Embedding-client confinement: ONLY `retrieval/**` may import an embedding
   client library. (No embedding client is wired yet in Phase 0; this rule
   exists now so Phase 3 can't accidentally violate it.)
3. `storage/**` owns the ORM models and the only DB session factory. No
   other package may import a raw `sqlalchemy.orm.Session` constructor or
   open its own engine — everyone goes through `storage.db`.
4. `app/**` (composition root) may import anything. Nothing may import
   `app.main` (no cycles back into the entrypoint).
5. (Phase 1) Dependency direction within the runtime is strictly
   orchestrator -> conversation -> telephony (docs/PHASE1_DESIGN.md
   "New packages, and why they land where they do"). `conversation/` must
   never import `orchestrator/` — the runtime calls conversation, never
   the reverse, which is what keeps conversation/runtime.py a clean seam
   for Phase 2 to replace without touching orchestrator/ at all.
6. (Phase 1) `orchestrator/` must never import `fastapi` — the runtime is
   framework-agnostic by construction, so it stays usable from something
   other than this specific FastAPI process (e.g. a standalone worker
   binary) without modification, should a later phase split it out.
7. (Phase 1 hardening) `storage.db.system_session` — the narrow,
   cross-organization role used by the worker runtime's reaper/
   reconciliation sweeps (docs/PHASE1_DESIGN.md "Concurrency / worker
   acquisition") — may only be imported from `orchestrator/**`. Every
   other module, especially `api/**` (which is always request-scoped to
   one authenticated caller's org), must go through `org_scoped_session`
   like everything else. This is deliberately a symbol-level rule, not
   just a module-level one: `storage.db` as a whole is fine for anyone to
   import (`get_session`, `org_scoped_session`), it's specifically this
   one cross-org-capable function that's restricted.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VendorConfinementRule:
    """A vendor/library import that is confined to one or more allowed
    package prefixes; any other importer is a layering violation."""

    forbidden_import_prefixes: tuple[str, ...]
    allowed_importer_prefixes: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class DependencyBanRule:
    """A general "module X may never import module Y" rule — for internal
    layering constraints that aren't about vendor SDK confinement (rule
    types 5 and 6 above)."""

    banned_import_prefixes: tuple[str, ...]
    forbidden_importer_prefixes: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SymbolConfinementRule:
    """A specific `from module import symbol` that is confined to one or
    more allowed importer prefixes — for restricting one function/class
    within an otherwise-unrestricted module (rule type 7 above)."""

    module: str
    symbol: str
    allowed_importer_prefixes: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class LayerGraph:
    vendor_confinement: tuple[VendorConfinementRule, ...] = field(default_factory=tuple)
    dependency_bans: tuple[DependencyBanRule, ...] = field(default_factory=tuple)
    symbol_confinement: tuple[SymbolConfinementRule, ...] = field(default_factory=tuple)
    # Packages that may never be imported by anything except the composition root.
    entrypoint_only_modules: tuple[str, ...] = field(default_factory=tuple)


LAYER_GRAPH = LayerGraph(
    vendor_confinement=(
        VendorConfinementRule(
            forbidden_import_prefixes=("exotel",),
            allowed_importer_prefixes=("telephony.exotel",),
            reason=(
                "Exotel SDK/socket libraries must be confined to "
                "telephony/exotel/ — this is the single vendor-audio "
                "adapter boundary (roadmap Part D.1 rule 5 / Part E.1)."
            ),
        ),
        VendorConfinementRule(
            forbidden_import_prefixes=("deepgram",),
            allowed_importer_prefixes=("telephony.deepgram",),
            reason="Deepgram client must be confined to telephony/deepgram/.",
        ),
        VendorConfinementRule(
            forbidden_import_prefixes=("groq",),
            allowed_importer_prefixes=("conversation.llm_client",),
            reason="Groq client must be confined to conversation/llm_client.py.",
        ),
    ),
    dependency_bans=(
        DependencyBanRule(
            banned_import_prefixes=("orchestrator",),
            forbidden_importer_prefixes=("conversation",),
            reason=(
                "conversation/ must not import orchestrator/ — dependency "
                "direction is orchestrator -> conversation -> telephony "
                "(docs/PHASE1_DESIGN.md), never the reverse. This was a "
                "real bug caught during Phase 1 implementation (an early "
                "draft of conversation/runtime.py imported FailureCategory "
                "from orchestrator.failures) — see telephony/contracts.py's "
                "docstring for where that type actually lives now."
            ),
        ),
        DependencyBanRule(
            banned_import_prefixes=("fastapi",),
            forbidden_importer_prefixes=("orchestrator",),
            reason=(
                "orchestrator/ must stay framework-agnostic — it's the "
                "execution runtime, not the web layer, and should remain "
                "usable without FastAPI in the loop."
            ),
        ),
    ),
    entrypoint_only_modules=("app.main",),
    symbol_confinement=(
        SymbolConfinementRule(
            module="storage.db",
            symbol="system_session",
            allowed_importer_prefixes=("orchestrator",),
            reason=(
                "system_session is the narrow cross-organization role — "
                "only orchestrator/ (the worker runtime's reaper/"
                "reconciliation sweeps) has a legitimate reason to look up "
                "data before knowing which org it belongs to. api/ and "
                "everything else must always be scoped to the "
                "authenticated caller's own org via org_scoped_session."
            ),
        ),
    ),
)
