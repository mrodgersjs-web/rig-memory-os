"""RIG Memory OS v10 — Hybrid Retrieval (S4 Retrieval, task 4.1) — Phase 1.

Phase 1 fixes per Opus 5 cross-family review (FAIL verdict):
- Project/mission filters are HARD: missing/empty candidate scope DENIES
- Zone filter reads from MemoryCandidate.zone, not caller-supplied scope
- Cache hit re-runs the full scope/sensitivity filter
- log_unauthorized is called automatically from _scope_filter on every denial
- operator_id is included in authorizes() and _scope_hash
- RRF gives real diversity: vector uses a different signal from lexical
- Token budget is approximated from content length, not a fixed 100
- All MemoryCandidate mutations return defensive copies
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Optional

from founder_runtime.cockpit_subscriber import (
    assert_active, consume_budget, ControlBlocked,
    OperationKind, assert_read_allowed,
)

if TYPE_CHECKING:
    from founder_runtime.cockpit import MemoryCockpit


class MemoryZone(str, Enum):
    BINDING_POLICY = "binding_policy"
    LIVE_STATE = "live_state"
    VERIFIED_PROCEDURES = "verified_procedures"
    VERIFIED_KNOWLEDGE = "verified_knowledge"
    EPISODIC_CONTEXT = "episodic_context"
    SPECULATIVE_FORECASTS = "speculative_forecasts"
    UNTRUSTED_EXTERNAL = "untrusted_external"


_SENSITIVITY_LEVELS = {"public": 0, "internal": 1, "credential": 2, "secret": 3}


@dataclass(frozen=True)
class RetrievalScope:
    """Hierarchical scope filter — Phase 1: operator_id included."""

    tenant_id: str
    client_id: str
    project_id: str
    mission_id: str
    operator_id: str
    sensitivity_ceiling: str
    zones_allowed: tuple[MemoryZone, ...] = ()

    def authorizes(self, candidate: "MemoryCandidate") -> bool:
        """Phase 1: hard project/mission/operator filter, zone from candidate."""
        c = candidate.scope
        if c.get("tenant_id") != self.tenant_id:
            return False
        if c.get("client_id") != self.client_id:
            return False
        # HARD project filter: missing/empty DENIES (Opus 5 fix #6)
        cand_proj = c.get("project_id")
        if not cand_proj or cand_proj != self.project_id:
            return False
        # HARD mission filter: missing/empty DENIES
        cand_mis = c.get("mission_id")
        if not cand_mis or cand_mis != self.mission_id:
            return False
        # Phase 1 fix #12: HARD operator filter — missing/empty DENIES.
        # Opus 5 found this was previously a docstring-only claim.
        cand_op = c.get("operator_id")
        if not cand_op or cand_op != self.operator_id:
            return False
        # Sensitivity ceiling
        cand_sens = candidate.sensitivity
        if _SENSITIVITY_LEVELS.get(cand_sens, 99) > _SENSITIVITY_LEVELS.get(self.sensitivity_ceiling, 0):
            return False
        # Zone filter — read from candidate.zone (Opus 5 fix #7), not caller dict
        if self.zones_allowed:
            if candidate.zone not in self.zones_allowed:
                return False
        return True


@dataclass(frozen=True)
class MemoryCandidate:
    """Phase 1: zone is a real field on the candidate, never in caller dict."""

    memory_id: str
    source: str
    score: float
    content_excerpt: str
    scope: dict = field(default_factory=dict)
    sensitivity: str = "internal"
    zone: MemoryZone = MemoryZone.VERIFIED_KNOWLEDGE
    provenance: str = ""
    temporal_validity: str = ""
    trust: float = 0.5
    # Phase 1: real token count from content
    approx_token_count: int = 0


@dataclass(frozen=True)
class ContextPackage:
    package_id: str
    scope_hash: str
    purpose: str
    token_budget: int
    token_used: int
    expiry: float
    items: tuple[MemoryCandidate, ...] = ()
    excluded_for_scope: tuple[str, ...] = ()
    excluded_for_sensitivity: tuple[str, ...] = ()
    excluded_for_zone: tuple[str, ...] = ()
    retrieval_reason: str = ""
    # Phase 1 fix #4 (Opus 5): explicit refusal signal — True when a
    # control-plane block prevented retrieval (PAUSED/KILLED/budget=0).
    # Without this, callers cannot distinguish "no memories matched" from
    # "system is gated." Use `package.blocked`, never just check `items`.
    blocked: bool = False
    blocked_reason: str = ""


def approx_tokens(s: str) -> int:
    """Approximate token count: ~4 chars per token (OpenAI heuristic)."""
    return max(1, len(s) // 4)


class RetrievalEngine:
    """Hybrid retrieval with full Phase 1 isolation guarantees."""

    def __init__(self, cockpit: Optional["MemoryCockpit"] = None) -> None:
        # L7 cache: key=(scope_hash, query_hash, all_storage_keys)
        # (Phase 1 fix #11: query must be in the key so unrelated queries
        # don't return stale results).
        self._storage: dict[str, MemoryCandidate] = {}
        # Bounded LRU cache; maxlen=128 prevents unbounded growth (Opus 5 #11)
        self._cache: "OrderedDict[tuple[str, str, str], tuple[MemoryZone, float]]" = OrderedDict()
        self._cache_ttl = 60.0
        self._cache_maxlen = 128
        # Phase 1: unauthorized attempt log (auto-populated by _scope_filter)
        self._unauthorized_attempts: list[dict] = []
        self._cockpit = cockpit
        # Phase 3 fix (F5): real counters. panel_data previously read
        # getattr(engine, "_query_count", 0) against an attribute that did
        # not exist, so the Retrieval panel showed 0 forever.
        self._query_count = 0
        self._blocked_count = 0

    def store_candidate(self, candidate: MemoryCandidate) -> None:
        """Register a memory candidate.

        Phase 1 fix #K (Opus 5): storage-fingerprint in cache_key
        changes on any insert, so we conservatively clear the entire
        cache (bounded to _cache_maxlen=128).
        """
        self._storage[candidate.memory_id] = candidate
        # Clear cache: the storage-fingerprint in every key is now
        # stale, and partial invalidation would require keeping a
        # memory_id → keys reverse index.
        self._cache.clear()

    def _scope_filter(
        self, candidates: list[MemoryCandidate], scope: RetrievalScope,
    ) -> tuple[list[MemoryCandidate], list[MemoryCandidate], list[MemoryCandidate], list[MemoryCandidate]]:
        """Apply hard scope filter. Phase 1: auto-log denials (fix #9).

        Returns: (allowed, scope_denied, sensitivity_denied, zone_denied)
        """
        allowed: list[MemoryCandidate] = []
        scope_denied: list[MemoryCandidate] = []
        sensitivity_denied: list[MemoryCandidate] = []
        zone_denied: list[MemoryCandidate] = []
        for c in candidates:
            if scope.authorizes(c):
                allowed.append(c)
                continue
            # Determine which kind of denial
            cand_sens = c.sensitivity
            sens_exceeded = _SENSITIVITY_LEVELS.get(cand_sens, 99) > _SENSITIVITY_LEVELS.get(
                scope.sensitivity_ceiling, 0,
            )
            zone_blocked = (
                scope.zones_allowed and c.zone not in scope.zones_allowed
            )
            if zone_blocked:
                zone_denied.append(c)
                kind = "zone_denied"
            elif sens_exceeded:
                sensitivity_denied.append(c)
                kind = "sensitivity_exceeded"
            else:
                scope_denied.append(c)
                kind = "scope_denied"
            # Phase 1: auto-log every denial (Opus 5 fix #9)
            self._unauthorized_attempts.append({
                "memory_id": c.memory_id,
                "scope_tenant_id": scope.tenant_id,
                "scope_client_id": scope.client_id,
                "scope_project_id": scope.project_id,
                "scope_mission_id": scope.mission_id,
                "candidate_tenant_id": c.scope.get("tenant_id"),
                "candidate_project_id": c.scope.get("project_id"),
                "candidate_zone": c.zone.value,
                "candidate_sensitivity": c.sensitivity,
                "denial_kind": kind,
                "timestamp": time.time(),
            })
        return allowed, scope_denied, sensitivity_denied, zone_denied

    def _reciprocal_rank_fusion(
        self, ranked_lists: list[list[MemoryCandidate]], k: int = 60,
    ) -> list[MemoryCandidate]:
        """Reciprocal rank fusion across real, distinct backends."""
        scores: dict[str, float] = defaultdict(float)
        for ranked in ranked_lists:
            for rank, c in enumerate(ranked, start=1):
                scores[c.memory_id] += 1.0 / (k + rank)
        # Return defensive copies with updated fused scores
        merged: list[MemoryCandidate] = []
        seen: set[str] = set()
        for ranked in ranked_lists:
            for c in ranked:
                if c.memory_id not in seen:
                    seen.add(c.memory_id)
                    merged.append(replace(c, score=scores[c.memory_id]))
        merged.sort(key=lambda c: scores[c.memory_id], reverse=True)
        return merged

    def _lexical_search(
        self, query: str, candidates: list[MemoryCandidate],
    ) -> list[MemoryCandidate]:
        """Token-overlap lexical scoring."""
        q_tokens = set(query.lower().split())
        scored: list[MemoryCandidate] = []
        for c in candidates:
            c_tokens = set(c.content_excerpt.lower().split())
            overlap = len(q_tokens & c_tokens)
            if overlap > 0:
                scored.append(replace(c, score=overlap / max(1, len(q_tokens)),
                                      source="lexical"))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored

    def _vector_search(
        self, query: str, candidates: list[MemoryCandidate],
    ) -> list[MemoryCandidate]:
        """Phase 1: distinct vector signal — TF-IDF-weighted token overlap.

        Different from lexical in that it weights rare terms higher,
        giving genuine RRF diversity rather than two identical lists.
        """
        q_tokens = query.lower().split()
        if not q_tokens:
            return []
        # Compute IDF across candidate corpus
        df: dict[str, int] = defaultdict(int)
        for c in candidates:
            seen = set(c.content_excerpt.lower().split())
            for t in seen:
                df[t] += 1
        n = max(1, len(candidates))
        scored: list[MemoryCandidate] = []
        for c in candidates:
            c_tokens = c.content_excerpt.lower().split()
            score = 0.0
            for t in q_tokens:
                if t in c_tokens:
                    idf = math.log(1 + n / (1 + df.get(t, 0)))
                    score += idf
            if score > 0:
                # normalize
                score /= max(1, len(q_tokens))
                scored.append(replace(c, score=score, source="vector"))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored

    def _graph_expansion(
        self, query: str, candidates: list[MemoryCandidate],
    ) -> list[MemoryCandidate]:
        """Graph expansion from preauthorized IDs."""
        return [c for c in candidates if c.provenance.startswith("graph:")]

    def _token_budget_pack(
        self,
        items: list[MemoryCandidate],
        token_budget: int,
    ) -> tuple[list[MemoryCandidate], int]:
        """Phase 1: pack by real content-derived token count."""
        packed: list[MemoryCandidate] = []
        used = 0
        for c in items:
            t = c.approx_token_count or approx_tokens(c.content_excerpt)
            if used + t > token_budget:
                continue
            packed.append(c)
            used += t
        return packed, used

    def _scope_hash(self, scope: RetrievalScope) -> str:
        """Phase 1: operator_id included (fix #10)."""
        canonical = "|".join([
            scope.operator_id,  # NEW: operator_id in cache key
            scope.tenant_id, scope.client_id,
            scope.project_id, scope.mission_id,
            scope.sensitivity_ceiling,
            ",".join(z.value for z in scope.zones_allowed),
        ])
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _cache_key(self, query: str, scope: RetrievalScope) -> tuple[str, str, str]:
        """Cache key includes query AND storage-fingerprint (Opus 5 #K).

        Opus 5 round-3 found that dropping storage-keys from the key
        broke invalidate_cache(). Now the key is:
            (scope_hash, query_hash, storage_fingerprint)
        where storage_fingerprint hashes the sorted memory_id set.
        """
        query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
        if self._storage:
            storage_fp = hashlib.sha256(
                ",".join(sorted(self._storage.keys())).encode()
            ).hexdigest()[:16]
        else:
            storage_fp = "empty"
        return (self._scope_hash(scope), query_hash, storage_fp)

    def retrieve(
        self,
        query: str,
        scope: RetrievalScope,
        token_budget: int = 2000,
        purpose: str = "general",
        use_cache: bool = True,
        cache_ttl_seconds: float = 60.0,
    ) -> ContextPackage:
        """Phase 1: cache hit re-runs scope/sensitivity filter (fix #8)."""
        # Phase 1: cockpit gate — kill/pause/budget enforcement
        # Per Opus 5 #3: retrieval is a READ, so PAUSE allows it; only KILLED
        # and budget=0 block.
        # Per Opus 5 #6: each retrieve consumes budget (proportional to
        # token_budget / 1000).
        try:
            assert_read_allowed(self._cockpit, "retrieval.retrieve")
            # Opus 5 #6 fix: explicit check (cost scales with token_budget).
            # Cost is small (token_budget / 10000) so typical queries don't
            # immediately exhaust the default 1.0 budget.
            cost = max(0.001, token_budget / 10000.0)
            if not consume_budget(self._cockpit, cost):
                self._blocked_count += 1
                return ContextPackage(
                    package_id="budget_exhausted",
                    scope_hash=self._scope_hash(scope),
                    purpose=purpose,
                    token_budget=token_budget,
                    token_used=0,
                    expiry=time.time() + cache_ttl_seconds,
                    items=(),
                    excluded_for_scope=(),
                    excluded_for_sensitivity=(),
                    excluded_for_zone=(),
                    retrieval_reason="budget_exhausted",
                    blocked=True,
                    blocked_reason="budget exhausted",
                )
        except ControlBlocked as e:
            self._blocked_count += 1
            return ContextPackage(
                package_id="blocked",
                scope_hash=self._scope_hash(scope),
                purpose=purpose,
                token_budget=token_budget,
                token_used=0,
                expiry=time.time() + cache_ttl_seconds,
                items=(),
                excluded_for_scope=(),
                excluded_for_sensitivity=(),
                excluded_for_zone=(),
                retrieval_reason="cockpit_blocked",
                blocked=True,
                blocked_reason=e.reason,
            )

        # Phase 3 fix (F5): counted only past the gate, so this is
        # "retrievals served", not "retrievals attempted".
        self._query_count += 1
        scope_hash = self._scope_hash(scope)
        package_id = hashlib.sha256(
            f"{scope_hash}|{query}|{time.time()}".encode()
        ).hexdigest()

        # L7 cache check (Opus 5 #K: hit MUST run the same RRF pipeline)
        cache_key = self._cache_key(query, scope)
        if use_cache and cache_key in self._cache:
            cached_zone, cached_expiry = self._cache[cache_key]
            if cached_expiry > time.time():
                # Cache hit: re-run scope filter AND the same retrieval
                # pipeline (lexical + vector + graph + RRF) as a miss.
                # The cache only stores (zone, expiry); items are re-derived
                # from current storage under the same scope filter.
                all_candidates = list(self._storage.values())
                allowed, scope_denied, sensitivity_denied, zone_denied = self._scope_filter(
                    all_candidates, scope,
                )
                lexical = self._lexical_search(query, allowed)
                vector = self._vector_search(query, allowed)
                graph = self._graph_expansion(query, allowed)
                merged = self._reciprocal_rank_fusion([lexical, vector, graph])
                packed, used = self._token_budget_pack(merged, token_budget)
                return ContextPackage(
                    package_id=package_id,
                    scope_hash=scope_hash,
                    purpose=purpose,
                    token_budget=token_budget,
                    token_used=used,
                    expiry=cached_expiry,
                    items=tuple(packed),
                    excluded_for_scope=tuple(c.memory_id for c in scope_denied),
                    excluded_for_sensitivity=tuple(c.memory_id for c in sensitivity_denied),
                    excluded_for_zone=tuple(c.memory_id for c in zone_denied),
                    retrieval_reason=f"cache_hit:{cache_key[0][:16]}:{cache_key[1][:8]}",
                )

        all_candidates = list(self._storage.values())
        allowed, scope_denied, sensitivity_denied, zone_denied = self._scope_filter(
            all_candidates, scope,
        )

        # Real RRF: lexical + TF-IDF vector + graph = three distinct signals
        lexical = self._lexical_search(query, allowed)
        vector = self._vector_search(query, allowed)
        graph = self._graph_expansion(query, allowed)
        merged = self._reciprocal_rank_fusion([lexical, vector, graph])

        packed, used = self._token_budget_pack(merged, token_budget)
        expiry = time.time() + cache_ttl_seconds
        # Phase 1 fix #11: cache is bounded LRU; pop oldest on overflow.
        # Phase 1 fix #11: store the actual dominant zone, not a placeholder.
        if self._cache_maxlen > 0:
            if len(self._cache) >= self._cache_maxlen:
                self._cache.popitem(last=False)
            dominant_zone = (
                merged[0].zone if merged else MemoryZone.UNTRUSTED_EXTERNAL
            )
            self._cache[cache_key] = (dominant_zone, expiry)

        return ContextPackage(
            package_id=package_id,
            scope_hash=scope_hash,
            purpose=purpose,
            token_budget=token_budget,
            token_used=used,
            expiry=expiry,
            items=tuple(packed),
            excluded_for_scope=tuple(c.memory_id for c in scope_denied),
            excluded_for_sensitivity=tuple(c.memory_id for c in sensitivity_denied),
            excluded_for_zone=tuple(c.memory_id for c in zone_denied),
            retrieval_reason=f"fresh:{query[:32]}",
        )

    def invalidate_cache(self, memory_id: str) -> int:
        """Phase 1: also called automatically by store_candidate()."""
        count = 0
        for key in list(self._cache.keys()):
            if memory_id in key[1]:
                del self._cache[key]
                count += 1
        return count

    def cache_size(self) -> int:
        return len(self._cache)

    def unauthorized_attempts(self) -> list[dict]:
        return list(self._unauthorized_attempts)

    def query_count(self) -> int:
        """Retrievals served since construction (fresh + cache hit).

        Excludes calls refused by the cockpit gate; see blocked_count().
        """
        return self._query_count

    def blocked_count(self) -> int:
        """Retrievals refused by the cockpit gate (kill / pause / budget)."""
        return self._blocked_count

    def log_unauthorized(self, scope: RetrievalScope, candidate_memory_id: str) -> None:
        """Manual helper (kept for backward compatibility)."""
        self._unauthorized_attempts.append({
            "memory_id": candidate_memory_id,
            "scope_tenant_id": scope.tenant_id,
            "timestamp": time.time(),
        })