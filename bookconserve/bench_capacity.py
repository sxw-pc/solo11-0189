"""Restorer/bench dual capacity ledgers with versioned correction history.

The legacy projection assumed bench occupancy equals personal work and the
eight-hour shift minus the raw receipt sum is the remaining personal balance.
That zero-remaining result is kept as revision 1 of every capacity ledger so
the pre-correction outcome remains auditable after supplementary evidence is
accepted.  Later revisions add separate personal and bench accounts instead
of rewriting the original receipt-based result.
"""
import copy
from collections import defaultdict
from .shift_capture import (
    CaptureConflict, ProcedureBlocked, active_hours, bench_hours,
    capture_session, hours, pending_review_periods, schedule_consolidation,
    union_hours,
)

ROUTE_PASSIVE = 'passive_drying_parallel'   # 补证路线甲：V1 被动干燥并行
ROUTE_ACTIVE = 'active_work_contiguous'     # 补证路线乙：校时后两段主动相接
LEGACY_REVISION = 1
PENDING_LABEL = 'pending_evidence'

__all__ = ['remaining', 'open_ledger', 'supplement_evidence', 'recalibrate',
           'schedule_consolidation', 'CaptureConflict', 'ProcedureBlocked']


def remaining(rows, capacity=8):
    """Legacy raw-receipt projection, unchanged for old fixtures."""
    used = hours(rows)
    return {'used': used, 'remaining': capacity - used}


def _bench_detail(session):
    by_device = defaultdict(list)
    for seg in session['bench_segments']:
        by_device[seg['device']].append((seg['start'], seg['end']))
    return {device: {'occupied_hours': union_hours(iv),
                     'intervals': [list(pair) for pair in sorted(iv)]}
            for device, iv in sorted(by_device.items())}


def _ledger(date, capacity, session, personal, bench, status):
    return {
        'date': date,
        'capacity': capacity,
        'status': status,
        'personal_account': dict(personal),
        'bench_account': dict(bench),
        'pending_review': pending_review_periods(session),
        'volumes': session['volumes'],
        'clock_sessions': session['clock_sessions'],
    }


def open_ledger(rows, evidence=(), date='2026-11-24', capacity=8):
    """Open a versioned capacity ledger; revision 1 freezes the legacy result."""
    legacy = remaining(rows, capacity=capacity)
    session = capture_session(rows, evidence, date=date)
    personal = {r: {'used_hours': active_hours(session, r),
                    'remaining_hours': capacity - active_hours(session, r)}
                for r in {row['restorer'] for row in rows}}
    bench = {'occupied_hours': bench_hours(session),
             'by_device': _bench_detail(session)}
    anchored = bool(evidence)
    revision1 = {
        'revision': LEGACY_REVISION,
        'route': 'legacy_bench_sum',
        'date': date,
        'raw_receipt_hours': legacy['used'],
        'used': legacy['used'],
        'remaining': legacy['remaining'],
        'note': '更正前按台面回盘逐行求和得到的零剩余基线，原回盘不可改写',
    }
    revision2 = _ledger(
        date, capacity, session, personal, bench,
        'evidence_resolved' if anchored else PENDING_LABEL)
    return {
        'date': date,
        'raw_rows': [dict(row) for row in rows],
        'revisions': [revision1, revision2],
        'current': revision2,
        'evidence_ids': sorted(ev['id'] for ev in evidence if 'id' in ev),
    }


def supplement_evidence(ledger, rows, evidence, route, date='2026-11-24',
                        capacity=8):
    """Append a correction revision from route A (passive) or B (active)."""
    if route not in (ROUTE_PASSIVE, ROUTE_ACTIVE):
        raise ValueError(f'unknown supplement route {route}')
    if ledger['raw_rows'] != [dict(row) for row in rows]:
        raise ValueError('original bench receipts must be retained unchanged')
    session = capture_session(rows, evidence, date=date)
    personal = {r: {'used_hours': active_hours(session, r),
                    'remaining_hours': capacity - active_hours(session, r)}
                for r in {row['restorer'] for row in rows}}
    bench = {'occupied_hours': bench_hours(session),
             'by_device': _bench_detail(session)}
    revision = _ledger(
        date, capacity, session, personal, bench, 'evidence_resolved')
    revision.update({
        'revision': len(ledger['revisions']) + 1,
        'route': route,
        'raw_receipt_hours': hours(rows),
        'evidence_ids': sorted(ev['id'] for ev in evidence if 'id' in ev),
        'note': ('V1 被动干燥，加固须在干燥复核签认后' if route == ROUTE_PASSIVE
                 else '设备校时后两段主动工作相接，V1 加固已完成'),
    })
    ledger['revisions'].append(revision)
    ledger['current'] = revision
    ledger['evidence_ids'] = sorted(
        set(ledger['evidence_ids']) | set(revision['evidence_ids']))
    return ledger


def recalibrate(ledgers, rows_by_date, evidence_by_date, route, capacity=8):
    """Late clock calibration recomputes only the ledgers of affected dates.

    Device offsets are session-shared: each date's evidence is solved as one
    session, and ledgers for dates with no new calibration stay byte-stable.
    """
    affected = set(evidence_by_date)
    snapshots = {date: copy.deepcopy(ledger)
                 for date, ledger in ledgers.items() if date not in affected}
    for date in affected:
        if date not in ledgers or date not in rows_by_date:
            raise CaptureConflict(f'calibration for unknown shift date {date}')
        supplement_evidence(
            ledgers[date], rows_by_date[date], evidence_by_date[date], route,
            date=date, capacity=capacity)
    for date, ledger in ledgers.items():
        if date in affected:
            continue
        # Unaffected dates stay byte-stable, including the frozen legacy
        # revision 1 carrying the pre-correction zero-remaining result.
        assert ledger == snapshots[date]
    return ledgers
