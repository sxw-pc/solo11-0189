"""离线借展采录：箱封签、开箱、逐件交接、送检、返馆点验。

本模块只负责采证侧的确定性处理：
  * 事件结构校验与双签判定（单方扫码只能记待核，见 :func:`signed_event`）；
  * 按实物 ID 去重、按事件时间排序（迟到回执保留原 id 与原 received_at）；
  * 箱/件关系构建（封签状态、开箱拆出、逐件归属，见 :func:`build_boxes`）；
  * SQLite 事务账本（异内容同 id 冲突整体回滚）。

推断逐件保管位置是 :mod:`bookconserve.loan_reconcile` 的职责，
箱级回执在那里与开封逐件清单、封签状态合读，不会自动覆盖已拆出的散页。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

# 动作分类：箱级动作、逐件动作、补证处置动作
PACKAGE_ACTIONS = frozenset({'seal', 'handoff', 'receive', 'return', 'open'})
ITEM_ACTIONS = frozenset({'unpack', 'item_handoff', 'return_check', 'inventory'})
CORRECTION_ACTIONS = frozenset({'correction', 'seal_proof'})
KNOWN_ACTIONS = PACKAGE_ACTIONS | ITEM_ACTIONS | CORRECTION_ACTIONS

# 每个动作的必填字段
REQUIRED_FIELDS = {
    'seal': ('package', 'seal_id', 'location'),
    'handoff': ('package', 'location'),
    'receive': ('package', 'location'),
    'return': ('package', 'location'),
    'open': ('package', 'location'),
    'unpack': ('package', 'item', 'location'),
    'item_handoff': ('item', 'from_party', 'to_party', 'location'),
    'return_check': ('package', 'item', 'location'),
    'inventory': ('item', 'location', 'party'),
    'correction': ('supersedes', 'correct_result'),
    'seal_proof': ('package', 'seal_id', 'location'),
}

# 旧版夹具（无动作分类的整箱事件）可识别动作
LEGACY_ACTIONS = frozenset({'handoff', 'receive', 'unpack', 'return'})

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS loan_sources (
    sha TEXT PRIMARY KEY,
    raw BLOB NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS loan_events (
    id TEXT PRIMARY KEY,
    at INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    action TEXT NOT NULL,
    package TEXT,
    item TEXT,
    body TEXT NOT NULL,
    sha TEXT NOT NULL
);
"""


def validate_event(event):
    """校验单条离线采录事件；不合法时抛 :class:`ValueError`。

    旧版夹具的裸箱级/拆件事件（只有 package/item/location/at/received_at）
    按可读旧账放行；新版事件必须带 action 且满足各动作的必填字段。
    双签动作 ``item_handoff`` 还要求交出方与接收方为两个不同责任方。
    """
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    if 'id' not in event or 'at' not in event or 'received_at' not in event:
        raise ValueError(f"event {event.get('id', '?')}: id/at/received_at required")
    action = event.get('action')
    if action is None:
        raise ValueError(f"event {event['id']}: action required")
    if action not in KNOWN_ACTIONS:
        raise ValueError(f"event {event['id']}: unknown action {action!r}")
    for field in REQUIRED_FIELDS[action]:
        if field not in event or event[field] in (None, ''):
            raise ValueError(f"event {event['id']}: field {field!r} required for {action}")
    if action == 'item_handoff' and event['from_party'] == event['to_party']:
        raise ValueError(f"event {event['id']}: handoff requires two distinct parties")
    if not isinstance(event['at'], int) or not isinstance(event['received_at'], int):
        raise ValueError(f"event {event['id']}: at/received_at must be integer timestamps")
    return True


def signed_event(event):
    """双签判定：交出方与接收方同时在场且不同，才算有效逐件交接。

    只有一方扫码（如展馆单方送检扫码）返回 ``False``，
    调用方必须把它记为待核，不得当成保管责任转移。
    """
    if event.get('action') != 'item_handoff':
        return False
    fp, tp = event.get('from_party'), event.get('to_party')
    return bool(fp and tp and fp != tp)


def _dedup_key(event):
    """去重键：逐件事件按实物 ID（合动作），箱级事件按箱号，其余按事件 id。"""
    item = event.get('item')
    if item:
        return ('item', item, event['action'])
    package = event.get('package')
    if package:
        return ('package', package, event['action'])
    return ('id', event['id'])


def deduplicate(events):
    """按实物 ID / 箱号去重。

    同一实物同一动作出现多条（补录重复采录）时，保留双签优先、
    其次先到（received_at 小）、再按 id 确定的一条；被丢弃的事件 id
    一并返回，供解释“被排除的冲突回执”。
    """
    def rank(ev):
        return (0 if signed_event(ev) else 1, ev['received_at'], ev['id'])

    winners = {}
    dropped = []
    for ev in events:
        key = _dedup_key(ev)
        old = winners.get(key)
        if old is None or rank(ev) < rank(old):
            if old is not None:
                dropped.append(old['id'])
            winners[key] = ev
        else:
            dropped.append(ev['id'])
    return list(winners.values()), dropped


def order_events(events):
    """确定性时间排序：事件发生时间 at，再到采录接收时间 received_at，再按 id。"""
    return sorted(events, key=lambda e: (e['at'], e['received_at'], e['id']))


def prepare_events(events):
    """采录流水线：逐条校验 → 去重 → 时间排序，返回 (有序事件, 被去重 id)。"""
    for ev in events:
        validate_event(ev)
    unique, dropped = deduplicate(events)
    return order_events(unique), dropped


def load_loan_record(path):
    """读取借展采录文件，兼容旧版裸事件夹具与新版分区补证夹具。

    新版结构::

        {"manifest": [...], "events": [基础事件...],
         "route_a": {"events": [...]}, "route_b": {"events": [...]}}

    旧版结构只有 ``manifest`` 与 ``events``，原样可读。
    """
    record = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(record, dict) or 'events' not in record:
        raise ValueError('loan record requires manifest and events')
    manifest = list(record.get('manifest', ()))
    base = list(record['events'])
    route_a = list(record.get('route_a', {}).get('events', ()))
    route_b = list(record.get('route_b', {}).get('events', ()))
    return {'manifest': manifest, 'events': base,
            'route_a_events': route_a, 'route_b_events': route_b, 'raw': record}


def latest(events, package=None):
    """按 received_at 选最后一张回执（箱级语义，保留旧接口）。

    注意这只能描述“最后一张箱级回执说了什么”，不能描述箱内每件实物的
    实际位置——整箱返馆扫描证明不了已拆出散页随之返馆。
    """
    rows = [e for e in events if package is None or e.get('package') == package]
    if not rows:
        raise ValueError('no event matches')
    return max(rows, key=lambda e: (e['received_at'], e['id']))


def build_boxes(events, manifest, excluded=None):
    """构建箱/件关系：每个箱的封签链、责任方时间线、开箱拆出与逐件归属。

    返回 ``{package: {...}}``，含：
      * ``seal_id`` / ``seal_intact``：当前封签号与封签链是否未被破坏；
      * ``items``：封入时逐件清单（实物 id -> 封入位置）；
      * ``removed``：开封/交接已拆出的实物 id 集合；
      * ``custody_timeline``：箱级责任方时间线 ``(at, location, party, event_id)``；
      * ``opened``：是否发生过未被封签证明推翻的开箱。
    箱级回执只更新箱本身的责任方与位置，不会改动已拆出实物的归属。
    ``excluded`` 为冲突检测已排除的事件 id 集合，构建箱关系时一并跳过。
    """
    excluded = excluded or frozenset()
    boxes = {}

    def box(package):
        return boxes.setdefault(package, {
            'package': package, 'seal_id': None, 'seal_intact': None,
            'items': {}, 'removed': set(), 'opened': False,
            'custody_timeline': [], 'seal_events': [],
        })

    def seed_manifest(b):
        # 旧夹具无显式封签事件：首次出现箱级动作时以借展清单建立逐件关系
        for item in manifest:
            b['items'].setdefault(item, None)

    for ev in order_events(events):
        if ev['id'] in excluded:
            continue
        action = ev.get('action')
        package = ev.get('package')
        if package is None:
            continue
        b = box(package)
        if action == 'seal':
            b['seal_id'] = ev['seal_id']
            # 封签被破坏（tamper）则封签链断裂；完好封签建立未开链
            b['seal_intact'] = not ev.get('tamper', False)
            b['seal_events'].append(ev['id'])
            for item in ev.get('items', manifest):
                b['items'].setdefault(item, ev['location'])
        elif action == 'seal_proof':
            b['seal_events'].append(ev['id'])
            if ev.get('intact'):
                # 正式封签未开证明：推翻此前单方扫码形成的临时开封/拆出状态
                b['seal_intact'] = True
                b['opened'] = False
                b['removed'].clear()
            elif b['seal_intact'] is not False:
                b['seal_intact'] = False
        elif action in ('handoff', 'receive', 'return'):
            seed_manifest(b)
            b['custody_timeline'].append(
                (ev['at'], ev['location'], ev.get('to_party') or ev.get('party'), ev['id']))
        elif action == 'open':
            seed_manifest(b)
            b['opened'] = True
            # 开封动作若伴随新封签状态，按采录更新；否则视为封签已开
            if 'seal_id' in ev:
                b['seal_id'] = ev['seal_id']
            b['seal_intact'] = False
            for item in ev.get('items', ()):
                b['items'].setdefault(item, ev['location'])
        elif action == 'unpack':
            seed_manifest(b)
            b['opened'] = True
            b['seal_intact'] = False
            item = ev.get('item')
            if item:
                b['items'].setdefault(item, ev['location'])
                b['removed'].add(item)
        elif action == 'item_handoff':
            item = ev.get('item')
            if item and item in b['items']:
                b['removed'].add(item)
        elif action == 'return_check':
            item = ev.get('item')
            if item:
                b['items'].setdefault(item, ev['location'])
    return boxes


def _canonical(event):
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def open_ledger(path):
    """打开/初始化借展 SQLite 账本。"""
    path = Path(path)
    if str(path) != ':memory:':
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript(LEDGER_SCHEMA)
    return db


def add_loan_events(db, events, source_raw=None):
    """事务化写入采录事件。

    先全量校验再入库；同 id 异内容抛 :class:`ValueError` 并整体回滚，
    同 id 同内容按重放处理。返回插入/重放计数与源文件 sha。
    """
    for ev in events:
        validate_event(ev)
    raw = source_raw if isinstance(source_raw, (bytes, bytearray)) else _canonical(events).encode()
    sha = hashlib.sha256(bytes(raw)).hexdigest()
    inserted = replayed = 0
    db.execute('SAVEPOINT loan_import')
    try:
        for ev in events:
            body = _canonical(ev)
            digest = hashlib.sha256(body.encode()).hexdigest()
            old = db.execute('SELECT sha FROM loan_events WHERE id=?', (ev['id'],)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError(f"same event id {ev['id']!r} with different content")
                replayed += 1
                continue
            db.execute(
                'INSERT INTO loan_events VALUES (?,?,?,?,?,?,?,?)',
                (ev['id'], ev['at'], ev['received_at'], ev['action'],
                 ev.get('package'), ev.get('item'), body, digest))
            inserted += 1
        db.execute('INSERT OR IGNORE INTO loan_sources(sha, raw) VALUES (?,?)', (sha, bytes(raw)))
        db.execute('RELEASE SAVEPOINT loan_import')
        db.commit()
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT loan_import')
        db.execute('RELEASE SAVEPOINT loan_import')
        db.rollback()
        raise
    return {'inserted': inserted, 'replayed': replayed, 'sha': sha}


def ledger_events(db):
    """读取账本内全部事件，按时间顺序返回（解析 canonical body）。"""
    return [json.loads(r['body']) for r in db.execute(
        'SELECT body FROM loan_events ORDER BY at, received_at, id')]
