"""Offline catalogue page capture with revision-bound cursors.

The conservation library collects loan-candidate bibliographies page by page
from a partner library's offline simulated catalogue. When the partner syncs
a redesign mid-crawl, a volume on the first page does not prove the batch was
fully captured: every page is filed under the (library, catalogue revision,
page, sort boundary) cursor it binds to, and a page from a foreign revision is
kept only as a conflict witness, never merged into another revision's batch.

The accession number identifies the physical volume: same title with distinct
accessions stays two physical records; same accession with distinct editions
keeps a supersession relation instead of being merged by title hash. This
module only captures and files catalogue pages; it does not adjudicate
conservation acceptance or physical loan handover.
"""
import json
from html.parser import HTMLParser
from pathlib import Path


class _PageHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.body = {}
        self.entries = []
        self.tombstones = []
        self.next = None
        self._open = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'body':
            self.body = a
        elif tag == 'div' and 'data-tombstone' in a:
            self._open = ('tombstone', {
                'kind': a['data-tombstone'],
                'accession': a.get('data-accession'),
                'volume': a.get('data-volume'),
                'sort': a.get('data-sort'),
                'to_sort': a.get('data-to-sort'),
                'title': '',
            })
        elif tag == 'div' and 'data-volume' in a:
            self._open = ('entry', {
                'accession': a.get('data-accession'),
                'volume': a['data-volume'],
                'edition': a.get('data-edition'),
                'sort': a.get('data-sort'),
                'title': '',
            })
        elif tag == 'a' and 'data-next' in a:
            self.next = a['data-next']

    def handle_data(self, data):
        if self._open is not None:
            self._open[1]['title'] += data

    def handle_endtag(self, tag):
        if tag == 'div' and self._open is not None:
            kind, record = self._open
            record['title'] = record['title'].strip()
            (self.tombstones if kind == 'tombstone' else self.entries).append(record)
            self._open = None


def parse_page(html, source=None):
    """Parse one catalogue page into a cursor-bound page record."""
    parser = _PageHTMLParser()
    parser.feed(html)
    parser.close()
    body = parser.body
    library = body.get('data-library')
    revision = body.get('data-revision')
    if not library or not revision:
        raise ValueError('catalogue page must carry data-library and data-revision')
    try:
        page_no = int(body['data-page'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('catalogue page must carry an integer data-page cursor') from None
    cursor = {
        'library': library,
        'revision': revision,
        'page': page_no,
        'sort_start': body.get('data-sort-start'),
        'sort_end': body.get('data-sort-end'),
    }
    digest = None
    if body.get('data-digest-serial') is not None:
        try:
            digest = {
                'library': library,
                'revision': revision,
                'serial': body['data-digest-serial'],
                'pages': int(body['data-digest-pages']),
                'volumes': int(body['data-digest-volumes']),
                'tombstones': int(body.get('data-digest-tombstones', 0)),
            }
        except (KeyError, TypeError, ValueError):
            raise ValueError('catalogue issuance digest must carry serial, pages and volumes') from None
    entries = []
    for record in parser.entries:
        if not record['accession']:
            raise ValueError('catalogue entry must carry data-accession')
        entries.append(record)
    return {
        'library': library,
        'revision': revision,
        'cursor': cursor,
        'entries': entries,
        'tombstones': parser.tombstones,
        'next': parser.next,
        'digest': digest,
        'source': source,
    }


def read_page(file):
    """Read and parse a single catalogue HTML page from disk."""
    path = Path(file)
    return parse_page(path.read_text(encoding='utf-8'), source=str(path))


def _fingerprint(page):
    """Content identity of a page: cursor, entries, tombstones and next link.

    The issuance digest and the capture source are excluded: a cached page
    re-supplied with its digest statement is the same page, and the same page
    may arrive from different mirrors.
    """
    body = {
        'cursor': page['cursor'],
        'entries': page['entries'],
        'tombstones': page['tombstones'],
        'next': page['next'],
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def evaluate_batch(batch):
    """Evaluate the closure of one (library, catalogue revision) batch.

    Only when the page chain, the totals and the issuance digest close at the
    same catalogue cut may the batch yield a usable bibliography; otherwise it
    yields only capture gaps and conflict witnesses.
    """
    gaps = []
    conflicts = [dict(c) for c in batch['conflicts']]
    pages = batch['pages']
    nums = sorted(pages)
    if not nums:
        gaps.append({'kind': 'no-pages'})
    else:
        missing = [n for n in range(1, nums[-1] + 1) if n not in pages]
        if missing:
            gaps.append({'kind': 'missing-pages', 'pages': missing})
        for n in nums:
            page = pages[n]
            if n == nums[-1]:
                if page['next']:
                    gaps.append({'kind': 'unclosed-next', 'page': n, 'next': page['next']})
            elif not page['next']:
                gaps.append({'kind': 'broken-next', 'page': n})
        for a, b in zip(nums, nums[1:]):
            end = pages[a]['cursor']['sort_end']
            start = pages[b]['cursor']['sort_start']
            if end is not None and start is not None and start <= end:
                conflicts.append({'kind': 'sort-boundary-overlap', 'pages': [a, b],
                                  'sort_end': end, 'sort_start': start})
        for n in nums:
            cursor = pages[n]['cursor']
            lo, hi = cursor['sort_start'], cursor['sort_end']
            for entry in pages[n]['entries']:
                sort = entry.get('sort')
                if sort is not None and lo is not None and hi is not None and not lo <= sort <= hi:
                    conflicts.append({'kind': 'entry-out-of-bounds', 'page': n,
                                      'accession': entry['accession'], 'sort': sort})
    live = {}
    for n in nums:
        for index, entry in enumerate(pages[n]['entries']):
            live.setdefault(entry['accession'], []).append((n, index, entry))
    tombstones = [dict(t) for t in batch['tombstones']]
    relocated = {t['accession'] for t in tombstones if t.get('kind') == 'relocated'}
    withdrawn = {t['accession'] for t in tombstones if t.get('kind') == 'withdrawn'}
    for accession in sorted(live):
        occurrences = live[accession]
        editions = {entry['edition'] for _, _, entry in occurrences}
        if len(occurrences) > 1 and len(editions) == 1 and accession not in relocated:
            conflicts.append({'kind': 'duplicate-accession', 'accession': accession,
                              'pages': [n for n, _, _ in occurrences]})
    for accession in sorted(withdrawn & set(live)):
        conflicts.append({'kind': 'tombstone-entry-clash', 'accession': accession})
    for accession in sorted(relocated - set(live)):
        gaps.append({'kind': 'relocation-unresolved', 'accession': accession})
    distinct = {}
    for statement in batch['digests']:
        distinct.setdefault(json.dumps(statement, ensure_ascii=False, sort_keys=True), statement)
    if len(distinct) > 1:
        conflicts.append({'kind': 'digest-mismatch', 'digests': list(distinct.values())})
    elif not distinct:
        gaps.append({'kind': 'missing-digest'})
    else:
        statement = next(iter(distinct.values()))
        for field, actual in (('pages', len(nums)), ('volumes', len(live)),
                              ('tombstones', len(tombstones))):
            if statement[field] != actual:
                conflicts.append({'kind': 'digest-mismatch', 'field': field,
                                  'expected': statement[field], 'actual': actual})
    volumes = []
    superseded = []
    for accession in sorted(live):
        occurrences = sorted(live[accession])
        current = occurrences[-1][2]
        for _, _, older in occurrences[:-1]:
            if older['edition'] != current['edition']:
                superseded.append({'accession': accession, 'volume': older['volume'],
                                   'from_edition': older['edition'],
                                   'to_edition': current['edition']})
        volumes.append({'accession': accession, 'volume': current['volume'],
                        'title': current['title'], 'edition': current['edition'],
                        'sort': current.get('sort')})
    volumes.sort(key=lambda record: (record['sort'] is None, record['sort'] or '',
                                     record['accession']))
    return {
        'library': batch['library'],
        'revision': batch['revision'],
        'complete': not gaps and not conflicts,
        'volumes': volumes,
        'tombstones': tombstones,
        'superseded': superseded,
        'gaps': gaps,
        'conflicts': conflicts,
        'witnesses': [dict(w) for w in batch['witnesses']],
    }


class CaptureStore:
    """Catalogue batches filed by (library, catalogue revision).

    A late page only enters the batch its cursor binds to and only triggers
    recomputation of that batch. Crawl runs are kept as immutable witnesses; a
    mixed-revision run stays unfinished forever and must not be retroactively
    treated as closed.
    """

    def __init__(self):
        self.batches = {}
        self.runs = []

    def _batch(self, library, revision):
        key = (library, revision)
        if key not in self.batches:
            self.batches[key] = {
                'library': library,
                'revision': revision,
                'pages': {},
                'fingerprints': {},
                'digests': [],
                'tombstones': [],
                'witnesses': [],
                'conflicts': [],
            }
        return self.batches[key]

    def batch(self, library, revision):
        return self.batches.get((library, revision))

    def add_digest(self, digest):
        """Register a pre-authorised catalogue issuance digest (page/volume totals)."""
        missing = [k for k in ('library', 'revision', 'serial', 'pages', 'volumes', 'tombstones')
                   if k not in digest]
        if missing:
            raise ValueError(f'digest statement missing {missing}')
        statement = {k: digest[k] for k in
                     ('library', 'revision', 'serial', 'pages', 'volumes', 'tombstones')}
        batch = self._batch(statement['library'], statement['revision'])
        if statement not in batch['digests']:
            batch['digests'].append(statement)
        return dict(statement)

    def add_tombstone(self, library, revision, tombstone):
        """Register a pre-authorised relocation/withdrawal tombstone for one batch only."""
        record = dict(tombstone)
        batch = self._batch(library, revision)
        if record not in batch['tombstones']:
            batch['tombstones'].append(record)
        return record

    def add_page(self, page, via=None, run=None):
        """File one page under its cursor; `via` is the cursor that led here."""
        cursor = page['cursor']
        key = (cursor['library'], cursor['revision'])
        batch = self._batch(*key)
        if via is not None and (via['library'], via['revision']) != key:
            witness = {'kind': 'chain-break', 'from': dict(via), 'to': dict(cursor)}
            for holder in (self._batch(via['library'], via['revision']), batch):
                if witness not in holder['witnesses']:
                    holder['witnesses'].append(witness)
            if run is not None:
                run['witnesses'].append(witness)
        if page['digest'] is not None and page['digest'] not in batch['digests']:
            batch['digests'].append(page['digest'])
        for tombstone in page['tombstones']:
            if tombstone not in batch['tombstones']:
                batch['tombstones'].append(tombstone)
        fingerprint = _fingerprint(page)
        page_no = cursor['page']
        receipt = {'batch': key, 'page': page_no}
        if page_no in batch['pages']:
            if batch['fingerprints'][page_no] == fingerprint:
                receipt['status'] = 'replayed'
            else:
                conflict = {'kind': 'page-content-mismatch', 'page': page_no,
                            'held': batch['fingerprints'][page_no], 'received': fingerprint}
                if conflict not in batch['conflicts']:
                    batch['conflicts'].append(conflict)
                receipt['status'] = 'conflict'
            return receipt
        batch['pages'][page_no] = page
        batch['fingerprints'][page_no] = fingerprint
        receipt['status'] = 'added'
        return receipt

    def crawl(self, pages):
        """Capture an ordered run of fetched pages, kept as an immutable witness."""
        run = {'id': f'run-{len(self.runs) + 1}', 'pages': [], 'witnesses': [],
               'status': 'unfinished'}
        via = None
        touched = []
        for page in pages:
            self.add_page(page, via=via, run=run)
            cursor = page['cursor']
            run['pages'].append(dict(cursor))
            key = (cursor['library'], cursor['revision'])
            if key not in touched:
                touched.append(key)
            via = cursor
        if not run['witnesses'] and touched and all(
                evaluate_batch(self.batches[key])['complete'] for key in touched):
            run['status'] = 'closed'
        self.runs.append(run)
        return run
