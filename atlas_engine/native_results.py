"""Small, allowlisted snapshots of native DTOs, not a workflow result compiler."""

MAX_RESULTS = 64
MAX_ID_BYTES = 256


def identifier(value):
    return isinstance(value, str) and len(value) <= MAX_ID_BYTES and len(value.encode('utf-8')) <= MAX_ID_BYTES


def snapshot(session, allowed_nodes):
    """Expose only exact submitted source IDs. Never copy opaque DTO contents.

    integer_output and boolean_output match the pinned native primitive DTOs.
    Other DTOs have an explicit unsupported marker, without values/paths/blobs.
    """
    results = session.get('results') or {}
    mapping = session.get('prepared_source_mapping') or {}
    entries = []
    unscoped = 0
    for node_id, result in results.items():
        source_id = mapping.get(node_id, node_id)
        if not identifier(node_id) or not identifier(source_id) or source_id not in allowed_nodes:
            unscoped += 1
            continue
        if len(entries) == MAX_RESULTS:
            continue
        kind = result.get('type') if isinstance(result, dict) else None
        entry = {'node_id': node_id, 'source_node_id': source_id,
                 'type': kind if identifier(kind) else None, 'status': 'unsupported_type'}
        if kind in ('integer_output', 'boolean_output'):
            value = result.get('value')
            if kind == 'integer_output' and type(value) is int:
                if -(2**63) <= value < 2**63:
                    entry.update(status='available', value=value)
                else:
                    entry['status'] = 'value_out_of_range'
            elif kind == 'boolean_output' and type(value) is bool:
                entry.update(status='available', value=value)
            else:
                entry['status'] = 'invalid_value'
        entries.append(entry)
    return {'entries': entries, 'total': len(results),
            'omitted': len(results) - len(entries), 'unscoped': unscoped,
            'truncated': len(results) - unscoped > MAX_RESULTS,
            'max_entries': MAX_RESULTS}
