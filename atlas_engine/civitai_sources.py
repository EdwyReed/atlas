"""Public exact-version source shapes; no prompt reconstruction/inferred usage.

Author = creator posts attached to this version, then their paginated images.
Capped version.images and post.images are coverage witnesses, not full sources.
"""
import re
from urllib.parse import urlsplit, parse_qs, urlencode
from .contracts import Fault
from .repository import dump

SEMANTICS = 'civitai-dual-stream-v2'
ASSET_UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'


def asset(record, fixture=False):
    url = record.get('url')
    if not isinstance(url, str):
        return None
    # RPC Image.url is the stored UUID; REST/version images use getEdgeUrl.
    # Accept the entire stored token, never a UUID substring in an opaque URL.
    if re.fullmatch(ASSET_UUID, url):
        return url.lower()
    u = urlsplit(url)
    if u.hostname != 'image.civitai.com' and not (fixture and u.hostname in ('127.0.0.1', 'localhost')):
        return None
    # getEdgeUrl embeds the stored Image.url UUID before transformation options.
    matches = re.findall(r'(?:^|/)(' + ASSET_UUID + r')(?=/|$)', u.path)
    return matches[0].lower() if len(matches) == 1 else None


def aliases(record, fixture=False):
    if not isinstance(record, dict):
        raise Fault('SOURCE_SHAPE', 'image object required')
    result = []
    iid = record.get('id')
    if type(iid) is int and iid > 0 or isinstance(iid, str) and iid:
        result.append({'system': 'civitai', 'image_id': iid})
    aid = asset(record, fixture)
    if aid:
        result.append({'system': 'civitai', 'asset_id': aid})
    if not result:
        url = record.get('url')
        if not isinstance(url, str) or not url:
            raise Fault('SOURCE_IDENTITY', 'image has no supplied ID, known asset UUID or URL')
        result.append({'system': 'civitai', 'source_url': url})
    return result


def community_url(base, version, limit):
    return base + '/api/v1/images?' + urlencode({'modelVersionId': version, 'limit': limit,
                                               'withMeta': 'true', 'browsingLevel': 31})


def continuation(onboard, page, current, version=None, post=None):
    metadata = page.get('metadata') or {}
    if not isinstance(metadata, dict):
        raise Fault('SOURCE_SHAPE', 'pagination metadata object required')
    value = metadata.get('nextPage')
    if not value and metadata.get('nextCursor'):
        # Official REST getNextPage serializes exactly these query params plus
        # the opaque cursor. Do not decode/reconstruct the cursor itself.
        from urllib.parse import parse_qsl
        values = [(k, v) for k, v in parse_qsl(urlsplit(current).query) if k != 'cursor']
        value = urlsplit(current)._replace(query=urlencode(values + [('cursor', metadata['nextCursor'])])).geturl()
    if not value:
        return None
    value = onboard.url(value)
    u = urlsplit(value)
    query = parse_qs(u.query)
    scope = {'postId': [str(post)]} if post is not None else {'modelVersionId': [str(version)]}
    if (u.path != '/api/v1/images' or set(query) - {next(iter(scope)), 'limit', 'page', 'cursor', 'withMeta', 'browsingLevel'}
            or any(query.get(k) != v for k, v in scope.items())):
        raise Fault('SOURCE_PAGINATION', 'unsupported exact-version pagination contract')
    # Some older source serializers omit explicit defaults in nextPage. Restore
    # only these fixed semantics; leave supplied cursor and other query bytes intact.
    additions = []
    for key, expected in (('withMeta', 'true'), ('browsingLevel', '31')):
        if key not in query:
            additions.append((key, expected))
        elif query[key] != [expected]:
            raise Fault('SOURCE_PAGINATION', 'continuation changed source visibility/metadata semantics')
    if additions:
        value += ('&' if u.query else '?') + urlencode(additions)
    return value


def author_slice(version):
    records = version.get('images') or []
    if not isinstance(records, list) or any(not isinstance(x, dict) for x in records):
        raise Fault('SOURCE_SHAPE', 'version images array required')
    return records


def rpc(o, method, query):
    url = o.base + '/api/trpc/' + method + '?' + urlencode({'input': dump({'json': query})})
    raw = o.json(url)
    try:
        page = raw['result']['data']['json']
        if not isinstance(page['items'], list) or 'nextCursor' not in page:
            raise TypeError()
    except (KeyError, TypeError):
        raise Fault('SOURCE_SHAPE', 'paginated RPC response with explicit cursor required') from None
    return url, raw, page


def cursor_check(cursor, previous):
    if cursor is not None and cursor in previous:
        raise Fault('SOURCE_PAGINATION', 'author cursor cycle')


def ordered(records, expected, fixture):
    from .onboarding_v2 import unique
    groups = unique(records, fixture)
    if type(expected) is not int or expected < 0 or len(groups) != expected:
        raise Fault('AUTHOR_COVERAGE', 'post imageCount differs from exhausted RPC source')
    rows = [g['raw'] for g in groups]
    if any(type(r.get('index')) is not int for r in rows):
        return rows  # Order is provenance, not a coverage requirement.
    # The DB order is (i.index, i.id); indices may be zero, gapped or shared.
    from collections import Counter
    shared = {index for index, count in Counter(r['index'] for r in rows).items() if count > 1}
    if any(type(r.get('id')) is not int or r['id'] <= 0 for r in rows if r['index'] in shared):
        return rows
    return sorted(rows, key=lambda r: (r['index'], r.get('id') if r['index'] in shared else 0))


def collect_author(o, job, update):
    """Checkpoint posts, RPC ordering and independent REST hydration per page.

    Only exact numeric IDs join the two sources. REST order is never carousel
    order; its raw records and URLs remain separate from literal RPC records.
    """
    from .onboarding_v2 import unique
    p, v = job['payload'], job['verified']
    witness = author_slice(v['version'])
    username = (v['model'].get('creator') or {}).get('username')
    if not isinstance(username, str) or not username or username == '-1':
        return update(author_records=witness, author_status={
            'complete': False, 'reason': 'author_identity_unavailable', 'observed': len(witness)})
    try:
        if 'author_posts' not in job:
            job = update(author_posts=[], author_pages=[], author_cursor=None, author_posts_done=False,
                         author_post_index=0, author_records=[], author_hydration=[])
        while not job['author_posts_done']:
            query = {'modelVersionId': p['version_id'], 'username': username, 'limit': 100,
                     'sort': 'Oldest', 'period': 'AllTime', 'browsingLevel': 31, 'include': []}
            if job['author_cursor'] is not None:
                query['cursor'] = job['author_cursor']
            url, raw, page = rpc(o, 'post.getInfinite', query)
            posts = {x['id']: x for x in job['author_posts']}
            for post in page['items']:
                if (not isinstance(post, dict) or type(post.get('id')) is not int or post['id'] <= 0
                        or post.get('modelVersionId') != p['version_id']
                        or (post.get('user') or {}).get('username') != username):
                    raise Fault('SOURCE_ID_MISMATCH', 'author post escaped exact version/creator scope')
                if post['id'] in posts and posts[post['id']] != post:
                    raise Fault('AUTHOR_COVERAGE', 'author post changed during frozen enumeration')
                posts.setdefault(post['id'], post)
            cursor = page['nextCursor']
            cursor_check(cursor, [x.get('cursor') for x in job['author_pages'] if x.get('role') == 'posts'] + [job['author_cursor']])
            job = update(author_posts=list(posts.values()), author_cursor=cursor, author_posts_done=cursor is None,
                         author_pages=job['author_pages'] + [{'url': url, 'raw': raw, 'role': 'posts', 'cursor': job['author_cursor']}])
            o.hook('after_author_page', {'job_id': job['id']})
        while job['author_post_index'] < len(job['author_posts']):
            post = job['author_posts'][job['author_post_index']]
            if not job.get('author_rpc_done'):
                query = {'postId': post['id'], 'period': 'AllTime', 'browsingLevel': 31,
                         'limit': 200, 'include': [], 'sort': 'Most Reactions'}
                # RPC withMeta is a selection filter, not REST's metadata
                # projection flag: true can exclude every image in a post.
                # Literal metadata comes from the independent REST hydration.
                if job.get('author_image_cursor') is not None:
                    query['cursor'] = job['author_image_cursor']
                url, raw, page = rpc(o, 'image.getInfinite', query)
                for record in page['items']:
                    if (not isinstance(record, dict) or record.get('postId') != post['id']
                            or record.get('modelVersionId') != p['version_id']
                            or ((post.get('user') or {}).get('id') is not None
                                and (record.get('user') or {}).get('id') != post['user']['id'])):
                        raise Fault('SOURCE_ID_MISMATCH', 'RPC image escaped exact post/version/creator scope')
                records = job.get('author_post_records', []) + page['items']
                cursor = page['nextCursor']
                cursor_check(cursor, job.get('author_image_cursors', []) + [job.get('author_image_cursor')])
                changes = dict(author_image_cursor=cursor, author_image_cursors=job.get('author_image_cursors', []) + [job.get('author_image_cursor')],
                               author_rpc_done=cursor is None, author_post_records=records,
                               author_pages=job['author_pages'] + [{'url': url, 'raw': raw, 'role': 'image_order'}])
                if cursor is None:
                    changes['author_post_ordered'] = ordered(records, post.get('imageCount'), o.fixture)
                job = update(**changes)
                o.hook('after_author_page', {'job_id': job['id']})
                continue
            current = job.get('author_image_next') or o.base + '/api/v1/images?' + urlencode({
                'postId': post['id'], 'limit': 200, 'withMeta': 'true', 'browsingLevel': 31})
            page = o.json(current)
            items = page.get('items')
            if not isinstance(items, list):
                raise Fault('SOURCE_SHAPE', 'post REST images array required')
            ids = {r['id'] for r in job['author_post_ordered'] if type(r.get('id')) is int and r['id'] > 0}
            hydration = list(job.get('author_post_hydration', []))
            for record in items:
                if not isinstance(record, dict) or record.get('postId', post['id']) != post['id']:
                    raise Fault('SOURCE_ID_MISMATCH', 'REST image escaped author post scope')
                if type(record.get('id')) is int and record['id'] in ids:
                    hydration.append({'raw': record, 'source_url': current})
            nxt = continuation(o, page, current, post=post['id'])
            if nxt and nxt in [x['url'] for x in job['author_pages']] + [current]:
                raise Fault('SOURCE_PAGINATION', 'author REST cursor cycle')
            changes = {'author_image_next': nxt, 'author_post_hydration': hydration,
                       'author_pages': job['author_pages'] + [{'url': current, 'raw': page, 'role': 'rest_metadata'}]}
            if not nxt:
                # Missing literal metadata is unknown, not an inferred prompt.
                changes.update(author_post_index=job['author_post_index'] + 1, author_post_records=[],
                               author_records=job['author_records'] + job['author_post_ordered'],
                               author_hydration=job.get('author_hydration', []) + hydration,
                               author_post_hydration=[], author_rpc_done=False, author_image_cursor=None,
                               author_image_cursors=[], author_post_ordered=[])
            job = update(**changes)
            o.hook('after_author_page', {'job_id': job['id']})
        # Validate the joint evidence before treating shared aliases as coverage:
        # a witness asserting a different numeric ID must not turn green.
        unique(job['author_records'] + witness, o.fixture)
        covered = {a for x in unique(job['author_records'], o.fixture) for a in x['aliases']}
        if any(not covered.intersection(x['aliases']) for x in unique(witness, o.fixture)):
            raise Fault('AUTHOR_COVERAGE', 'author posts do not cover known version carousel assets')
        return update(author_status={'complete': True, 'reason': 'source_exhausted',
                                     'source': 'creator posts / RPC source indices / exact-ID REST metadata',
                                     'observed': len(unique(job['author_records'], o.fixture))})
    except Fault as exc:
        if exc.code == 'SOURCE_HTTP_429' or not (exc.code.startswith('SOURCE_HTTP_') or exc.code == 'AUTHOR_COVERAGE'):
            raise
        records = job.get('author_records', []) + witness
        return update(author_records=records, author_status={'complete': False, 'reason': exc.code,
                      'observed': len(unique(records, o.fixture)), 'source': 'creator posts / RPC source indices / exact-ID REST metadata',
                      'retry_requires_new_refresh': True})
