"""实物废弃物采证账本：来源工单、分类检测、皮重/称重、封签与拆封分装守恒。

所有状态均由不可变事件按 (at, version, id) 顺序重算得出，不允许直接在数据库
里挪动重量：批次从一个容器转入另一个容器，必须经由 unseal（拆封）、split_place
（现场重称分装）与 sign（双方签收）三类物理事件，且分装前后重量守恒。
"""
from collections import OrderedDict

SUBSYSTEM = 'workshop-waste'

# 事件动作枚举，顺序即实物处置链的允许次序。
SOURCE = 'source'            # 来源工单（MS 工单登记批次）
TARE = 'tare'                # 容器皮重
CLASSIFY = 'classify'        # 分类检测（供应商成分证 / 馆内复核证）
PLACE = 'place'              # 实物扫入容器并称重（毛重/净重）
SEAL = 'seal'                # 容器封签
UNSEAL = 'unseal'            # 拆封（分装前必须先拆封）
SPLIT_PLACE = 'split_place'  # 现场拆分：把批次实物重称放入新容器
SIGN = 'sign'                # 馆方/清运方双方签收


class LedgerError(ValueError):
    """事件违反实物处置链或守恒约束。"""


def _require(data, keys, event_id):
    missing = [k for k in keys if k not in data]
    if missing:
        raise LedgerError(f"事件 {event_id} 缺少字段 {missing}")
    return True


class WasteLedger:
    """对 workshop-waste 事件流做确定性重算的内存投影。"""

    def __init__(self, events=()):
        self.batches = OrderedDict()       # 批次号 -> 批次状态
        self.containers = OrderedDict()    # 容器号 -> 容器状态
        self.events = []
        for event in events:
            self.apply(event)

    # ------------------------------------------------------------------ #
    # 事件重放
    # ------------------------------------------------------------------ #
    def apply(self, event):
        for key in ('id', 'at', 'version', 'subject', 'action', 'data'):
            if key not in event:
                raise LedgerError(f"事件信封缺少字段 {key}")
        if event.get('subsystem') not in (None, SUBSYSTEM):
            raise LedgerError(f"事件 {event['id']} 子系统不属于 {SUBSYSTEM}")
        if event['id'] in {e['id'] for e in self.events}:
            raise LedgerError(f"事件 id 重复：{event['id']}")
        action = event['action']
        handler = getattr(self, f'_on_{action}', None)
        if handler is None:
            raise LedgerError(f"未知事件动作 {action}（事件 {event['id']}）")
        handler(event)
        self.events.append(event)
        return self

    def _batch(self, batch_id, event_id):
        batch = self.batches.get(batch_id)
        if batch is None:
            raise LedgerError(f"事件 {event_id} 引用了未登记来源工单的批次 {batch_id}")
        return batch

    def _container(self, container_id, event_id):
        container = self.containers.get(container_id)
        if container is None:
            raise LedgerError(f"事件 {event_id} 引用了未登记皮重的容器 {container_id}")
        return container

    def _on_source(self, event):
        d = event['data']
        _require(d, ('batch', 'work_order', 'kg', 'declared_category'), event['id'])
        if d['kg'] <= 0:
            raise LedgerError(f"事件 {event['id']} 来源工单重量必须为正")
        if d['batch'] in self.batches:
            raise LedgerError(f"批次 {d['batch']} 重复建立来源工单")
        self.batches[d['batch']] = {
            'batch': d['batch'],
            'work_order': d['work_order'],
            'source_kg': d['kg'],
            'declared_category': d['declared_category'],
            'category': d['declared_category'],
            # 清洁垫类别必须经供应商成分证与馆内复核双证核证；
            # 其余类别（如湿纸浆）的分类由来源工单自身确立。
            'classification': (None if d['declared_category'].endswith('pads')
                               else d['declared_category']),
            'evidence': [],                  # 分类检测证据链（保留争议）
            'location': None,                # 当前唯一所在容器
            'chain': [],                     # 当前交接链上的物理事件
            'kg_in_place': 0.0,
            'active_kg': 0.0,                # 仍在实物处置链内的重量
            'sealed': False,
        }

    def _on_tare(self, event):
        d = event['data']
        _require(d, ('container', 'tare_kg'), event['id'])
        if d['container'] in self.containers:
            raise LedgerError(f"容器 {d['container']} 皮重重复登记")
        if d['tare_kg'] < 0:
            raise LedgerError(f"事件 {event['id']} 皮重不得为负")
        self.containers[d['container']] = {
            'container': d['container'],
            'tare_kg': d['tare_kg'],
            'capacity_kg': d.get('capacity_kg'),
            'batches': [],                   # [(batch, kg)]
            'sealed': False,
            'seal_event': None,
            'gross_kg': 0.0,
            'net_kg': 0.0,
            'history': [],                   # 封签/拆封/分装链
        }

    def _on_classify(self, event):
        d = event['data']
        _require(d, ('batch', 'result', 'basis'), event['id'])
        if d['result'] not in ('solvent', 'water-based'):
            raise LedgerError(f"事件 {event['id']} 检测结论必须为 solvent 或 water-based")
        if d['basis'] not in ('supplier-cert', 'inhouse-review'):
            raise LedgerError(f"事件 {event['id']} 检测依据必须为供应商成分证或馆内复核证")
        batch = self._batch(d['batch'], event['id'])
        # 检测原件只能追加，证据链保留原始分类争议，不覆盖历史结论。
        batch['evidence'].append({
            'event': event['id'], 'at': event['at'], 'result': d['result'],
            'basis': d['basis'], 'doc': d.get('doc'),
        })
        supplier = {e['result'] for e in batch['evidence'] if e['basis'] == 'supplier-cert'}
        inhouse = {e['result'] for e in batch['evidence'] if e['basis'] == 'inhouse-review'}
        if supplier and inhouse:
            if supplier == inhouse and len(supplier) == 1:
                batch['classification'] = next(iter(supplier))
                batch['category'] = ('solvent-pads' if batch['classification'] == 'solvent'
                                     else 'water-based-pads')
            else:
                # 供应商原件与馆内复核冲突：分类争议未决，批次隔离待核。
                batch['classification'] = 'disputed'
                batch['category'] = 'disputed-pads'
        else:
            batch['classification'] = 'pending'   # 证据不完整，不能放行

    def _on_place(self, event):
        d = event['data']
        _require(d, ('batch', 'container', 'kg', 'gross_kg'), event['id'])
        batch = self._batch(d['batch'], event['id'])
        container = self._container(d['container'], event['id'])
        if batch['location'] is not None:
            raise LedgerError(
                f"批次 {batch['batch']} 已在容器 {batch['location']} 中，"
                "一个实物批次只能存在于一个容器；分装须先拆封再重称")
        if container['sealed']:
            raise LedgerError(f"容器 {container['container']} 已封签，禁止扫入新实物")
        if d['kg'] <= 0:
            raise LedgerError(f"事件 {event['id']} 称重重量必须为正")
        self._check_capacity(container, d['kg'], event['id'])
        # 毛重是累计秤读数：本次毛重 - 皮重 - 桶内已有净重 = 本次扫入净重。
        expected_net = round(d['gross_kg'] - container['tare_kg'] - container['net_kg'], 9)
        if abs(expected_net - d['kg']) > 1e-9:
            raise LedgerError(
                f"事件 {event['id']} 净重 {d['kg']} 与毛重 {d['gross_kg']}、"
                f"皮重 {container['tare_kg']} 及桶内已有净重不一致")
        container['batches'].append((batch['batch'], d['kg']))
        container['gross_kg'] = d['gross_kg']
        container['net_kg'] = round(container['net_kg'] + d['kg'], 9)
        container['history'].append({'event': event['id'], 'at': event['at'],
                                     'action': PLACE, 'batch': batch['batch'], 'kg': d['kg']})
        batch['location'] = container['container']
        batch['kg_in_place'] = d['kg']
        batch['active_kg'] = d['kg']
        batch['chain'].append(event['id'])

    def _on_seal(self, event):
        d = event['data']
        _require(d, ('container', 'seal_id'), event['id'])
        container = self._container(d['container'], event['id'])
        if container['sealed']:
            raise LedgerError(f"容器 {container['container']} 封签重复")
        if not container['batches']:
            raise LedgerError(f"容器 {container['container']} 为空，不得封签")
        existing_seals = {c['seal_event'] for c in self.containers.values() if c['sealed']}
        if d['seal_id'] in existing_seals or any(
                h.get('seal_id') == d['seal_id']
                for c in self.containers.values() for h in c['history']):
            raise LedgerError(f"封签号 {d['seal_id']} 重复使用")
        container['sealed'] = True
        container['seal_event'] = event['id']
        container['history'].append({'event': event['id'], 'at': event['at'],
                                     'action': SEAL, 'seal_id': d['seal_id']})
        for batch_id, _kg in container['batches']:
            batch = self.batches[batch_id]
            batch['sealed'] = True
            batch['chain'].append(event['id'])

    def _on_unseal(self, event):
        d = event['data']
        _require(d, ('container', 'seal_id'), event['id'])
        container = self._container(d['container'], event['id'])
        if not container['sealed']:
            raise LedgerError(f"容器 {container['container']} 未封签，不能拆封")
        if d['seal_id'] not in [h.get('seal_id') for h in container['history']]:
            raise LedgerError(
                f"拆封事件 {event['id']} 的封签号 {d['seal_id']} "
                f"与容器 {container['container']} 实际封签不符")
        container['sealed'] = False
        container['seal_event'] = None
        container['history'].append({'event': event['id'], 'at': event['at'],
                                     'action': UNSEAL, 'seal_id': d['seal_id']})
        for batch_id, _kg in container['batches']:
            batch = self.batches[batch_id]
            batch['sealed'] = False
            batch['chain'].append(event['id'])

    def _on_split_place(self, event):
        d = event['data']
        _require(d, ('batch', 'from_container', 'to_container', 'kg', 'gross_kg'), event['id'])
        batch = self._batch(d['batch'], event['id'])
        source = self._container(d['from_container'], event['id'])
        target = self._container(d['to_container'], event['id'])
        if batch['location'] != source['container']:
            raise LedgerError(
                f"批次 {batch['batch']} 当前不在 {source['container']}，"
                "不能凭补录制造实物移动")
        if source['sealed']:
            raise LedgerError(
                f"容器 {source['container']} 尚未拆封，分装必须先拆封再重称")
        if target['sealed']:
            raise LedgerError(f"目标容器 {target['container']} 已封签")
        if target['container'] == source['container']:
            raise LedgerError("分装源容器与目标容器必须不同")
        if any(b == batch['batch'] for b, _ in target['batches']):
            raise LedgerError(f"批次 {batch['batch']} 已在目标容器中")
        if d['kg'] <= 0:
            raise LedgerError(f"事件 {event['id']} 分装重量必须为正")
        # 守恒：拆出重量不得超过该批次在源桶内的现存实物重量。
        current = dict(source['batches']).get(batch['batch'], 0.0)
        if d['kg'] > current + 1e-9:
            raise LedgerError(
                f"分装 {d['kg']}kg 超过批次 {batch['batch']} 在 {source['container']} "
                f"的现存 {current}kg，重量不守恒")
        expected_net = round(d['gross_kg'] - target['tare_kg'] - target['net_kg'], 9)
        if abs(expected_net - d['kg']) > 1e-9:
            raise LedgerError(
                f"事件 {event['id']} 分装净重与目标桶毛重/皮重不一致")
        self._check_capacity(target, d['kg'], event['id'])
        # 从源桶扣减实物；若批次全部转出则源桶不再持有该批次。
        remaining = round(current - d['kg'], 9)
        source['batches'] = [(b, k if b != batch['batch'] else remaining)
                             for b, k in source['batches'] if not (b == batch['batch'] and remaining <= 1e-9)]
        source['net_kg'] = round(max(source['net_kg'] - d['kg'], 0.0), 9)
        # 转出后源桶按新净重与皮重复称，毛重不得停留在拆封前的读数。
        source['gross_kg'] = round(source['tare_kg'] + source['net_kg'], 9)
        source['history'].append({'event': event['id'], 'at': event['at'], 'action': SPLIT_PLACE,
                                  'batch': batch['batch'], 'kg': d['kg'],
                                  'to_container': target['container']})
        target['batches'].append((batch['batch'], d['kg']))
        target['gross_kg'] = d['gross_kg']
        target['net_kg'] = round(target['net_kg'] + d['kg'], 9)
        target['history'].append({'event': event['id'], 'at': event['at'], 'action': SPLIT_PLACE,
                                  'batch': batch['batch'], 'kg': d['kg'],
                                  'from_container': source['container']})
        batch['location'] = target['container']
        batch['kg_in_place'] = d['kg']
        batch['active_kg'] = d['kg']
        batch['chain'].append(event['id'])

    def _on_sign(self, event):
        d = event['data']
        _require(d, ('container', 'museum_party', 'carrier_party'), event['id'])
        container = self._container(d['container'], event['id'])
        if not container['sealed']:
            raise LedgerError(f"容器 {container['container']} 未封签，不能签收交接")
        if not d['museum_party'] or not d['carrier_party']:
            raise LedgerError(f"事件 {event['id']} 交接须馆方与清运方双签")
        container['history'].append({'event': event['id'], 'at': event['at'], 'action': SIGN,
                                     'museum_party': d['museum_party'],
                                     'carrier_party': d['carrier_party']})
        for batch_id, _kg in container['batches']:
            self.batches[batch_id]['chain'].append(event['id'])

    def _check_capacity(self, container, added_kg, event_id):
        capacity = container['capacity_kg']
        if capacity is not None and container['net_kg'] + added_kg > capacity + 1e-9:
            raise LedgerError(
                f"事件 {event_id} 使容器 {container['container']} 净重 "
                f"{container['net_kg'] + added_kg}kg 超过容量 {capacity}kg")

    # ------------------------------------------------------------------ #
    # 投影查询
    # ------------------------------------------------------------------ #
    def container_contents(self, container_id):
        """返回容器当前实际内容（净重与批次分类），空桶或不存在返回 None。"""
        container = self.containers.get(container_id)
        if container is None or not container['batches']:
            return None
        return {
            'container': container_id,
            'tare_kg': container['tare_kg'],
            'gross_kg': container['gross_kg'],
            'net_kg': container['net_kg'],
            'sealed': container['sealed'],
            'seal_event': container['seal_event'],
            'items': [
                {'batch': b, 'kg': k,
                 'category': self.batches[b]['category'],
                 'classification': self.batches[b]['classification'],
                 'work_order': self.batches[b]['work_order']}
                for b, k in container['batches']
            ],
        }

    def quarantine_batches(self):
        """分类未核证或争议未决、必须隔离不得交付的批次。"""
        return [b for b in self.batches.values()
                if b['classification'] in (None, 'pending', 'disputed')]

    def snapshot(self):
        return {
            'batches': [dict(b, evidence=list(b['evidence']), chain=list(b['chain']))
                        for b in self.batches.values()],
            'containers': [self.container_contents(c) for c in self.containers
                           if self.container_contents(c) is not None],
            'quarantine': [b['batch'] for b in self.quarantine_batches()],
        }


def replay(events):
    """从事件流重算实物账本。"""
    return WasteLedger(events)


def weight_for(entries, container):
    """兼容旧夹具：按容器对净重求和。

    仅做重量聚合，不代表可交付；交付资格必须经 pickup_manifest 的分类核证
    与兼容表判断。
    """
    return sum(item['kg'] for item in entries if item['container'] == container)
