"""联合采购账：批次证书、起订量、确认运力、有效期与到货签收联合核对。

旧逻辑 ``min(unit_price)`` 把 V1 的 50 元单价当成五张的完整报价，无视其
常规到货 5 月 5 日晚于 5 月 3 日工期、起订量 10 张以及加急运力。本模块以
:mod:`bookconserve.supplier_offers` 的真实批次/交付能力为输入，以跨工单需求
驱动总采购量、余料归属与迟到补单责任：

* 证书不符或未核验的批次不得订作等效材料（V3 9gsm 永不入围）；
* 工期运力与实际起订量必须同时锁定，供应商局部确认不得当全单交付；
* 整批多买的余料独立入库存，不能全记到单张工单消耗，跨项目转用保留独立
  领用链（RC 链）与批次有效期；
* 已签采购单迟到或缺货时保留原合同、原供应商与原批次，补单挂接原单，
  绝不静默改供应商。
"""
from .supplier_offers import (
    offers_for, certificate_matches, quote_is_valid, lot_is_usable,
    feasible_modes, delivery_modes, can_lock,
)


def _demand(demand):
    return {
        'work_order': demand['work_order'],
        'material': demand['material'],
        'gsm': demand['gsm'],
        'needed': int(demand['needed']),
        'needed_by': demand['needed_by'],
    }


def _ordered(demands):
    return sorted((_demand(d) for d in demands),
                  key=lambda d: (d['needed_by'], d['work_order']))


def usable_stock(stock, material, gsm, use_by):
    """库存中批次规格相符且在领用日未到期的可用余料。"""
    return [
        s for s in stock
        if s['material'] == material and s.get('gsm') == gsm
        and lot_is_usable({'lot_expiry': s.get('lot_expiry')}, use_by)
        and s.get('quantity', 0) > 0
    ]


def _reserve_stock(demands, stock, counters=None):
    """先按先到期先出（FEFO）占用库存，返回独立领用记录、剩余需求与剩余库存。

    领用链按工单各自编号（RC-<工单>-NNN），跨修复项目转用时链路彼此独立。
    """
    counters = counters if counters is not None else {}
    pool = sorted((dict(s) for s in stock),
                  key=lambda s: (s.get('lot_expiry') or '9999-12-31', s['lot']))
    requisitions, remaining = [], []
    for d in demands:
        need = d['needed']
        for s in pool:
            if need == 0:
                break
            if s['material'] != d['material'] or s.get('gsm') != d['gsm']:
                continue
            if not lot_is_usable({'lot_expiry': s.get('lot_expiry')},
                                 d['needed_by']):
                continue
            take = min(need, s['quantity'])
            if take <= 0:
                continue
            s['quantity'] -= take
            need -= take
            counters[d['work_order']] = counters.get(d['work_order'], 0) + 1
            requisitions.append({
                'chain_id': f"RC-{d['work_order']}-{counters[d['work_order']]:03d}",
                'work_order': d['work_order'],
                'material': d['material'],
                'gsm': d['gsm'],
                'quantity': take,
                'source': 'stock',
                'lot': s['lot'],
                'po_id': None,
                'unit_cost': s.get('unit_cost'),
            })
        if need > 0:
            leftover = dict(d)
            leftover['needed'] = need
            remaining.append(leftover)
    return requisitions, remaining, [s for s in pool if s['quantity'] > 0]


def _purchase_order(offer, mode, quantity, remaining_demands, counters):
    serves, requisitions = [], []
    outstanding = quantity
    for d in remaining_demands:
        take = min(outstanding, d['needed'])
        if take <= 0:
            continue
        outstanding -= take
        counters[d['work_order']] = counters.get(d['work_order'], 0) + 1
        serves.append({'work_order': d['work_order'], 'quantity': take,
                       'needed_by': d['needed_by']})
        requisitions.append({
            'chain_id': f"RC-{d['work_order']}-{counters[d['work_order']]:03d}",
            'work_order': d['work_order'],
            'material': d['material'],
            'gsm': d['gsm'],
            'quantity': take,
            'source': 'purchase',
            'lot': offer['lot'],
            'po_id': f"PO-{offer['id']}-{mode['mode']}-001",
            'unit_cost': offer['unit_price'],
        })
    goods_value = quantity * offer['unit_price']
    po = {
        'po_id': f"PO-{offer['id']}-{mode['mode']}-001",
        'offer_id': offer['id'],
        'supplier': offer['supplier'],
        'material': offer['material'],
        'gsm': offer['gsm'],
        'lot': offer['lot'],
        'certificate_doc': offer['certificate']['doc'],
        'mode': mode['mode'],
        'arrival': mode['arrival'],
        'needed_by': min(d['needed_by'] for d in remaining_demands),
        'quantity': quantity,
        'minimum': offer['minimum'],
        'confirmed_capacity': mode['confirmed_capacity'],
        'unit_price': offer['unit_price'],
        'goods_value': goods_value,
        'expedite_fee': mode['fee'],
        'total_cash': goods_value + mode['fee'],
        'quote_valid_until': offer.get('quote_valid_until'),
        'lot_expiry': offer.get('lot_expiry'),
        'serves': serves,
    }
    surplus = []
    if outstanding > 0:
        surplus.append({
            'material': offer['material'],
            'gsm': offer['gsm'],
            'lot': offer['lot'],
            'offer_id': offer['id'],
            'supplier': offer['supplier'],
            'quantity': outstanding,
            'lot_expiry': offer.get('lot_expiry'),
            'carrying_value': outstanding * offer['unit_price'],
        })
    return po, requisitions, surplus


def plan_routes(offers, demands, stock=None, as_of=None):
    """枚举全部联合可行的获授权采购路线。

    每条路线是一份完整采购账：正式采购单、总现金支出、逐工单独立领用链
    （库存领用与采购领用以 RC 链分别留痕）、可继续使用的余料及未满足需求。
    只有证书核验、报价有效期、到货工期、MOQ 与已确认运力全部通过的路线
    才会出现；被拦下的报价见 :func:`explain_blocks`。
    """
    demands = _ordered(demands)
    materials = {d['material'] for d in demands}
    if len(materials) != 1:
        raise ValueError('联合采购账按单一材料分组规划，请分材料调用')
    material = next(iter(materials))
    gsm = demands[0]['gsm']
    stock = stock or []
    counters = {}
    stock_req, remaining, leftover_stock = _reserve_stock(
        demands, stock, counters)
    routes = []

    if not remaining:
        routes.append({
            'route_id': 'STOCK',
            'feasible': True,
            'purchase_orders': [],
            'total_cash': 0,
            'requisitions': stock_req,
            'surplus': leftover_stock,
            'unmet_demands': [],
        })
        return routes

    earliest = min(d['needed_by'] for d in remaining)
    latest = max(d['needed_by'] for d in remaining)
    total_need = sum(d['needed'] for d in remaining)
    for offer in offers_for(offers, material, gsm, as_of):
        for mode in feasible_modes(offer, earliest):
            order_qty = max(total_need, offer['minimum'])
            if not can_lock(offer, mode['mode'], order_qty, earliest,
                            use_by=latest):
                continue
            po, purchase_req, surplus = _purchase_order(
                offer, mode, order_qty, remaining, dict(counters))
            routes.append({
                'route_id': f"{offer['id']}:{mode['mode']}",
                'feasible': True,
                'purchase_orders': [po],
                'total_cash': po['total_cash'],
                'requisitions': stock_req + purchase_req,
                'surplus': leftover_stock + surplus,
                'unmet_demands': [],
            })
    routes.sort(key=lambda r: (r['total_cash'], r['route_id']))
    return routes


def explain_blocks(offers, demands, stock=None, as_of=None):
    """说明每张报价为什么无法形成联合可行路线（审计用，不改变采购账）。"""
    demands = _ordered(demands)
    material = demands[0]['material']
    gsm = demands[0]['gsm']
    _, remaining, _ = _reserve_stock(demands, stock or [])
    if not remaining:
        return []
    earliest = min(d['needed_by'] for d in remaining)
    latest = max(d['needed_by'] for d in remaining)
    total_need = sum(d['needed'] for d in remaining)
    blocks = []
    for offer in offers:
        if offer.get('material') != material:
            continue
        if not certificate_matches(offer, material, gsm):
            blocks.append({'offer_id': offer['id'],
                           'reason': '批次克重证明未核验或与修复规格不符，不得订作等效材料'})
            continue
        if as_of is not None and not quote_is_valid(offer, as_of):
            blocks.append({'offer_id': offer['id'], 'reason': '报价单已过有效期'})
            continue
        # 诊断必须遍历全部运力：晚于工期的常规运力同样要留痕，不能只看可行集
        required = max(total_need, offer['minimum'])
        for mode in delivery_modes(offer):
            if mode['arrival'] > earliest:
                reason = '运力到货晚于工单工期'
            elif required > mode['confirmed_capacity']:
                reason = '供应商已确认运力不足，局部确认不得当全单交付'
            elif not lot_is_usable(offer, mode['arrival']) or not lot_is_usable(offer, latest):
                reason = '批次在到货或最晚领用日前到期'
            else:
                continue
            blocks.append({'offer_id': offer['id'], 'mode': mode['mode'],
                           'reason': reason})
    return blocks


def choose(offers, demands, stock=None, as_of=None):
    """显式政策选择器：在联合可行路线中取总现金占用最低者，返回路线 ID。

    注意这是“全部硬约束通过后的最低总现金”，不是旧的最低单价——V1 常规
    运力因晚于工期根本不入围，V1 加急 600 与 V2 常规 750 才是可比对象。
    """
    routes = plan_routes(offers, demands, stock=stock, as_of=as_of)
    if not routes:
        raise ValueError('无联合可行采购路线，不得在硬约束缺口下下单')
    return routes[0]['route_id']


def confirm_supply(purchase_order, confirmed_quantity):
    """供应商运力确认回执：只有全量确认才能锁定订单，局部确认保持未锁定。"""
    confirmed_quantity = int(confirmed_quantity)
    return {
        'po_id': purchase_order['po_id'],
        'supplier': purchase_order['supplier'],
        'lot': purchase_order['lot'],
        'confirmed_quantity': confirmed_quantity,
        'required_quantity': purchase_order['quantity'],
        'locked': confirmed_quantity >= purchase_order['quantity'],
        'shortfall': max(0, purchase_order['quantity'] - confirmed_quantity),
    }


def receive(purchase_order, delivered_quantity, received_on):
    """到货签收：保留原合同与原供应商，迟到记违约，缺货挂同供应商补单。

    签收不接受换供应商：补单强制沿用原 PO 的供应商、批次与运力，采购方
    另行追责，系统不静默改写合同主体。
    """
    delivered_quantity = int(delivered_quantity)
    if delivered_quantity < 0 or delivered_quantity > purchase_order['quantity']:
        raise ValueError('签收数量超出原采购单范围')
    shortfall = purchase_order['quantity'] - delivered_quantity
    if shortfall:
        status = 'short'
    elif received_on > purchase_order['arrival']:
        status = 'late'
    else:
        status = 'on_time'
    receipt = {
        'po_id': purchase_order['po_id'],
        'supplier': purchase_order['supplier'],
        'lot': purchase_order['lot'],
        'mode': purchase_order['mode'],
        'ordered_quantity': purchase_order['quantity'],
        'delivered_quantity': delivered_quantity,
        'received_on': received_on,
        'contracted_arrival': purchase_order['arrival'],
        'status': status,
    }
    replenishment = None
    if shortfall:
        replenishment = {
            'po_id': f"{purchase_order['po_id']}-R1",
            'kind': 'replenishment',
            'linked_po': purchase_order['po_id'],
            'offer_id': purchase_order['offer_id'],
            'supplier': purchase_order['supplier'],
            'lot': purchase_order['lot'],
            'mode': purchase_order['mode'],
            'quantity': shortfall,
            'unit_price': purchase_order['unit_price'],
            'serves': purchase_order['serves'],
            'reason': '原单缺货，按原合同同供应商同批次补单，不得静默改供应商',
        }
    return {'purchase_order': dict(purchase_order), 'receipt': receipt,
            'replenishment': replenishment}
