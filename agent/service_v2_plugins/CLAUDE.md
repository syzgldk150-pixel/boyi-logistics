# 当前 V2 插件源码规则

当前业务动作只维护在本目录对应插件的 `payload/`；每日应签规则、采集和渲染在 `sync_daily_should_sign_v2/payload/business/`。Host 不维护应签类型清单或覆盖插件提交的判断。公共字段协议在根目录 `shared/ronghui_*_fields.py`，修改公共协议需要核心更新。

打包不得读取 `agent/legacy/` 或已退役的 `agent/first_party_automation_plugins/`。迁移历史、旧摘要和 SQL 保留用于离线回归，不能作为线上回退入口。维护与发布顺序见本目录 `README.md`。

## 阶段一收尾包

- 分批 `2.0.3` 显式提交问题件登记的 `before_cutoff/postpones_sign`，复用每日应签包内 `problem_event_flags`；共享打包器将同一规则及值解析模块放入独立 ZIP，不复制业务公式。每日应签 `2.0.9` 同样引用这个单一规则。
- 分批登记 Connector 严格要求显式分类字段。宿主与分批包需按收尾发布说明配套更新；旧包缺字段会明确失败，不能用默认布尔值补齐。旧 Action V1 打包仅保持离线回归闭合，不恢复线上入口。
- 当前 M02/M03 分别使用财务、自提 V2 基线/候选/回退 ZIP，在冻结宿主上验证实际字段/判断变化；准备结果不作为正式冻结证据。
