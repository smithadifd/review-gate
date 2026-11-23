"""Follow a carried finding to the finding that first raised it, over stored receipt events alone.

A stored finding is only ``id``, ``cell`` and ``carried_from`` (the event id of
the receipt it was carried from), so the walk reads nothing else: the root of a
finding id is the text after its last ``#``, and the entries of an event on a
root are its findings whose id has that root. The document facts a runner
records (moved lines, residual links, descriptions) are not in the event and are
never read.

A carried finding names one event. Every entry of that event on the finding's
root is a branch: an entry with no ``carried_from`` ends its branch, and its id
is that branch's origin; an entry with one is followed the same way. The walk
accepts only when every branch ends and all of them end at one origin id (the
same id reached in two events is one origin). It never picks one entry over
another, by cell, line or anything else: when the branches agree there is
nothing to pick, and when they do not it refuses. So every entry on one root
shares one origin, and with it one waiver id and one hardening source.

Refusals, all ``receipt_carried_root_missing``: an event that holds no entry on
the root, or a ``carried_from`` that names no event of ``events`` (the detail is
the id of the finding that names it); a cycle (``carried finding cycle``); and
branches that end at more than one origin (the id of the finding that names the
event, then ``: root matches several entries that do not reach one origin``).

The root is the same along the whole walk, so each event is resolved once and
the walk is linear in events.
"""
from __future__ import annotations

from .refusal import refuse

MISSING = "receipt_carried_root_missing"
CYCLE = "carried finding cycle"
SEVERAL = "root matches several entries that do not reach one origin"


def root_of(finding_id):
    return finding_id.rsplit("#", 1)[1]


def entries_on_root(event, root):
    return [f for f in event.payload.get("findings", ()) if root_of(f["id"]) == root]


def carried_origin(finding, events):
    """``(origin id, visited events)`` for ``finding``, which has ``carried_from``.

    ``events`` are the item's stored receipts; only ``id`` and ``payload`` are
    read, so projected events serve as well as ledger events. Visited events
    are the distinct events the walk entered, by event id, newest first: on a
    chain with one entry per event that is each hop in order.
    """
    root = root_of(finding["id"])
    by_id = {event.id: event for event in events}
    reached = {}  # event id -> the one origin every branch from it ends at
    active = set()  # event ids on the path being walked
    # A frame: (the finding that names the event, the event id, its entries left, origins so far).
    top = (None, None, iter([finding]), [])
    stack = [top]
    while stack:
        referrer, target, entries, origins = stack[-1]
        entry = next(entries, None)
        if entry is None:
            stack.pop()
            if target is None:
                break
            active.discard(target)
            if len(set(origins)) != 1:
                refuse(MISSING, f"{referrer['id']}: {SEVERAL}")
            reached[target] = origins[0]
            stack[-1][3].append(origins[0])
            continue
        if "carried_from" not in entry:
            origins.append(entry["id"])
            continue
        ahead = entry["carried_from"]
        if ahead in reached:
            origins.append(reached[ahead])
            continue
        if ahead in active:
            refuse(MISSING, CYCLE)
        source = by_id.get(ahead)
        on_root = [] if source is None else entries_on_root(source, root)
        if not on_root:
            refuse(MISSING, entry["id"])
        active.add(ahead)
        stack.append((entry, ahead, iter(on_root), []))
    visited = sorted((by_id[event_id] for event_id in reached), key=lambda event: event.id, reverse=True)
    return top[3][0], tuple(visited)
