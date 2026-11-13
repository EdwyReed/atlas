"""One bounded native nearest-resize recipe, not a Canvas/DTO compiler."""
from urllib.parse import quote
from .contracts import Fault, WorkflowImage

RECIPE = 'native-image-resize-v1'


def image_name(value):
    if not isinstance(value, dict) or set(value) != {'image_name'}:
        return None
    name = value['image_name']
    if (not isinstance(name, str) or not name or len(name.encode('utf-8')) > 200 or
            any(ord(c) < 32 or c in '/\\' for c in name)):
        return None
    return name


def recipe(nodes, edges, *, native=False):
    """Validate only the fixed three-node topology, preserving native authority."""
    kinds = {kind: [nid for nid, n in nodes.items() if n.get('type') == kind]
             for kind in ('img_resize', 'workflow_return_value', 'workflow_return')}
    if len(nodes) != 3 or any(len(v) != 1 for v in kinds.values()):
        raise Fault('WORKFLOW_NODE_UNSUPPORTED', 'IMAGE recipe requires resize, named value and return only')
    resize, rv, ret = (kinds[k][0] for k in kinds)
    def value(nid, field):
        n = nodes[nid]
        return n.get(field) if native else n.get('inputs', {}).get(field, {}).get('value')
    if any(type(value(resize, k)) is not int or not 1 <= value(resize, k) <= 2048 for k in ('width', 'height')) or value(resize, 'resample_mode') != 'nearest':
        raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'IMAGE recipe requires literal dimensions 1..2048 and nearest resampling')
    if native:
        links = [(e.get('source', {}).get('node_id'), e.get('source', {}).get('field'),
                  e.get('destination', {}).get('node_id'), e.get('destination', {}).get('field')) for e in edges]
    else:
        links = [(e.get('source'), e.get('sourceHandle'), e.get('target'), e.get('targetHandle')) for e in edges]
    if sorted(links) != sorted([(resize, 'image', rv, 'value'), (rv, 'value', ret, 'values')]):
        raise Fault('WORKFLOW_OUTPUT_UNSUPPORTED', 'IMAGE return must be connected directly to resize.image')
    return resize, rv, ret, value(rv, 'key')


def inputs(engine, p, observed):
    supplied = p['workflow_inputs']
    expected = observed['workflow_inputs']
    if set(supplied) - set(expected):
        raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'input must name an observed exposed field exactly')
    result = {}
    for key, value in supplied.items():
        spec = expected[key]
        if spec['type'] == 'image':
            ref = WorkflowImage.model_validate(value).model_dump()
            if any(ref[k] != p[k] for k in ('connection_id', 'backend_generation')):
                raise Fault('NATIVE_SCOPE_MISMATCH', 'IMAGE input differs from workflow backend')
            dto = engine.invoke.call('GET', '/api/v1/images/i/' + quote(ref['image_name'], safe=''))
            if dto.get('image_name') != ref['image_name'] or any(type(dto.get(k)) is not int or not 1 <= dto[k] <= 2048 for k in ('width','height')):
                raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'native IMAGE identity/dimensions outside recipe')
            result[key] = {'image_name': ref['image_name']}
        elif type(value) is not int or not spec['minimum'] <= value <= spec['maximum']:
            raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'bounded literal integer required')
        else:
            result[key] = value
    if any(spec['type'] == 'image' and key not in supplied for key, spec in expected.items()):
        raise Fault('WORKFLOW_INPUT_UNSUPPORTED', 'explicit scoped IMAGE input required; no saved-image default')
    return result


def hydrate(engine, job, item, compact):
    """Promote exactly one selected image after root AND child provenance agree."""
    if not job.get('plan', {}).get('workflow_image_recipe'):
        return compact
    compact['outputs'] = []
    entries = compact['native_results']['entries']
    compact['native_results']['image_status'] = 'image_lineage_unavailable'
    for candidate in entries:
        for field in ('value', 'image', 'lineage'):
            candidate.pop(field, None)
        candidate['status'] = 'image_lineage_unavailable'
    if len(entries) != 1:
        return compact
    entry = entries[0]
    session = item['session']
    if (item.get('status') != 'completed' or entry.get('type') != 'workflow_return_output' or
            item.get('item_id') not in job['native_ids'] or
            item.get('queue_id') != job['native_scope']['queue_id'] or
            session.get('id') != item.get('session_id') or
            session.get('graph') != job['plan']['graph'] or
            len(session.get('results', {})) != 1 or
            session.get('prepared_source_mapping') != {entry['node_id']: 'call'} or
            session.get('source_prepared_mapping', {}).get('call') != [entry['node_id']]):
        return compact
    prepared_call = session.get('execution_graph', {}).get('nodes', {}).get(entry['node_id'], {})
    if prepared_call != dict(job['plan']['graph']['nodes']['call'], id=entry['node_id']):
        return compact
    selected = session['results'][entry['node_id']].get('values', {}).get(job['payload']['return_key'])
    name = image_name(selected)
    if not name:
        entry['status'] = 'unsupported_value_type'
        return compact
    history = session.get('workflow_call_history', [])
    if len(history) != 1:
        return compact
    h = history[0]
    children = h.get('child_item_ids', [])
    if (h.get('status') != 'completed' or h.get('source_call_node_id') != 'call' or
            h.get('prepared_call_node_id') != entry['node_id'] or h.get('workflow_id') != job['payload']['workflow_id'] or
            h.get('parent_session_id') != item['session_id'] or h.get('depth') != 1 or
            h.get('expected_child_count') != 1 or len(children) != 1 or type(children[0]) is not int or
            h.get('completed_child_item_ids') != children):
        return compact
    child = engine.invoke.item(children[0])
    if (child.get('item_id') != children[0] or child.get('parent_item_id') != item['item_id'] or
            child.get('root_item_id') != item['item_id'] or child.get('parent_session_id') != item['session_id'] or
            child.get('workflow_call_id') != h.get('id') or child.get('workflow_call_depth') != 1 or
            child.get('queue_id') != job['native_scope']['queue_id'] or child.get('user_id') != item.get('user_id') or
            child.get('status') != 'completed' or child.get('session_id') != h.get('child_session_id')):
        return compact
    cs = child['session']; graph = cs.get('graph', {})
    parent = {'workflow_call_id': h['id'], 'parent_session_id': item['session_id'],
              'prepared_call_node_id': entry['node_id'], 'source_call_node_id': 'call',
              'workflow_id': job['payload']['workflow_id'], 'depth': 1}
    if (cs.get('id') != child['session_id'] or cs.get('workflow_call_parent') != parent or
            h.get('child_session_ids') != [child['session_id']] or
            not isinstance(graph.get('id'), str) or not graph['id'] or
            h.get('child_outputs') != {str(child['item_id']): {job['payload']['return_key']: selected}} or
            h.get('aggregated_values') != {job['payload']['return_key']: [selected]}):
        return compact
    nodes = graph.get('nodes', {})
    try:
        resize, rv, ret, key = recipe(nodes, graph.get('edges', []), native=True)
    except Fault:
        entry['status'] = 'executed_recipe_unsupported'
        return compact
    source_name = image_name(nodes[resize].get('image'))
    source_key = 'saved_workflow_input::' + resize + '::image'
    source_ref = {k: job['native_scope'][k] for k in ('connection_id', 'backend_generation')} | {'image_name': source_name}
    if (key != job['payload']['return_key'] or not source_name or name == source_name or
            job['payload']['workflow_inputs'].get(source_key) != source_ref or
            prepared_call['workflow_inputs'].get(source_key) != {'image_name': source_name} or
            any(n.get('id') != nid for nid, n in nodes.items())):
        return compact
    def results(source):
        return [(nid, r) for nid, r in cs.get('results', {}).items()
                if cs.get('prepared_source_mapping', {}).get(nid) == source]
    resized, named, returned = results(resize), results(rv), results(ret)
    if any(len(r) != 1 for r in (resized, named, returned)) or len(cs.get('results', {})) != 3:
        return compact
    rn, rr = resized[0]; vn, vr = named[0]; tn, tr = returned[0]
    for source, prepared in ((resize, rn), (rv, vn), (ret, tn)):
        executed = cs.get('execution_graph', {}).get('nodes', {}).get(prepared, {})
        if (cs.get('source_prepared_mapping', {}).get(source) != [prepared] or
                executed.get('id') != prepared or executed.get('type') != nodes[source]['type']):
            return compact
    executed_resize = cs['execution_graph']['nodes'][rn]
    if any(executed_resize.get(k) != nodes[resize].get(k) for k in ('image', 'width', 'height', 'resample_mode')):
        return compact
    # Explicit overrides are intent; omitted dimensions remain native latest defaults.
    if any(nodes[resize][k] != job['payload']['workflow_inputs'].get('saved_workflow_input::' + resize + '::' + k, nodes[resize][k]) for k in ('width', 'height')):
        return compact
    if (rr.get('type') != 'image_output' or image_name(rr.get('image')) != name or
            vr.get('type') != 'workflow_return_value_output' or vr.get('value') != {'key': key, 'value': selected} or
            tr.get('type') != 'workflow_return_output' or image_name(tr.get('values', {}).get(key)) != name or
            any(type(rr.get(k)) is not int or rr[k] != nodes[resize][k] for k in ('width','height'))):
        return compact
    lineage = {**job['native_scope'], 'root_item_id': item['item_id'], 'root_session_id': item['session_id'],
               'root_graph_id': session['graph']['id'], 'prepared_call_node_id': entry['node_id'],
               'workflow_call_id': h['id'], 'child_item_id': child['item_id'], 'child_session_id': child['session_id'],
               'child_graph_id': graph['id'], 'resize_source_node_id': resize, 'resize_prepared_node_id': rn,
               'return_prepared_node_id': tn, 'return_key': key, 'recipe': RECIPE,
               'width': rr['width'], 'height': rr['height'], 'input_source': source_ref}
    compact['native_results']['image_status'] = 'available'
    entry.update(status='available', image={k:job['native_scope'][k] for k in ('connection_id','backend_generation')} | {'image_name':name}, lineage=lineage)
    compact['outputs'] = [name]
    return compact
