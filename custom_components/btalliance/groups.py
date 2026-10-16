"""Short Telink mesh group addresses and conservative member-state aggregation."""

from __future__ import annotations

import time
from typing import Any


def validate_group_address(value: int) -> int:
    """Support short group IDs, excluding zero and the FF terminator."""
    address = int(value)
    if not 0x8001 <= address <= 0x80FE:
        raise ValueError("Group address must be between 32769 and 33022")
    return address


def aggregate_group_state(members: set[int], states: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Unknown/offline/stale members cannot establish a uniform group state."""
    now = time.time()
    known = [states[address] for address in sorted(members)
             if address in states and states[address].get('online') is not False
             and now - states[address].get('last_seen', 0) <= 180]
    complete = bool(members) and len(known) == len(members)
    on_values = [state.get('is_on') for state in known]
    result: dict[str, Any] = {
        'is_on': True if True in on_values else False if complete and all(value is False for value in on_values) else None,
        'state_complete': complete,
        'known_member_count': len(known),
        'mixed_state': len(set(on_values)) > 1,
    }
    if complete:
        for key in ('luminance', 'color_mode', 'red', 'green', 'blue', 'color_temp', 'warm', 'cool'):
            values = {state.get(key) for state in known}
            if len(values) == 1 and None not in values:
                result[key] = next(iter(values))
            elif len(values) > 1:
                result['mixed_state'] = True
    return result
