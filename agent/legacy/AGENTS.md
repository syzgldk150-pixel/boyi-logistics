# 离线历史材料

仅用于显式历史迁移回归，不进入 ECS 发布清单，不允许添加到线上 sys.path 或作为执行失败后的回退。业务算法只在当前 V2 插件维护。已执行 SQL 和旧包摘要不得修改。

`frozen/` 源码（分批1.0.26的规则文件、扫描 V1 的 `action.py`）只保存重建原摘要所需的历史字节，不在此修复或演进业务规则；来源及用途见`first_party_automation_plugins/README.md`。
