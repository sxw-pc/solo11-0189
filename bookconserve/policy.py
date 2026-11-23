"""Independent mandate, treatment and inspection ledgers."""
from collections import defaultdict
SUBSYSTEMS=('mandate','treatment','inspection')

def _groups(events,sub):
    result=defaultdict(list)
    for e in events:
        if e['subsystem']==sub:result[e['subject']].append(e)
    return result

def _decision(subject,name,result,evidence):
    return (subject,name,result,'按不可变证据及当前关联账本重算',[e['id'] for e in evidence])

def mandates(events):
    states={}; entities=[]; decisions=[]
    for volume,rows in _groups(events,'mandate').items():
        owners=[e for e in rows if e['action']=='ownership']; grants=[e for e in rows if e['action']=='grant']; pulls=[e for e in rows if e['action']=='withdraw']
        owner=owners[-1] if owners else None; grant=grants[-1] if grants else None
        valid=bool(owner and grant and owner['data']['holder']==grant['data']['holder'] and owner['data']['from']<=grant['at']<=owner['data']['to'] and len({g['data']['mandate_id'] for g in grants})==1)
        scope=bool(owner and grant and owner['data']['edition']==grant['data']['edition'] and grant['data']['page_from']<=grant['data']['page_to'])
        revoked=bool(grant and any(p['data']['mandate_id']==grant['data']['mandate_id'] for p in pulls))
        baseline=next((e['data'] for e in events if e['subsystem']=='treatment' and e['subject']==volume and e['action']=='baseline'),{})
        damage=any(e['subsystem']=='inspection' and e['subject']==volume and e['action']=='damage' and e['data'].get('independent') and e['data']['severity']>baseline.get('severity',0) for e in events)
        states[volume]={'allowed':valid and scope and not revoked,'grant':grant['data'] if grant else {},'holder':owner['data']['holder'] if owner else None}
        entities.extend(('修复委托确权','馆藏权属' if e['action']=='ownership' else '修复委托',e['id'],e['action'],e['id']) for e in rows)
        for name,result in [('权属链是否授予修复委托权','有效' if valid else '无效'),('委托撤回是否冻结后续工序','冻结' if revoked else '开放'),('卷册版本与授权范围是否一致','一致' if scope else '不一致')]:decisions.append(_decision(volume,name,result,rows))
        decisions.append(_decision(volume,'委托损伤赔偿责任是否重开','重开' if damage else '未重开',rows+[e for e in events if e['subsystem']=='inspection' and e['subject']==volume and e['action']=='damage']))
    return states,entities,decisions

def treatments(events,mandate):
    states={}; entities=[]; decisions=[]
    contaminated={e['data']['lot'] for e in events if e['subsystem']=='treatment' and e['action']=='contaminate'}
    for volume,rows in _groups(events,'treatment').items():
        baseline=next((e for e in reversed(rows) if e['action']=='baseline'),None)
        material=next((e for e in reversed(rows) if e['action']=='material'),None)
        steps=[e for e in rows if e['action']=='procedure']; grant=mandate.get(volume,{}).get('grant',{})
        ph=baseline['data']['ph'] if baseline else None
        compatible=bool(material and ph is not None and material['data']['ph_min']<=ph<=material['data']['ph_max'])
        forbidden=any(e['data']['method'] not in grant.get('methods',[]) for e in steps)
        ordered=bool(len(steps)==3 and [e['data']['method'] for e in steps]==['clean','dry','consolidate'] and steps[2]['at']>=steps[1]['data']['ready_at'])
        polluted=bool(material and material['data']['lot'] in contaminated)
        states[volume]={'complete':bool(mandate.get(volume,{}).get('allowed') and compatible and not forbidden and ordered and not polluted),'baseline':baseline['data'] if baseline else {},'polluted':polluted,'lot':material['data']['lot'] if material else None}
        entities.extend(('修复处理履约','残损图谱' if e['action'] in ('baseline','procedure') else '材料批次',e['id'],e['action'],e['id']) for e in rows)
        linked=rows+[e for e in events if e['subsystem']=='mandate' and e['subject']==volume]
        for name,result in [('材料批次是否适配纸张酸度','适配' if compatible else '不适配'),('禁用工艺是否进入施工记录','违禁' if forbidden else '合规'),('工序次序是否满足干燥与加固','达标' if ordered else '未达标'),('材料污染是否隔离关联卷册','隔离' if polluted else '正常')]:decisions.append(_decision(volume,name,result,linked))
    return states,entities,decisions

def inspections(events,mandate,treatment):
    entities=[]; decisions=[]
    for volume,rows in _groups(events,'inspection').items():
        samples=[e for e in rows if e['action']=='sample']; signs=[e for e in rows if e['action']=='sign']; damages=[e for e in rows if e['action']=='damage']
        baseline=treatment.get(volume,{}).get('baseline',{})
        covered=bool(baseline.get('strata') and set(baseline['strata']).issubset({e['data']['stratum'] for e in samples}))
        independent=bool(signs and all(e['data']['inspector']!=e['data']['restorer'] for e in signs))
        damaged=any(e['data']['severity']>baseline.get('severity',0) and e['data']['independent'] for e in damages)
        accepted=bool(covered and independent and treatment.get(volume,{}).get('complete') and not damaged)
        entities.extend(('独立复检验收','复检样区' if e['action'] in ('sample','damage') else '验收裁定',e['id'],e['action'],e['id']) for e in rows)
        linked=rows+[e for e in events if e['subject']==volume and e['subsystem'] in ('mandate','treatment')]
        for name,result in [('复检样区是否覆盖残损分层','覆盖' if covered else '偏置'),('新增损伤是否形成返工责任','赔偿复核' if damaged and independent else '无新增责任'),('迟到复检是否撤销阶段验收','验收' if accepted else '返工')]:decisions.append(_decision(volume,name,result,linked))
    return entities,decisions

def calculate(events):
    m,me,md=mandates(events);t,te,td=treatments(events,m);ie,ide=inspections(events,m,t)
    return me+te+ie,md+td+ide
