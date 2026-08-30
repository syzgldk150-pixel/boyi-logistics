---
module: 车辆调度
type: 模块文档
tags: [车辆调度, 地图, 路线规划, 高德地图, 本地估算]
related: [../project_overview.md, ../code_navigation_index.md]
status: active
implementation_status: map_only
updated: 2026-08-30
---

# 车辆调度模块概述

## 当前结论

当前 `/dispatch` 只是 Console 中的地图路线规划与本地试算页，不是车辆调度执行系统。页面没有接入真实车辆、司机、排班、派单、轨迹、货拉拉或运满满订单/报价接口，也不会向 Agent 提交调度写操作。

## 已实现范围

- 页面入口：`/dispatch`
- 使用高德地图 JS API 2.0 的 `AutoComplete`、`PlaceSearch`、`Geocoder` 与 `Driving`
- 支持发货地/目的地候选、地址消歧、驾车路线、真实路线距离与预计时长
- 浏览器定位成功时只用于地图中心和地址搜索上下文，不自动写入发货地
- 基于路线距离、用户输入的重量和体积做前端本地试算

页面中的“货拉拉”“运满满”和内部估价均来自 `console/templates/dispatch.html` 的本地公式，只用于界面演示，不能作为平台实时价格、结算依据或真实调度结果。

## 未实现范围

- 车辆、司机、排班和运力资源数据源
- 调度任务创建、分配、状态流转与持久化
- 车辆实时位置、轨迹和异常预警
- 货拉拉、运满满或其他平台的真实询价、下单与回执
- 调度记录向共享财务账本或 AI 客服输出

## 代码位置

- 页面路由与配置：`console/app.py` 的 `_render_dispatch()`
- 页面与路线逻辑：`console/templates/dispatch.html`
- 路线工具：`console/static/js/amap_route_utils.js`
- 样式：`console/static/style.css`
- 菜单目录：`console/navigation.py`

高德 Key 只由服务端配置注入页面；前端不得内置 Key 或默认值。缺少 Key、SDK 加载失败、地址多候选或路线规划失败时必须明确提示，不得用固定距离或估算路线兜底。

## 后续启用条件

只有接入并验证真实车辆/司机/派单数据源、定义调度状态机与权限、通过受管 Command 执行写操作，并对第三方平台结果建立可核验回执后，才能把本模块状态从 `map_only` 改为调度系统。
