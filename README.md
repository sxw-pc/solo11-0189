# 古籍修复委托验收采证台

完全离线的修复委托、材料工序和独立复检三账采证与重裁演示。仅使用合成 JSON 和本地 SQLite。原始文件 SHA-256 留存，导入及裁定事务化，异内容事件 ID 冲突回滚，历史及转移日志保留。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m bookconserve demo --db demo.sqlite
python -m bookconserve audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：`mandate-import`、`treatment-import`、`inspection-import`，均使用 `--db` 和 `--file`。每项正常及异常夹具在 `fixtures/`，演示逐个导入并输出原证摘要、当前与历史裁定及旧新值转移。三条跨账路径：委托撤回冻结后续工序及验收；污染材料追溯卷册并撤销验收；独立复检新增损伤重开委托赔偿责任。`audit` 从原证重新计算并核对数据库投影。
