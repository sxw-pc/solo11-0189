"""逐件保管对账：箱级回执与封签/开封清单合读，不自动覆盖已拆出散页。

核心规则（对应业务约束）：

1. 箱级回执只证明箱本身的位置与责任方；必须与开封时的逐件清单及封签
   状态合读，封签已开或存在拆件记录时，不能把箱位置复制给箱内每件。
2. 每次逐件移交须有交出方、接收方和当时有效的保管责任；单方扫码只能
   记待核（pending），不产生保管转移。
3. 同一实物同一时刻不能同时归两处：直接位置主张冲突时，迟到的一条被
   排除并记录原因，旧去向版本保留，只重算被触及的实物。
4. 迟到回执不抹历史：每个实物保留完整去向版本链，更正以新版本追加。

主结果是实物当前保管人、未闭合交接与返馆缺件清单；不复核修复损伤和验收。
"""
from __future__ import annotations

from .loan_capture import build_boxes, order_events, prepare_events, signed_event

# 旧系统把“最后一张整箱回执”当成逐件结论的合成裁定 id
ALL_RETURNED_INFERENCE = 'inference:whole-crate-returned'

# 去向状态
CONFIRMED = 'confirmed'      # 有双签/盘点/点验支撑的当前保管
PENDING = 'pending'          # 只有单方扫码，待核
UNVERIFIED = 'unverified'    # 箱级动作无法推出逐件位置

# 两路独立补证处置标识
ROUTE_A = 'route_a'          # 拆件属实：F12 仍在实验室，出催回单
ROUTE_B = 'route_b'          # 扫码误归：封签未开，出闭合返馆单


def _new_item_state():
    return {
        'status': UNVERIFIED,
        'location': None,
        'custodian': None,
        'evidence': [],
        'versions': [],
        'unclosed': [],
    }


def _record_version(state, *, at, location, custodian, status, event_id, note):
    """追加一个去向版本；迟到回执只新增版本，绝不改写历史版本。"""
    state['versions'].append({
        'at': at, 'location': location, 'custodian': custodian,
        'status': status, 'event_id': event_id, 'note': note,
    })


def _set_current(state, *, location, custodian, status, event_id, note, at):
    state['status'] = status
    state['location'] = location
    state['custodian'] = custodian
    if event_id and event_id not in state['evidence']:
        state['evidence'].append(event_id)
    _record_version(state, at=at, location=location, custodian=custodian,
                    status=status, event_id=event_id, note=note)


def _close_pending(state, event_id, closure):
    """闭合该实物全部未结的单方待核记录，并从待结列表移除。"""
    for pending in state['unclosed']:
        if not pending.get('closed_by'):
            pending['closed_by'] = event_id
            pending['closure'] = closure
    state['unclosed'] = [u for u in state['unclosed'] if not u.get('closed_by')]


def _direct_claims(events):
    """收集会直接确定逐件位置的事件，用于同物同时刻两处冲突检测。

    箱级返馆本身不是逐件直接主张（它只能在封签未开且无拆件时被合读），
    所以不参与此表；参与的是双签交接、实验室盘点、逐件点验。
    """
    claims = []
    for ev in events:
        action = ev.get('action')
        if action == 'item_handoff' and signed_event(ev):
            claims.append((ev['item'], ev))
        elif action in ('inventory', 'return_check'):
            claims.append((ev['item'], ev))
    return claims


def _conflict_scan(events):
    """同一实物同一时刻两个不同位置主张 -> 迟到者排除，旧版本保留。

    只重算触及物件：返回 (被排除事件 id 集合, 冲突说明列表)。
    """
    excluded = set()
    notes = []
    best = {}
    for item, ev in _direct_claims(events):
        key = (item, ev['at'])
        old = best.get(key)
        if old is None:
            best[key] = ev
            continue
        if old['location'] == ev['location']:
            continue
        # 同物同时刻两处：先到的回执成立，迟到的一条排除
        late, keep = (ev, old) if (ev['received_at'], ev['id']) > (old['received_at'], old['id']) else (old, ev)
        excluded.add(late['id'])
        notes.append({
            'item': item, 'at': ev['at'],
            'kept': keep['id'], 'excluded': late['id'],
            'locations': [keep['location'], late['location']],
            'reason': '同一实物同一时刻不能同时归两处，迟到回执排除，旧去向版本保留',
        })
        best[key] = keep
    return excluded, notes


def _seal_conflict_scan(events):
    """封签状态冲突：同一封签号的正式证据互相矛盾（如未开证明 vs 破坏封签）。

    单方扫码（unpack）只是对封签链的临时性、待核观察：随后的正式
    ``seal_proof``（未开证明）可以推翻它，此时该扫码作为“误归回执”排除。
    正式证据之间（seal 破坏 / seal_proof / open）冲突时，迟到的相反证据
    排除，封签链维持先到版本。两路封签证据互斥在 route 包装器中强校验。
    """
    excluded = set()
    notes = []
    # 正式封签状态断言（不含单方 unpack 扫码，也不含 open：正式开箱是合法状态转换）
    formal = {}
    provisional_unpacks = []
    for ev in order_events(events):
        action = ev.get('action')
        seal_id = ev.get('seal_id')
        if action == 'unpack':
            provisional_unpacks.append(ev)
            continue
        if not seal_id or action not in ('seal', 'seal_proof'):
            continue
        if action == 'seal':
            intact = not ev.get('tamper', False)
        else:
            intact = bool(ev.get('intact'))
        prev = formal.get(seal_id)
        if prev is not None and prev[1] != intact:
            excluded.add(ev['id'])
            notes.append({
                'seal_id': seal_id, 'excluded': ev['id'], 'kept': prev[0]['id'],
                'reason': '封签状态冲突（正式未开证明与破坏封签记录矛盾），迟到相反证据排除，封签链保留先到版本',
            })
        else:
            formal[seal_id] = (ev, intact)

    # 单方拆件扫码只有在“无正式开箱且同箱封签被正式证明未开”时才定性为误归；
    # 若存在正式开箱记录，扫码保留为待核，等待双签交接补证闭合。
    intact_seals = {sid for sid, (_, intact) in formal.items() if intact}
    box_seals = {}
    opened_packages = set()
    for ev in order_events(events):
        if ev.get('seal_id') and ev.get('package'):
            box_seals.setdefault(ev['package'], set()).add(ev['seal_id'])
        if ev.get('action') == 'open':
            opened_packages.add(ev.get('package'))
    for ev in provisional_unpacks:
        package = ev.get('package')
        if package in opened_packages:
            continue
        if intact_seals & box_seals.get(package, set()):
            sid = next(iter(intact_seals & box_seals[package]))
            notes.append({
                'seal_id': sid, 'excluded': ev['id'], 'kept': None,
                'reason': '无正式开箱记录且同箱封签被正式证明未开，单方送检扫码定性为误归回执并排除，不产生待核保管转移',
            })
            excluded.add(ev['id'])
    return excluded, notes


def reconcile(events, manifest, route=None):
    """对一批离线采录事件重算逐件保管对账。

    返回字典含：``custody``（每实物当前保管人/位置/状态/证据/版本链）、
    ``unclosed_handoffs``、``return_shortages``、``excluded_receipts``、
    ``corrections``、``documents``、``boxes``。纯函数，不修改入参事件。
    """
    ordered, dedup_dropped = prepare_events(events)
    excluded, conflict_notes = _conflict_scan(ordered)
    seal_excluded, seal_notes = _seal_conflict_scan(ordered)
    excluded |= seal_excluded
    boxes = build_boxes(ordered, manifest, excluded)
    # 箱级返馆合读失败的排除说明（事件 id -> 说明），不回写、不修改入参事件
    return_exclusions = {}

    items = {item: _new_item_state() for item in manifest}

    def state(item):
        return items.setdefault(item, _new_item_state())

    for ev in ordered:
        if ev['id'] in excluded:
            continue
        action = ev.get('action')
        package = ev.get('package')
        box = boxes.get(package) if package else None

        if action == 'seal':
            for item in ev.get('items', manifest):
                st = state(item)
                _set_current(st, location=ev['location'],
                             custodian=ev.get('party') or ev.get('to_party'),
                             status=CONFIRMED, event_id=ev['id'], at=ev['at'],
                             note='封签封入，逐件清单与箱同责')
        elif action in ('handoff', 'receive'):
            # 箱级责任方转移：只带动仍封在箱内、未拆出的实物
            if box is None:
                continue
            for item, sealed_loc in box['items'].items():
                if item in box['removed']:
                    continue
                st = state(item)
                _set_current(st, location=ev['location'],
                             custodian=ev.get('to_party') or ev.get('party'),
                             status=CONFIRMED, event_id=ev['id'], at=ev['at'],
                             note='箱级责任方转移，实物封签在箱内随箱同责')
        elif action == 'open':
            if box is None:
                continue
            for item in box['items']:
                # 只有已拆出的实物才断开箱级合读链；未拆出件继续随箱同责
                if item not in box['removed']:
                    continue
                st = state(item)
                _set_current(st, location=None, custodian=None,
                             status=UNVERIFIED, event_id=ev['id'], at=ev['at'],
                             note='开箱拆封且该件已拆出，箱级回执不再能推出其位置，待逐件交接')
        elif action == 'unpack':
            # 单方送检扫码：只能记待核，不能当成保管责任转移
            item = ev.get('item')
            if not item:
                continue
            st = state(item)
            st['unclosed'].append({
                'item': item, 'event_id': ev['id'], 'at': ev['at'],
                'kind': 'single_party_scan',
                'from_party': ev.get('from_party') or ev.get('party'),
                'to_party': None,
                'reason': '单方扫码无双方签收，记待核，不构成保管责任转移',
            })
            _set_current(st, location=ev['location'], custodian=None,
                         status=PENDING, event_id=ev['id'], at=ev['at'],
                         note='单方送检扫码，待核')
        elif action == 'item_handoff':
            item = ev['item']
            st = state(item)
            if not signed_event(ev):
                st['unclosed'].append({
                    'item': item, 'event_id': ev['id'], 'at': ev['at'],
                    'kind': 'single_party_scan',
                    'from_party': ev.get('from_party'), 'to_party': ev.get('to_party'),
                    'reason': '缺少接收方/交出方签署，单方扫码只能记待核',
                })
                _set_current(st, location=ev['location'], custodian=None,
                             status=PENDING, event_id=ev['id'], at=ev['at'],
                             note='逐件交接缺双签，待核')
                continue
            # 双签：还须交出方在 at 时刻持有有效保管责任
            current_party = st['custodian']
            if current_party is not None and current_party != ev['from_party']:
                st['unclosed'].append({
                    'item': item, 'event_id': ev['id'], 'at': ev['at'],
                    'kind': 'custody_mismatch',
                    'from_party': ev['from_party'], 'to_party': ev['to_party'],
                    'reason': f"交出方 {ev['from_party']} 当时不持有有效保管责任（在册：{current_party}）",
                })
                continue
            # 闭合此前的单方待核记录
            _close_pending(st, ev['id'], '双签交接补证闭合')
            _set_current(st, location=ev['location'], custodian=ev['to_party'],
                         status=CONFIRMED, event_id=ev['id'], at=ev['at'],
                         note=f"双签逐件交接：{ev['from_party']} -> {ev['to_party']}")
        elif action == 'inventory':
            item = ev['item']
            st = state(item)
            _close_pending(st, ev['id'], '责任方在库盘点确认在册，待核扫码闭合')
            _set_current(st, location=ev['location'], custodian=ev['party'],
                         status=CONFIRMED, event_id=ev['id'], at=ev['at'],
                         note='责任方在库盘点，确认实物在册')
        elif action == 'return_check':
            item = ev['item']
            st = state(item)
            _close_pending(st, ev['id'], '返馆逐件点验原件，待核扫码闭合')
            _set_current(st, location=ev['location'],
                         custodian=ev.get('party') or ev.get('to_party'),
                         status=CONFIRMED, event_id=ev['id'], at=ev['at'],
                         note='返馆逐件点验原件，确认在册')
        elif action == 'seal_proof':
            # 仅更新封签链；逐件结论留给 return 合读或 return_check
            continue
        elif action == 'return':
            _apply_box_return(ev, box, items, return_exclusions, manifest)
        elif action == 'correction':
            _apply_correction(ev, items)

    # 汇总被排除回执（去重冲突 + 封签冲突 + 合读排除 + 采录去重）
    excluded_receipts = [
        {'event_id': n['excluded'], 'reason': n['reason'],
         'detail': {k: v for k, v in n.items() if k not in ('excluded', 'reason')}}
        for n in conflict_notes + seal_notes]
    for event_id, note in return_exclusions.items():
        excluded_receipts.append({'event_id': event_id, 'reason': note[0],
                                  'detail': {'package': note[1]}})
    for dropped_id in dedup_dropped:
        excluded_receipts.append({
            'event_id': dropped_id,
            'reason': '同一实物/箱同动作重复采录，按双签优先、先到优先保留一条',
            'detail': {'dedup': True}})

    custody = {item: _custody_view(st) for item, st in items.items()}
    unclosed = [u for st in items.values() for u in st['unclosed']]
    shortages = _shortages(items, boxes, manifest)
    corrections = [ev for ev in ordered if ev.get('action') == 'correction']
    documents = _documents(route, items, boxes, manifest, corrections, excluded_receipts)

    return {
        'route': route,
        'custody': custody,
        'unclosed_handoffs': unclosed,
        'return_shortages': shortages,
        'excluded_receipts': excluded_receipts,
        'corrections': [_correction_view(ev) for ev in corrections],
        'documents': documents,
        'boxes': {pid: _box_view(b) for pid, b in boxes.items()},
    }


def _apply_box_return(ev, box, items, return_exclusions, manifest):
    """整箱返馆扫描与封签状态、开封逐件清单合读。

    * 封签未开且无拆件：箱内逐件随箱确认返馆；
    * 曾开箱但该件从未拆出（仍在开封逐件清单内）：返馆扫描与清单合读确认；
    * 已拆出实物（removed）：箱级回执不能覆盖其逐件去向，只登记箱本身返馆，
      阻断说明写入 excluded_receipts，实物维持各自逐件结论，旧版本保留。
    """
    if box is None:
        return
    seal_ok = box['seal_id'] is not None and box['seal_intact'] and not box['opened']
    blocked = []
    for item in box['items']:
        st = items.setdefault(item, _new_item_state())
        if item not in box['removed']:
            _set_current(st, location=ev['location'],
                         custodian=ev.get('to_party') or ev.get('party'),
                         status=CONFIRMED, event_id=ev['id'], at=ev['at'],
                         note=('封签未开且无拆件，箱级返馆与封签清单合读确认返馆'
                               if seal_ok else
                               '开箱逐件清单显示该件从未拆出，返馆扫描与清单合读确认返馆'))
        else:
            blocked.append(item)
            _record_version(st, at=ev['at'], location=ev['location'],
                            custodian=ev.get('to_party') or ev.get('party'),
                            status=st['status'], event_id=ev['id'],
                            note='迟到箱级回执仅留痕：不覆盖现有逐件去向，旧版本保留')
    if blocked:
        return_exclusions[ev['id']] = (
            '整箱返馆为箱级回执：存在已拆出实物，不能自动覆盖其逐件去向，'
            f'仅登记箱本身返馆（被阻断件：{blocked}），该件须凭双签交接或逐件点验认定',
            box['package'])


def _apply_correction(ev, items):
    """纠错回执：为先前错误结论出更正版本，只重算被触及实物。"""
    targets = ev.get('items') or sorted(items.keys())
    for item in targets:
        st = items.setdefault(item, _new_item_state())
        # 纠错可闭合指向该件的未结待核
        _close_pending(st, ev['id'], '纠错回执闭合')
        _record_version(st, at=ev['at'], location=st['location'],
                        custodian=st['custodian'], status=st['status'],
                        event_id=ev['id'],
                        note=f"更正版本：{ev.get('supersedes')} -> {ev.get('correct_result')}")


def _custody_view(st):
    return {
        'status': st['status'],
        'location': st['location'],
        'custodian': st['custodian'],
        'evidence': list(st['evidence']),
        'versions': list(st['versions']),
    }


def _box_view(box):
    return {
        'package': box['package'],
        'seal_id': box['seal_id'],
        'seal_intact': box['seal_intact'],
        'opened': box['opened'],
        'items': sorted(box['items'].keys()),
        'removed': sorted(box['removed']),
        'custody_timeline': [
            {'at': at, 'location': loc, 'party': party, 'event_id': eid}
            for at, loc, party, eid in box['custody_timeline']],
    }


def _correction_view(ev):
    return {
        'event_id': ev['id'],
        'supersedes': ev.get('supersedes'),
        'correct_result': ev.get('correct_result'),
        'items': ev.get('items'),
        'note': ev.get('note', '对先前错误全返记录出具更正版本，旧结论留痕不删除'),
    }


def _shortages(items, boxes, manifest):
    """返馆缺件：清单实物当前不在 museum（含待核/未核实/确认在他处）。"""
    out = []
    for item in manifest:
        st = items.get(item)
        if st is None:
            out.append({'item': item, 'status': UNVERIFIED, 'location': None,
                        'custodian': None, 'reason': '清单内无任何采录证据'})
        elif st['location'] != 'museum' or st['status'] != CONFIRMED:
            out.append({'item': item, 'status': st['status'],
                        'location': st['location'], 'custodian': st['custodian'],
                        'reason': '未见封签未开合读或逐件点验确认其返馆'})
    return out


def _documents(route, items, boxes, manifest, corrections, excluded):
    if route is None:
        return []
    docs = []
    correction_ids = [ev['id'] for ev in corrections]
    if route == ROUTE_A:
        still_out = [s['item'] for s in _shortages(items, boxes, manifest)]
        if still_out:
            docs.append({
                'type': '返馆缺件催回单',
                'route': ROUTE_A,
                'missing': still_out,
                'confirmed_returned': [i for i in manifest if i not in still_out],
                'correction_ids': correction_ids,
                'basis': '实验室与展馆双签开箱交接 + 实验室在库盘点；箱级返馆回执对拆出件排除',
            })
    elif route == ROUTE_B:
        if not _shortages(items, boxes, manifest):
            docs.append({
                'type': '闭合返馆单',
                'route': ROUTE_B,
                'all_returned': list(manifest),
                'correction_ids': correction_ids,
                'basis': '返馆时封签未开证明 + 逐件点验原件 + 实验室纠错回执；送检扫码系误归 C1',
            })
    return docs


# ---- 两路独立补证处置的证据纪律 -------------------------------------------

ROUTE_REQUIRED = {
    ROUTE_A: {'seal', 'open', 'item_handoff', 'inventory', 'return', 'correction'},
    ROUTE_B: {'seal', 'seal_proof', 'return_check', 'inventory', 'correction'},
}
ROUTE_FORBIDDEN = {
    # A 路靠真实拆件，不得使用“封签未开证明”
    ROUTE_A: {'seal_proof'},
    # B 路主张送检扫码误归，不得存在 F12 的双签开箱交接（那是 A 路的封签/拆件证据）
    ROUTE_B: {'item_handoff'},
}


def _route_events(base_events, supplement, route):
    """合一路补证事件并强校验证据纪律：两路封签/拆件证据互斥。"""
    combined = list(base_events) + list(supplement)
    actions = {ev.get('action') for ev in combined}
    missing = ROUTE_REQUIRED[route] - actions
    if missing:
        raise ValueError(f'{route} 补证不足，缺少动作：{sorted(missing)}')
    forbidden = ROUTE_FORBIDDEN[route] & actions
    if forbidden:
        raise ValueError(
            f'{route} 不得引用另一路的证据，禁止动作：{sorted(forbidden)}；'
            '封签证据不得在两路间挪移')
    return combined


def route_a_disposition(base_events, supplement_events, manifest):
    """处置一（拆件属实）：双签 F12 开箱交接 + 实验室盘点 -> 催回单。

    证明 V1 已返、F12 仍在实验室；对先前错误的全返记录出更正版本，
    并解释被排除的冲突/箱级回执。
    """
    combined = _route_events(base_events, supplement_events, ROUTE_A)
    result = reconcile(combined, manifest, route=ROUTE_A)
    _require_document(result, '返馆缺件催回单')
    return result


def route_b_disposition(base_events, supplement_events, manifest):
    """处置二（扫码误归）：封签未开证明 + 逐件点验 + 实验室纠错 -> 闭合返馆单。

    证明送检扫码误归 C1，V1/F12 均已返馆；同样对错误全返记录出更正版本。
    """
    combined = _route_events(base_events, supplement_events, ROUTE_B)
    result = reconcile(combined, manifest, route=ROUTE_B)
    _require_document(result, '闭合返馆单')
    return result


def _require_document(result, doc_type):
    if not any(d['type'] == doc_type for d in result['documents']):
        raise ValueError(f'{doc_type} 未能生成：补证链不闭合')


def whereabouts(events, manifest):
    """逐件当前去向（旧接口保留，语义已修正）。

    返回 ``{item: location}``；待核或无法由箱级回执推出的件位置为 ``None``，
    调用方可改用 :func:`reconcile` 获取状态、责任方与版本链。
    """
    result = reconcile(events, manifest)
    return {item: view['location'] for item, view in result['custody'].items()}
