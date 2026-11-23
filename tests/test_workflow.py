import json,tempfile,unittest
from pathlib import Path
from bookconserve.core import connect,import_file,report,audit
from bookconserve.policy import calculate

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def result(db,subject,name):
    return db.execute('SELECT result FROM current_decisions WHERE subject=? AND decision=?',(subject,name)).fetchone()[0]
class ConservationWorkflow(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def load(self,sub,phase='ok'):return import_file(self.db,sub,ROOT/f'{sub}-{phase}.json')
    def baseline(self):
        for sub in ('mandate','treatment','inspection'):self.load(sub)
    def test_ten_decisions_and_replay(self):
        self.baseline();self.assertGreaterEqual(len(report(self.db)['decisions']),10)
        self.assertEqual('验收',result(self.db,'V1','迟到复检是否撤销阶段验收'))
        self.assertEqual(0,self.load('inspection')['inserted']);self.assertTrue(audit(self.db)['ok'])
    def test_withdrawal_blocks_treatment_and_acceptance(self):
        self.baseline();self.load('mandate','bad')
        self.assertEqual('冻结',result(self.db,'V1','委托撤回是否冻结后续工序'))
        self.assertEqual('返工',result(self.db,'V1','迟到复检是否撤销阶段验收'))
    def test_contaminated_lot_reopens_acceptance(self):
        self.baseline();self.load('treatment','bad')
        self.assertEqual('隔离',result(self.db,'V1','材料污染是否隔离关联卷册'))
        self.assertEqual('返工',result(self.db,'V1','迟到复检是否撤销阶段验收'))
    def test_late_damage_reopens_liability(self):
        self.baseline();self.load('inspection','bad')
        self.assertEqual('赔偿复核',result(self.db,'V1','新增损伤是否形成返工责任'))
        self.assertEqual('重开',result(self.db,'V1','委托损伤赔偿责任是否重开'))
        self.assertEqual('返工',result(self.db,'V1','迟到复检是否撤销阶段验收'))
    def test_collision_rolls_back(self):
        self.baseline();before=report(self.db)
        event=json.loads((ROOT/'mandate-ok.json').read_text(encoding='utf-8'))[0];event['data']['holder']='Other'
        p=Path(self.tmp.name)/'collision.json';p.write_text(json.dumps([event]),encoding='utf-8')
        with self.assertRaises(ValueError):import_file(self.db,'mandate',p)
        self.assertEqual(before,report(self.db))
    def test_independent_rule_boundaries(self):
        import copy
        base=sum((json.loads((ROOT/f'{s}-ok.json').read_text(encoding='utf-8')) for s in ('mandate','treatment','inspection')),[])
        def decision(rows,name):return next(d[2] for d in calculate(rows)[1] if d[1]==name)
        cases=[(1,'edition','Other','卷册版本与授权范围是否一致','不一致'),
               (3,'ph_min',7,'材料批次是否适配纸张酸度','不适配'),
               (4,'method','bleach','禁用工艺是否进入施工记录','违禁'),
               (6,'at','2026-12-03','工序次序是否满足干燥与加固','未达标'),
               (8,'stratum','tear','复检样区是否覆盖残损分层','偏置')]
        for index,key,value,name,want in cases:
            rows=copy.deepcopy(base)
            if key=='at':rows[index]['at']=value
            else:rows[index]['data'][key]=value
            with self.subTest(name=name):self.assertEqual(want,decision(rows,name))
if __name__=='__main__':unittest.main()
