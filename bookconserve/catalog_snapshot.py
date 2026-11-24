"""Catalogue snapshots: a usable bibliography only from a closed cut.

The page chain, the totals and the issuance digest must close at the same
catalogue cut before any bibliography is emitted. Mixed-revision or otherwise
unclosed batches yield only capture gaps and conflict witnesses; a late page
recomputes only its own library's own catalogue batch. The products are the
complete holdings bibliography and the capture gaps; this module does not
adjudicate conservation acceptance or physical loan handover.
"""
import re

from .catalog_capture import evaluate_batch


def _revision_key(revision):
    match = re.match(r'^(.*?)(\d+)$', revision or '')
    if match:
        return (match.group(1), int(match.group(2)))
    return (revision or '', -1)


def _product(evaluation, complete):
    return {
        'library': evaluation['library'],
        'revision': evaluation['revision'],
        'complete': complete,
        'volumes': evaluation['volumes'] if complete else [],
        'tombstones': evaluation['tombstones'] if complete else [],
        'superseded': evaluation['superseded'] if complete else [],
        'gaps': evaluation['gaps'],
        'conflicts': evaluation['conflicts'],
        'witnesses': evaluation['witnesses'],
    }


def build_batch(store, library, revision):
    """Snapshot one catalogue batch of one library."""
    batch = store.batch(library, revision)
    if batch is None:
        return {'library': library, 'revision': revision, 'complete': False,
                'volumes': [], 'tombstones': [], 'superseded': [],
                'gaps': [{'kind': 'no-batch'}], 'conflicts': [], 'witnesses': []}
    evaluation = evaluate_batch(batch)
    return _product(evaluation, evaluation['complete'])


def build(store, library, revision=None):
    """Build the library's current holdings bibliography and capture gaps.

    With `revision` None, the newest closed catalogue revision is the current
    bibliography; revisions still open are attached as gap and conflict
    witnesses. A mixed-revision crawl cannot prove the complete list of any
    frozen revision, so `volumes` stays empty until a cut closes.
    """
    if revision is not None:
        return build_batch(store, library, revision)
    evaluated = [evaluate_batch(batch) for (lib, _), batch in sorted(store.batches.items())
                 if lib == library]
    closed = [e for e in evaluated if e['complete']]
    gaps = []
    conflicts = []
    witnesses = []
    for e in evaluated:
        if not e['complete']:
            gaps.extend(dict(g, revision=e['revision']) for g in e['gaps'])
            conflicts.extend(dict(c, revision=e['revision']) for c in e['conflicts'])
        witnesses.extend(dict(w, revision=e['revision']) for w in e['witnesses'])
    if not closed:
        return {'library': library, 'revision': None, 'complete': False,
                'volumes': [], 'tombstones': [], 'superseded': [],
                'gaps': gaps, 'conflicts': conflicts, 'witnesses': witnesses}
    latest = max(closed, key=lambda e: _revision_key(e['revision']))
    product = _product(latest, True)
    product['gaps'] = gaps
    product['conflicts'] = conflicts
    product['witnesses'] = witnesses
    superseded = list(latest['superseded'])
    seen = {}
    for e in sorted(closed, key=lambda e: _revision_key(e['revision'])):
        for record in e['volumes']:
            accession = record['accession']
            previous = seen.get(accession)
            if previous is not None and previous[1] != record['edition']:
                superseded.append({'accession': accession, 'volume': record['volume'],
                                   'from_edition': previous[1], 'to_edition': record['edition'],
                                   'from_revision': previous[0], 'to_revision': e['revision']})
            seen[accession] = (e['revision'], record['edition'])
    product['superseded'] = superseded
    return product
