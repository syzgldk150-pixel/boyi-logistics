# 当前 V2 插件源码规则

当前业务动作只维护在本目录对应插件的 `payload/`；每日应签规则、采集和渲染在 `sync_daily_should_sign_v2/payload/business/`。Host 不维护应签类型清单或覆盖插件提交的判断。公共字段协议在根目录 `shared/ronghui_*_fields.py`，修改公共协议需要核心更新。

打包不得读取 `agent/legacy/` 或已退役的 `agent/first_party_automation_plugins/`。迁移历史、旧摘要和 SQL 保留用于离线回归，不能作为线上回退入口。维护与发布顺序见本目录 `README.md`。
