"""Offline capture of physical work periods and procedure states.

A bench receipt (bench camera or handheld timer) only proves that a device
recorded a bench interval.  It does not prove active restorer work: passive
drying keeps a bench occupied without consuming personal capacity, so bench
occupancy can never be summed into personal active time.

Raw receipts are supplemented with five kinds of offline evidence:

* clock_session: device/date shared clock offset and downtime segments.  The
  offset applies to every row of that device for the whole session, it can
  never be nudged per row to move an overlap away;
* presence: restorer location trajectory (co-located video / attendance);
* video: continuous co-located video pinning one receipt row to active work
  or passive drying;
* environment: drying environment curve giving the earliest dry-ready instant;
* sign_off: independent procedure sign-off (e.g. dry review).

All times are numeric hour-of-day values; dates are strings.  Evidence
without an explicit date belongs to the session date.
"""
from collections import defaultdict

DEFAULT_DATE = '2026-11-24'

ACTIVE = 'active'                    # 主动操作
PASSIVE_DRY = 'passive_dry'          # 被动干燥
UNANCHORED = 'unanchored'            # 缺时钟锚点/签认的原始台面段
PENDING_REVIEW = 'pending_review'    # 待复核
DEVICE_STOPPED = 'device_stopped'    # 落在设备停机段内
DRY_REVIEW = 'dry_review'
CONSOLIDATION = 'consolidation'


class CaptureConflict(ValueError):
    """Evidence cannot be made mutually consistent for one session."""


class ProcedureBlocked(RuntimeError):
    """A procedure is gated by an environment/sign-off precondition."""


def hours(rows):
    """Legacy raw receipt total: every end-start added without merging.

    Kept unchanged so the original bench receipts and the pre-correction
    zero-remaining projection stay reproducible.
    """
    return sum(row['end'] - row['start'] for row in rows)


def union_hours(intervals):
    """Total length of numeric intervals after overlap union."""
    if not intervals:
        return 0.0
    ordered = sorted((min(a, b), max(a, b)) for a, b in intervals)
    total = 0.0
    lo, hi = ordered[0]
    for a, b in ordered[1:]:
        if a > hi:
            total += hi - lo
            lo, hi = a, b
        elif b > hi:
            hi = b
    return total + hi - lo


def _ev_date(ev, date):
    return ev.get('date', date)


def build_clock_sessions(evidence, date):
    """Session-shared (device, date) -> (offset, downtime) calibration.

    Downtime intervals are expressed in device-local clock readings and are
    converted with the same offset as the receipts.
    """
    sessions = {}
    for ev in evidence:
        if ev.get('type') != 'clock_session' or _ev_date(ev, date) != date:
            continue
        key = (ev['device'], _ev_date(ev, date))
        record = (ev.get('offset_hours', 0),
                  tuple((a, b) for a, b in ev.get('downtime', ())))
        if key in sessions and sessions[key] != record:
            raise CaptureConflict(
                f'conflicting clock sessions for device {ev["device"]} on {key[1]}')
        sessions[key] = record
    return sessions


def calibrate_rows(rows, sessions, date):
    """Apply the single session offset of each row's device to that row."""
    calibrated = []
    for row in rows:
        row_date = row.get('date', date)
        offset, downtime = sessions.get((row['device'], row_date), (0, ()))
        start, end = row['start'] + offset, row['end'] + offset
        stopped = [(a + offset, b + offset) for a, b in downtime
                   if a + offset < end and start < b + offset]
        calibrated.append({
            'id': row['id'],
            'volume': row['volume'],
            'restorer': row['restorer'],
            'device': row['device'],
            'date': row_date,
            'raw_start': row['start'],
            'raw_end': row['end'],
            'start': start,
            'end': end,
            'offset_hours': offset,
            'downtime': stopped,
            'state': UNANCHORED,
        })
    return calibrated


def build_presence(evidence, date):
    """Restorer -> sorted physical presence trajectory."""
    trajectory = defaultdict(list)
    for ev in evidence:
        if ev.get('type') != 'presence' or _ev_date(ev, date) != date:
            continue
        for seg in ev.get('segments', ()):
            trajectory[ev['restorer']].append(
                (seg['start'], seg['end'],
                 seg.get('location'), seg.get('volume')))
    for restorer, segs in trajectory.items():
        segs.sort()
        for (a, b, loc1, vol1), (c, d, loc2, vol2) in zip(segs, segs[1:]):
            if c < b and (loc1 != loc2 or vol1 != vol2):
                raise CaptureConflict(
                    f'{restorer} cannot be present at two locations at once')
    return dict(trajectory)


def build_video_pins(evidence, date):
    """row_id -> continuous video pin covering the calibrated row."""
    pins = {}
    for ev in evidence:
        if ev.get('type') != 'video' or _ev_date(ev, date) != date:
            continue
        pins[ev['row_id']] = {
            'mode': ev['mode'],
            'start': ev['start'],
            'end': ev['end'],
            'stage': ev.get('stage'),
            'restorer': ev.get('restorer'),
        }
    return pins


def _require_presence(presence, row):
    segs = presence.get(row['restorer'], ())
    covering = [s for s in segs if s[0] <= row['start'] and row['end'] <= s[1]]
    if not covering:
        raise CaptureConflict(
            f'no presence trajectory covers active work on receipt {row["id"]}')
    if any(s[3] is not None and s[3] != row['volume'] for s in covering):
        raise CaptureConflict(
            f'presence places {row["restorer"]} at another volume than {row["volume"]}')


def _new_volume():
    return {
        'state': UNANCHORED,
        'active': [],
        'passive': [],
        'dry_ready_at': None,
        'dry_review_at': None,
        'consolidated_at': None,
    }


def _volume_states(calibrated, evidence, date, pins):
    volumes = defaultdict(_new_volume)
    for row in calibrated:
        v = volumes[row['volume']]
        if row['state'] == ACTIVE:
            v['active'].append((row['start'], row['end']))
        elif row['state'] == PASSIVE_DRY:
            v['passive'].append((row['start'], row['end']))
    for ev in evidence:
        if _ev_date(ev, date) != date:
            continue
        if ev.get('type') == 'environment':
            volumes[ev['volume']]['dry_ready_at'] = ev['dry_ready_at']
        elif ev.get('type') == 'sign_off' and ev.get('stage') == DRY_REVIEW:
            volumes[ev['volume']]['dry_review_at'] = ev['at']
    for row_id, pin in pins.items():
        if pin['mode'] == ACTIVE and pin['stage'] == CONSOLIDATION:
            row = next(r for r in calibrated if r['id'] == row_id)
            volumes[row['volume']]['consolidated_at'] = row['end']
    for v in volumes.values():
        if v['consolidated_at'] is not None:
            v['state'] = 'consolidated'
        elif v['passive']:
            if v['dry_review_at'] is not None:
                if (v['dry_ready_at'] is not None
                        and v['dry_review_at'] < v['dry_ready_at']):
                    raise CaptureConflict(
                        'dry review signed before the environment dry-ready instant')
                v['state'] = 'ready_to_consolidate'
            elif v['dry_ready_at'] is not None:
                v['state'] = 'awaiting_dry_review'
            else:
                v['state'] = 'drying'
        elif v['active']:
            v['state'] = 'active_work'
    return {k: dict(v) for k, v in volumes.items()}


def capture_session(rows, evidence=(), date=DEFAULT_DATE):
    """Resolve raw receipts plus evidence into physical procedure periods."""
    sessions = build_clock_sessions(list(evidence), date)
    calibrated = calibrate_rows(rows, sessions, date)
    presence = build_presence(list(evidence), date)
    pins = build_video_pins(list(evidence), date)
    by_id = {r['id']: r for r in calibrated}

    for row_id, pin in pins.items():
        if row_id not in by_id:
            raise CaptureConflict(f'video pins unknown receipt {row_id}')
        row = by_id[row_id]
        if pin['mode'] not in (ACTIVE, PASSIVE_DRY):
            raise CaptureConflict(f'unknown video mode {pin["mode"]}')
        if not (pin['start'] <= row['start'] and row['end'] <= pin['end']):
            raise CaptureConflict(
                f'video {pin["start"]}-{pin["end"]} does not cover calibrated '
                f'receipt {row_id} {row["start"]}-{row["end"]}')
        if pin['mode'] == ACTIVE and row['downtime']:
            raise CaptureConflict(
                f'active operation on {row_id} falls inside device downtime')
        row['state'] = pin['mode']

    active_by_restorer = defaultdict(list)
    for row in calibrated:
        if row['state'] == ACTIVE:
            active_by_restorer[row['restorer']].append(row)
    for restorer, segs in active_by_restorer.items():
        segs.sort(key=lambda r: (r['start'], r['end']))
        for earlier, later in zip(segs, segs[1:]):
            if later['start'] < earlier['end']:
                raise CaptureConflict(
                    f'{restorer} active intervals overlap; personal time cannot '
                    'be double counted')
        for row in segs:
            _require_presence(presence, row)

    volumes = _volume_states(calibrated, list(evidence), date, pins)
    active_segments = [
        {'restorer': r['restorer'], 'volume': r['volume'], 'row_id': r['id'],
         'start': r['start'], 'end': r['end']}
        for r in calibrated if r['state'] == ACTIVE]
    passive_segments = [
        {'restorer': r['restorer'], 'volume': r['volume'], 'row_id': r['id'],
         'start': r['start'], 'end': r['end']}
        for r in calibrated if r['state'] == PASSIVE_DRY]
    bench_segments = [
        {'device': r['device'], 'volume': r['volume'], 'row_id': r['id'],
         'start': r['start'], 'end': r['end'], 'state': r['state']}
        for r in calibrated]
    return {
        'date': date,
        'rows': calibrated,
        'clock_sessions': [
            {'device': device, 'date': d, 'offset_hours': offset,
             'downtime': [list(iv) for iv in stopped]}
            for (device, d), (offset, stopped) in sorted(sessions.items())],
        'presence': {k: [list(s) for s in v] for k, v in presence.items()},
        'active_segments': active_segments,
        'passive_segments': passive_segments,
        'bench_segments': bench_segments,
        'volumes': volumes,
    }


def active_hours(session, restorer=None):
    """Unioned personal active hours; overlaps never double counted."""
    intervals = [(s['start'], s['end']) for s in session['active_segments']
                 if restorer is None or s['restorer'] == restorer]
    return union_hours(intervals)


def bench_hours(session):
    """Occupied bench-hours: union per device, then summed across devices."""
    by_device = defaultdict(list)
    for seg in session['bench_segments']:
        by_device[seg['device']].append((seg['start'], seg['end']))
    return sum(union_hours(iv) for iv in by_device.values())


def pending_review_periods(session):
    """Receipt periods still awaiting anchors, sign-off or downtime review."""
    return [
        {'row_id': r['id'], 'volume': r['volume'], 'device': r['device'],
         'start': r['start'], 'end': r['end'],
         'reason': DEVICE_STOPPED if r['downtime'] else PENDING_REVIEW}
        for r in session['rows'] if r['state'] == UNANCHORED]


def schedule_consolidation(session, volume, start):
    """Consolidation gate: environment dry-ready plus dry review sign-off.

    A zero personal remaining balance never opens this gate and a passive
    drying bench record never advances a volume into consolidation by itself.
    """
    v = session['volumes'].get(volume)
    if v is None or v['state'] not in ('ready_to_consolidate', 'consolidated'):
        raise ProcedureBlocked(
            f'{volume} cannot consolidate before dry environment review sign-off')
    if v['dry_review_at'] is not None and start < v['dry_review_at']:
        raise ProcedureBlocked(
            f'{volume} consolidation at {start} precedes dry review at '
            f'{v["dry_review_at"]}')
    return {'volume': volume, 'start': start, 'allowed': True}
