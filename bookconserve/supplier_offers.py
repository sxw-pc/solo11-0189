"""供应商可用批次与真实交付能力账本。

报价单不再只是 ``material`` 代码加单价。每张报价单是一个供应商批次，
只有同时通过以下核验才可能进入采购账：

1. 规格证书与修复要求（材料代码 + 克重）相符且已核验；
2. 报价单在计划日仍在有效期内；
3. 所选运力（常规/加急）的实际到货不晚于工单工期；
4. 该运力已确认的可用运力能覆盖实际起订量（局部确认不可当全单交付）；
5. 采购数量满足供应商起订量 MOQ；
6. 批次在到货日（及后续领用日）仍未到期。
"""
from datetime import date


def _as_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def certificate_matches(offer, material, gsm):
    """批次/克重证明是否与修复要求相符。未核验或克重不符一律不得订作等效材料。"""
    cert = offer.get('certificate') or {}
    return bool(
        cert.get('verified')
        and offer.get('material') == material
        and cert.get('material') == material
        and gsm is not None
        and offer.get('gsm') == gsm
        and cert.get('gsm') == gsm
    )


def quote_is_valid(offer, as_of):
    """报价单在计划日是否仍有效（过期报价不得下单）。"""
    until = offer.get('quote_valid_until')
    return until is None or _as_date(as_of) <= _as_date(until)


def lot_is_usable(offer, usable_on):
    """批次在指定日期是否仍可用。到期批次不得供任何在到期日之后的领用。"""
    expiry = offer.get('lot_expiry')
    return expiry is None or _as_date(usable_on) <= _as_date(expiry)


def offers_for(offers, material, gsm=None, as_of=None):
    """返回通过规格证书核验且报价仍有效的供应商批次。

    仅按 material 代码过滤会把 9gsm 的 V3 当成 12gsm 等效材料；这里以已核验
    证书上的材料代码与克重为准。``gsm`` 省略时只按材料代码做粗筛（不含证书
    核验），供展示用途，任何下单路径都必须显式传入克重。
    """
    result = [offer for offer in offers if offer.get('material') == material]
    if gsm is not None:
        result = [
            offer for offer in result
            if certificate_matches(offer, material, gsm)
        ]
    if as_of is not None:
        result = [offer for offer in result if quote_is_valid(offer, as_of)]
    return result


def delivery_modes(offer):
    """呈现供应商批次真实可订的运力：常规与加急各自的到货日、费用和已确认运力。

    加急到货不早于常规时没有额外运力价值；加急确认运力为零时该运力不存在。
    """
    modes = [{
        'mode': 'regular',
        'arrival': offer['delivery'],
        'fee': 0,
        'confirmed_capacity': offer.get('regular_confirmed', 0),
    }]
    if (
        offer.get('expedite_delivery')
        and _as_date(offer['expedite_delivery']) < _as_date(offer['delivery'])
        and offer.get('expedite_confirmed', 0) > 0
    ):
        modes.append({
            'mode': 'expedite',
            'arrival': offer['expedite_delivery'],
            'fee': offer.get('expedite_fee', 0),
            'confirmed_capacity': offer['expedite_confirmed'],
        })
    return modes


def feasible_modes(offer, needed_by):
    """真实到货不晚于工期的运力。只过滤 delivery<=工期 的做法忽略了确认运力，
    因此这里只按到货日期初筛，起订量与确认运力在 :func:`can_lock` 联合锁定。"""
    return [
        mode for mode in delivery_modes(offer)
        if _as_date(mode['arrival']) <= _as_date(needed_by)
    ]


def can_lock(offer, mode_name, quantity, needed_by, use_by=None):
    """联合核验：该批次能否以指定运力锁定 ``quantity`` 张并满足工期。

    硬约束联合判定，任一不过即不可下单：
      * 运力真实到货不晚于工期；
      * 实际采购量满足起订量 MOQ；
      * 供应商在该运力上的已确认运力覆盖全量（局部确认不得当全单交付）；
      * 批次在到货日及最晚领用日未到期。
    """
    mode = next((m for m in feasible_modes(offer, needed_by)
                 if m['mode'] == mode_name), None)
    if mode is None:
        return False
    if quantity < offer.get('minimum', 1):
        return False
    if quantity > mode['confirmed_capacity']:
        return False
    if not lot_is_usable(offer, mode['arrival']):
        return False
    if use_by is not None and not lot_is_usable(offer, use_by):
        return False
    return True
