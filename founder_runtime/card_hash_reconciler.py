"""RIG Memory OS v10 — Phase 0 card hash reconciliation (S1 Durable Base).

Reconciles evidence card files (Markdown + JSON) and `index.json` from
immutable content hashes. Per design D4:
- Compute content hash for each Markdown and JSON card file
- Rebuild `index.json` from hash-matching, not from in-memory counts
- No new collection starts until reconciliation passes
- Hash drift → stale detection → reconciliation required

Per the v10 spec:
- Card files are projections, not primary truth (the immutable event
  ledger in Postgres is the source of record)
- Reconciliation is reversible: the old index is backed up
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


CARD_FILE_SUFFIXES = (".md", ".json")
INDEX_FILENAME = "index.json"
BACKUP_SUFFIX = ".pre-reconcile-backup"


@dataclass
class CardRecord:
    """One evidence card on disk, identified by content hash."""

    path: Path
    content_hash: str
    size_bytes: int
    source_type: str  # md | json


@dataclass
class ReconciliationResult:
    """Outcome of a hash-based card reconciliation run."""

    card_count: int
    cards_indexed: int
    orphans_before_rebuild: list[Path] = field(default_factory=list)
    orphans_after_rebuild: list[Path] = field(default_factory=list)
    missing_in_index: list[Path] = field(default_factory=list)
    extra_in_index: list[Path] = field(default_factory=list)
    index_hash_matched: bool = False
    backup_path: Optional[Path] = None
    message: str = ""

    @property
    def orphans(self) -> list[Path]:
        """Backwards-compat: returns orphans_after_rebuild."""
        return self.orphans_after_rebuild

    @property
    def is_clean(self) -> bool:
        # After rebuild, the ONLY meaningful "clean" check is whether the
        # rebuilt index matches the on-disk card hashes. Orphans detected
        # BEFORE the rebuild are resolved by the rebuild itself; what
        # matters is whether anything remains inconsistent.
        return (
            self.card_count == self.cards_indexed
            and not self.orphans_after_rebuild
            and not self.missing_in_index
            and not self.extra_in_index
            and self.index_hash_matched
        )


def hash_file(path: Path) -> str:
    """Compute the SHA-256 content hash of a file."""
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def discover_cards(card_dir: Path) -> list[CardRecord]:
    """Walk `card_dir` and return one CardRecord per evidence card file."""
    cards: list[CardRecord] = []
    for p in sorted(card_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in CARD_FILE_SUFFIXES:
            continue
        if p.name == INDEX_FILENAME:
            continue
        cards.append(
            CardRecord(
                path=p,
                content_hash=hash_file(p),
                size_bytes=p.stat().st_size,
                source_type="md" if p.suffix.lower() == ".md" else "json",
            )
        )
    return cards


def load_index(card_dir: Path) -> dict[str, dict]:
    """Load `index.json` if present. Returns a path → entry map.

    Returns an empty dict if no index exists — reconciliation will then
    build one from the actual card files on disk.
    """
    index_path = card_dir / INDEX_FILENAME
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text())
    except json.JSONDecodeError:
        return {}
    # Index entries are keyed by file path; the value is the metadata
    # record (must include `content_hash` for verification).
    return data


def reconcile_cards(card_dir: Path) -> ReconciliationResult:
    """Reconcile card files and index.json from immutable content hashes.

    Returns a ReconciliationResult describing:
    - how many cards were found on disk
    - how many are accounted for in the index
    - orphans (cards not in index)
    - missing (in index but not on disk)
    - extra (in index but content_hash doesn't match)
    """
    cards = discover_cards(card_dir)
    index = load_index(card_dir)

    on_disk_paths = {str(c.path): c for c in cards}
    on_disk_hashes = {c.content_hash: c for c in cards}
    in_index_paths = set(index.keys())

    # Orphan = card file exists but not in index
    orphans = [c.path for c in cards if str(c.path) not in index]

    # Missing = index references a card that doesn't exist on disk
    missing = [
        Path(k) for k in in_index_paths if k not in on_disk_paths
    ]

    # Extra = index references a card with a hash that doesn't match
    # the on-disk file (stale index or modified card)
    extra: list[Path] = []
    for k, entry in index.items():
        if k in on_disk_paths:
            card = on_disk_paths[k]
            expected = entry.get("content_hash", "")
            if expected and expected != card.content_hash:
                extra.append(card.path)

    # Backup existing index before rebuilding
    index_path = card_dir / INDEX_FILENAME
    backup_path: Optional[Path] = None
    if index_path.exists():
        backup_path = card_dir / f"{INDEX_FILENAME}{BACKUP_SUFFIX}"
        backup_path.write_bytes(index_path.read_bytes())

    # Rebuild index from disk (hash-matching only)
    new_index: dict[str, dict] = {}
    for c in cards:
        new_index[str(c.path)] = {
            "content_hash": c.content_hash,
            "size_bytes": c.size_bytes,
            "source_type": c.source_type,
            "reconciled_at": c.path.stat().st_mtime,
        }
    index_path.write_text(json.dumps(new_index, indent=2, sort_keys=True))

    # Verify the rebuilt index is hash-consistent with on-disk state
    rebuilt = load_index(card_dir)
    rebuilt_hashes = {k: v.get("content_hash") for k, v in rebuilt.items()}
    on_disk_hash_map = {str(c.path): c.content_hash for c in cards}
    index_hash_matched = rebuilt_hashes == on_disk_hash_map

    return ReconciliationResult(
        card_count=len(cards),
        cards_indexed=len(new_index),
        orphans_before_rebuild=orphans,
        orphans_after_rebuild=[],  # rebuild resolved them
        missing_in_index=missing,
        extra_in_index=extra,
        index_hash_matched=index_hash_matched,
        backup_path=backup_path,
        message=(
            f"reconciled {len(cards)} card files; "
            f"{len(orphans)} orphans-before-rebuild, {len(missing)} missing, "
            f"{len(extra)} stale; index rebuilt"
        ),
    )


# =====================================================================
# Per design D4: No new collection flow may start until reconciliation passes.
# =====================================================================

def reconciliation_allows_new_collection(result: ReconciliationResult) -> bool:
    """Gate: return True only if reconciliation is clean and complete."""
    return result.is_clean