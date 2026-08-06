"""Override miner — groups Jake override events by detector, drafts reweight proposals (never auto-applied)."""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path.home() / '.rig' / 'state' / 'jake-overrides.jsonl'
OUT_PATH = Path.home() / '.rig' / 'state' / 'override-proposals.json'


def load_events():
    """Load override events from LOG_PATH, skipping corrupt lines silently.

    Returns an empty list if LOG_PATH does not exist. Each event is a dict
    with keys: ts, target_path, session_id, reason, detector_id, outcome.
    """
    if not LOG_PATH.exists():
        return []

    events = []
    with LOG_PATH.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return events


def mine(events, threshold=2):
    """Group events by detector_id and draft reweight proposals.

    Events missing a detector_id are grouped under 'unknown'. For each
    detector, counts total, wrong_rule, expedient, and unknown (all other
    reasons) events. Detectors with wrong_rule count >= threshold get a
    pending-review reweight proposal (never auto-applied).

    Returns a tuple: (detectors: dict, proposals: list).
    """
    grouped = defaultdict(list)
    for event in events:
        detector_id = event.get('detector_id') or 'unknown'
        grouped[detector_id].append(event)

    detectors = {}
    proposals = []

    for detector_id, detector_events in grouped.items():
        wrong_rule_events = [e for e in detector_events if e.get('reason') == 'wrong_rule']
        expedient_events = [e for e in detector_events if e.get('reason') == 'expedient']
        unknown_events = [
            e for e in detector_events
            if e.get('reason') not in ('wrong_rule', 'expedient')
        ]

        total = len(detector_events)
        wrong_rule = len(wrong_rule_events)
        expedient = len(expedient_events)
        unknown = len(unknown_events)

        detectors[detector_id] = {
            'total': total,
            'wrong_rule': wrong_rule,
            'expedient': expedient,
            'unknown': unknown,
        }

        if wrong_rule >= threshold:
            proposals.append({
                'detector_id': detector_id,
                'version': wrong_rule,
                'evidence': wrong_rule_events[:10],
                'proposal': 'review trigger thresholds',
                'status': 'pending_review',
                'created': datetime.now(timezone.utc).isoformat(),
            })

    return detectors, proposals


def _atomic_write(path, data):
    """Write JSON data to path atomically via a per-PID tmp file + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'{path.name}.tmp.{os.getpid()}')
    with tmp_path.open('w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(tmp_path, path)


def main():
    """Load events, mine proposals, atomically write results, and report."""
    events = load_events()
    detectors, proposals = mine(events)

    output = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'detectors': detectors,
        'proposals': proposals,
    }

    _atomic_write(OUT_PATH, output)

    proposal_detector_ids = {p['detector_id'] for p in proposals}
    for detector_id, counts in detectors.items():
        has_proposal = 'yes' if detector_id in proposal_detector_ids else 'no'
        print(
            f"{detector_id} | blocks={counts['total']} "
            f"wrong_rule={counts['wrong_rule']} expedient={counts['expedient']} "
            f"proposal={has_proposal}"
        )

    return 0


if __name__ == '__main__':
    sys.exit(main())
