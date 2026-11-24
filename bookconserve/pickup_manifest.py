"""清运清单：兼容表核证、容量与分类双门控、双签及签收后不可逆修订。

硬约束：
1. 内部兼容表禁止含溶剂清洁垫与湿纸浆同桶；容量合格不能替代分类核证。
2. 司机签收后的原清单不可覆盖；后到检测改变分类时，只能生成带关联链的
   追回/分装或更正清运单，并保留原签发时间与责任人。
"""
from .waste_capture import WasteLedger, weight_for

# 内部兼容表：列出允许同桶混装的类别对（对称关系）。
# 含溶剂清洁垫（solvent-pads）刻意不在表内——它与任何湿态有机质都不可同桶。
MIXING_COMPATIBILITY = {
    frozenset({'wet-cellulose', 'water-based-pads'}),
}

ISSUE = 'issue'                 # 馆方签发清运清单
SIGN = 'sign'                   # 司机（清运方）双签接收（清单事件）
LEGACY_SIGN = 'legacy_sign'     # 旧“仅看容量”规则留下的既成签收（只记录，不放行）
FREEZE = 'freeze'               # 冻结已签收清单（追回前置）
RECALL = 'recall'               # 追回/分装指令（关联原清单）
CORRECT_ISSUE = 'correct_issue'  # 更正清运单（关联原清单，保留原签发证据）

DELIVERABLE = '可交付'
BLOCKED = '暂缓交付'
FROZEN = '已冻结待追回'
DELIVERED = '司机已签收'
LEGACY_PENDING = '遗留签收待核'
REVISED = '已由关联更正单替代'


def categories_compatible(categories):
    """兼容表核证：容器内所有类别两两必须在内部兼容表内。

    单一类别天然合规；未核证/争议分类直接判不合规。
    """
    categories = set(categories)
    if len(categories) <= 1:
        return True
    checked = set()
    for left in categories:
        for right in categories:
            pair = frozenset((left, right))
            if left == right or pair in checked:
                continue
            checked.add(pair)
            if pair not in MIXING_COMPATIBILITY:
                return False
    return True


class ManifestError(ValueError):
    """清单事件违反签发或修订约束。"""


class PickupLedger:
    """在实物账本之上重算清运清单及其修订链。"""

    def __init__(self, events=()):
        self.waste = WasteLedger()
        self.manifests = {}          # 清单号 -> 清单状态
        self._seal_to_manifest = {}
        for event in events:
            self.apply(event)

    def apply(self, event):
        action = event.get('action')
        d = event.get('data', {})
        if action == SIGN and 'manifest_id' in d:
            # 先核清单门控，再落实物签收，保证非法事件不产生半笔投影。
            self._validate_sign(event)
            self.waste.apply(event)
            self._on_sign(event)
        elif action in (ISSUE, LEGACY_SIGN, FREEZE, RECALL, CORRECT_ISSUE):
            handler = getattr(self, f'_on_{action}')
            handler(event)
        else:
            # 来源/分类/称重/封签/拆封/分装/物理签收交给实物账本。
            self.waste.apply(event)
        return self

    # ------------------------------------------------------------------ #
    # 清单事件
    # ------------------------------------------------------------------ #
    def _on_issue(self, event):
        d = event['data']
        for key in ('manifest_id', 'container', 'capacity_kg', 'museum_party'):
            if key not in d:
                raise ManifestError(f"事件 {event['id']} 缺少字段 {key}")
        if d['manifest_id'] in self.manifests:
            raise ManifestError(f"清单号 {d['manifest_id']} 重复签发")
        contents = self.waste.container_contents(d['container'])
        if contents is None:
            raise ManifestError(f"容器 {d['container']} 无实物，不能签发清单")
        check = evaluate_container(self.waste, d['container'], d['capacity_kg'])
        manifest = {
            'manifest_id': d['manifest_id'],
            'container': d['container'],
            'seal_id': next((h.get('seal_id') for h in
                             self.waste.containers[d['container']]['history']
                             if h.get('seal_id') and h['action'] == 'seal'), None),
            'issued_at': event['at'],
            'issuer': d['museum_party'],
            'capacity_kg': d['capacity_kg'],
            'status': DELIVERABLE if check['ok'] else BLOCKED,
            'reasons': list(check['reasons']),
            'contents_at_issue': check['contents'],
            'signed_at': None,
            'driver': None,
            'superseded_by': None,
            'linked_from': d.get('linked_from'),
            'revision_note': d.get('note'),
            'history': [{'event': event['id'], 'at': event['at'], 'action': ISSUE}],
        }
        self.manifests[d['manifest_id']] = manifest
        if manifest['seal_id']:
            self._seal_to_manifest[manifest['seal_id']] = d['manifest_id']

    def _validate_sign(self, event):
        d = event['data']
        for key in ('manifest_id', 'driver'):
            if key not in d:
                raise ManifestError(f"事件 {event['id']} 缺少字段 {key}")
        manifest = self._manifest(d['manifest_id'], event['id'])
        if manifest['status'] != DELIVERABLE:
            raise ManifestError(
                f"清单 {d['manifest_id']} 当前状态 {manifest['status']}，"
                "分类或兼容核证未通过，司机不得签收")
        if manifest['signed_at'] is not None:
            raise ManifestError(f"清单 {d['manifest_id']} 已签收，不可重复签收或覆盖")
        container = self.waste.containers[manifest['container']]
        if not container['sealed']:
            raise ManifestError(
                f"容器 {manifest['container']} 封签已不存在，不能向司机交接")

    def _on_sign(self, event):
        d = event['data']
        manifest = self._manifest(d['manifest_id'], event['id'])
        manifest['status'] = DELIVERED
        manifest['signed_at'] = event['at']
        manifest['driver'] = d['driver']
        manifest['history'].append({'event': event['id'], 'at': event['at'],
                                    'action': SIGN, 'driver': d['driver']})

    def _on_legacy_sign(self, event):
        """如实记录旧“仅看容量”规则下的既成司机签收。

        新核证规则不承认其交付效力：清单进入遗留签收待核，必须冻结、追回
        并以更正清运单收口；原签发时间与责任司机原样保留。
        """
        d = event['data']
        for key in ('manifest_id', 'driver', 'museum_party'):
            if key not in d:
                raise ManifestError(f"事件 {event['id']} 缺少字段 {key}")
        manifest = self._manifest(d['manifest_id'], event['id'])
        if manifest['signed_at'] is not None:
            raise ManifestError(f"清单 {d['manifest_id']} 已有签收记录")
        manifest['signed_at'] = event['at']
        manifest['driver'] = d['driver']
        manifest['status'] = LEGACY_PENDING
        manifest['history'].append({'event': event['id'], 'at': event['at'],
                                    'action': LEGACY_SIGN, 'driver': d['driver']})

    def _on_freeze(self, event):
        d = event['data']
        manifest = self._manifest(d['manifest_id'], event['id'])
        if manifest['signed_at'] is None:
            raise ManifestError(f"清单 {d['manifest_id']} 未经司机签收，无需冻结")
        if manifest['status'] not in (DELIVERED, FROZEN, LEGACY_PENDING):
            raise ManifestError(f"清单 {d['manifest_id']} 状态 {manifest['status']} 不可冻结")
        manifest['status'] = FROZEN
        manifest['history'].append({'event': event['id'], 'at': event['at'], 'action': FREEZE,
                                    'by': d.get('museum_party')})

    def _on_recall(self, event):
        d = event['data']
        manifest = self._manifest(d['manifest_id'], event['id'])
        if manifest['status'] != FROZEN:
            raise ManifestError(
                f"追回前必须先冻结清单 {d['manifest_id']}（当前 {manifest['status']}）")
        for key in ('recall_id', 'seal_id'):
            if key not in d:
                raise ManifestError(f"事件 {event['id']} 缺少字段 {key}")
        # 追认司机实际持有的封签桶：追回的封签号必须是原清单签发时登记的实物封签。
        if d['seal_id'] != manifest['seal_id']:
            raise ManifestError(
                f"追回封签 {d['seal_id']} 与司机实际持有的原封签 "
                f"{manifest['seal_id']} 不符，不能凭补录制造实物")
        manifest['history'].append({'event': event['id'], 'at': event['at'], 'action': RECALL,
                                    'recall_id': d['recall_id'], 'seal_id': d['seal_id']})

    def _on_correct_issue(self, event):
        d = event['data']
        for key in ('manifest_id', 'linked_from', 'container', 'capacity_kg', 'museum_party'):
            if key not in d:
                raise ManifestError(f"事件 {event['id']} 缺少字段 {key}")
        original = self._manifest(d['linked_from'], event['id'])
        if original['signed_at'] is None:
            raise ManifestError(
                f"原清单 {d['linked_from']} 未经司机签收；签收前的分类变化应直接"
                "拆封分装并签发新单，不需要更正清运单")
        if d['manifest_id'] in self.manifests:
            raise ManifestError(f"更正单号 {d['manifest_id']} 重复")
        # 原清单不可覆盖：只登记关联替代关系，原签发时间、责任司机与封签原样保留。
        # 一次追回后可分装出多个新桶，因此一个原单可被多张更正单关联。
        if original['superseded_by'] is None:
            original['superseded_by'] = []
        original['superseded_by'].append(d['manifest_id'])
        original['status'] = REVISED
        original['history'].append({'event': event['id'], 'at': event['at'],
                                    'action': CORRECT_ISSUE, 'new_manifest': d['manifest_id']})
        contents = self.waste.container_contents(d['container'])
        check = evaluate_container(self.waste, d['container'], d['capacity_kg'])
        if not check['ok']:
            raise ManifestError(
                f"更正单 {d['manifest_id']} 对应容器仍未通过核证：{check['reasons']}")
        corrected = {
            'manifest_id': d['manifest_id'],
            'container': d['container'],
            'seal_id': next((h.get('seal_id') for h in
                             self.waste.containers[d['container']]['history']
                             if h.get('seal_id') and h['action'] == 'seal'), None),
            'issued_at': event['at'],
            'issuer': d['museum_party'],
            'capacity_kg': d['capacity_kg'],
            'status': DELIVERABLE,
            'reasons': [],
            'contents_at_issue': check['contents'],
            'signed_at': None,
            'driver': None,
            'superseded_by': None,
            'linked_from': d['linked_from'],
            'revision_note': d.get('note'),
            'history': [{'event': event['id'], 'at': event['at'], 'action': CORRECT_ISSUE}],
        }
        self.manifests[d['manifest_id']] = corrected

    def _manifest(self, manifest_id, event_id):
        manifest = self.manifests.get(manifest_id)
        if manifest is None:
            raise ManifestError(f"事件 {event_id} 引用了不存在的清单 {manifest_id}")
        return manifest

    # ------------------------------------------------------------------ #
    # 投影
    # ------------------------------------------------------------------ #
    def delivery_status(self, container_id, capacity_kg):
        """容器当前可交付清运状态。"""
        return evaluate_container(self.waste, container_id, capacity_kg)

    def manifest_revision_chain(self, manifest_id):
        """返回清单的完整修订关系（原单 -> 更正单），含原签发时间与责任人。"""
        root = self.manifests.get(manifest_id)
        if root is None:
            return []
        # 先沿 linked_from 回到最早的原始清单。
        seen = set()
        root = self.manifests[manifest_id]
        while root['linked_from'] and root['linked_from'] not in seen:
            seen.add(root['manifest_id'])
            root = self.manifests[root['linked_from']]

        def walk(manifest):
            node = {
                'manifest_id': manifest['manifest_id'],
                'container': manifest['container'],
                'status': manifest['status'],
                'issued_at': manifest['issued_at'],
                'issuer': manifest['issuer'],
                'signed_at': manifest['signed_at'],
                'driver': manifest['driver'],
                'linked_from': manifest['linked_from'],
                'superseded_by': manifest['superseded_by'],
                'revision_note': manifest['revision_note'],
            }
            children = manifest['superseded_by'] or []
            node['corrections'] = [walk(self.manifests[m]) for m in children]
            return node

        return [walk(root)]

    def report(self):
        return {
            'containers': [self.delivery_status(c, ctn.get('capacity_kg') or 10)
                           for c, ctn in self.waste.containers.items()
                           if ctn['batches']],
            'quarantine': self.waste.quarantine_batches(),
            'manifests': list(self.manifests.values()),
        }


def evaluate_container(ledger, container_id, capacity_kg):
    """容量 + 分类核证双门控；容量合格永远不能替代分类核证。"""
    contents = ledger.container_contents(container_id)
    reasons = []
    if contents is None:
        return {'container': container_id, 'ok': False, 'status': BLOCKED,
                'reasons': ['容器无实物'], 'contents': None}
    # 门控一：分类核证。任何批次检测证据缺失或争议未决，整桶隔离。
    uncertified = [item['batch'] for item in contents['items']
                   if item['classification'] in (None, 'pending', 'disputed')]
    if uncertified:
        reasons.append(f"批次 {uncertified} 分类检测证据不完整或存在争议，须隔离待核")
    # 门控二：内部兼容表。
    categories = [item['category'] for item in contents['items']]
    if not categories_compatible(categories):
        reasons.append(
            f"类别组合 {sorted(set(categories))} 违反内部兼容表："
            "含溶剂清洁垫禁止与湿纸浆同桶")
    # 门控三：容量（即使 9≤10 也不能救回前两门控）。
    if contents['net_kg'] > capacity_kg + 1e-9:
        reasons.append(
            f"净重 {contents['net_kg']}kg 超过容量 {capacity_kg}kg")
    ok = not reasons
    return {'container': container_id, 'ok': ok,
            'status': DELIVERABLE if ok else BLOCKED,
            'reasons': reasons, 'contents': contents}


def ready(entries, container, capacity, ledger=None):
    """旧接口兼容：仅按容量判断的布尔结果保留给纯聚合场景。

    修复后默认要求传入由来源/分类/封签/称重事件重算出的 WasteLedger（或
    PickupLedger）；此时 ready 同时执行分类核证与兼容表判断，容量合格不再
    单独放行。
    """
    if ledger is None:
        # 无事件账本时维持原容量语义，但调用方应知悉这不构成交付核证。
        return weight_for(entries, container) <= capacity
    physical = ledger.waste if isinstance(ledger, PickupLedger) else ledger
    check = evaluate_container(physical, container, capacity)
    return check['ok']
