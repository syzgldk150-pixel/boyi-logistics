---
name: 博益物流控制台
description: 面向物流运营的高密度调度与业务处理后台
colors:
  canvas: "#F9F9FB"
  surface: "#FFFFFF"
  ink: "#111827"
  text: "#4B5563"
  muted: "#9CA3AF"
  line: "#E5E7EB"
  accent: "#EF4444"
  accent-soft: "#FEF2F2"
  success: "#10B981"
  warning: "#F59E0B"
  info: "#3B82F6"
typography:
  display:
    fontFamily: "Noto Sans SC, Source Han Sans SC, system-ui, -apple-system, sans-serif"
    fontSize: "1.65rem"
    fontWeight: 900
    lineHeight: 1.18
  body:
    fontFamily: "Noto Sans SC, Source Han Sans SC, system-ui, -apple-system, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.6
  mono:
    fontFamily: "JetBrains Mono, Consolas, monospace"
rounded:
  sm: "6px"
  md: "8px"
  lg: "12px"
  xl: "16px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "20px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    rounded: "{rounded.md}"
    padding: "0 16px"
    height: "42px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "0 16px"
    height: "42px"
  navigation-active:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent}"
    rounded: "{rounded.md}"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.xl}"
    padding: "20px"
---

# Design System: 博益物流控制台

## Overview

**Creative North Star: "物流调度台"**

这是面向真实业务操作的任务型后台。冷白画布、清晰分隔线和紧凑的信息层级让用户能在查单、录单、审核与调度之间快速切换。红色仅用于当前选择、关键动作和明确状态，不承担装饰任务。

移动端沿用同一套信息架构，但用底栏和更多面板替代固定侧栏。密集内容按工作性质重排，普通页面采用单列，宽表在自身容器中滚动，地图与第三方原页在动态视口内进入专注工作区。

- 关键特征：高密度、可扫读、真实状态、克制动效、桌面与移动一致的导航语义。
- 明确拒绝：营销落地页、装饰性卡片堆叠、低密度仪表盘、隐藏移动端核心功能和不透明的失败兜底。

## Colors

冷白与石墨灰建立可靠的工作底色，单一红色强调让危险、当前选择和需注意的动作不被稀释。

### Primary

- **操作红** (`#EF4444`)：当前导航、重点状态、焦点关联和需要立即识别的操作。
- **石墨主操作** (`#111827`)：主要提交按钮，保持严谨、稳定的操作语义。

### Neutral

- **调度画布** (`#F9F9FB`)：页面背景与大面积工作区。
- **工作表面** (`#FFFFFF`)：侧栏、面板、输入与弹层。
- **主文本** (`#111827`)：标题、重要数据与主要可交互文案。
- **正文灰** (`#4B5563`)：常规说明，必须保持与表面的 AA 对比度。
- **分隔线** (`#E5E7EB`)：边界和表格分隔，不作为状态唯一载体。

**单一强调规则。** 红色用于状态与行动，不用于大面积背景、渐变文字或无意义装饰。

## Typography

**Display Font:** Noto Sans SC, Source Han Sans SC, system-ui, -apple-system, sans-serif
**Body Font:** Noto Sans SC, Source Han Sans SC, system-ui, -apple-system, sans-serif
**Label/Mono Font:** JetBrains Mono, Consolas, monospace

**Character:** 使用一套可读性优先的无衬线字族，靠字重与间距区分层级，适合中英文混排、表格数据和高频操作。

### Hierarchy

- **Display**（900，1.65rem，1.18）：页面标题与主要工作区标题。
- **Title**（800，1.2–1.5rem，1.25）：面板与功能分区标题。
- **Body**（400，14px，1.6）：说明、表单和常规内容。
- **Label**（600–800，0.75–0.95rem）：导航、控件标签和状态标签。
- **Mono**（400，继承字号）：单号、代码和需要对齐扫描的数据。

**任务密度规则。** 产品 UI 不使用营销式超大标题，移动端保持可读的固定层级，而非依靠流动字号缩放。

## Elevation

界面默认依靠白色表面和细分隔线分层，仅以低对比度、短模糊半径阴影表达可悬浮或可操作的表面。焦点和状态通过颜色与描边表达，阴影不与装饰性粗边框叠加。

### Shadow Vocabulary

- **基础表面**（`0 4px 20px rgba(0, 0, 0, 0.05)`）：普通面板和卡片。
- **轻触表面**（`0 2px 10px rgba(0, 0, 0, 0.02)`）：搜索与次级容器。
- **悬停反馈**（`0 10px 26px rgba(15, 23, 42, 0.08)`）：仅用于可操作卡片的状态反馈。

**平面优先规则。** 不用玻璃拟态或厚重阴影制造层级，弹层和抽屉才可以使用明确的结构性阴影。

## Components

### Buttons

- **Shape:** 8px 圆角，默认最小高度 42px，触控环境提升到 44px。
- **Primary:** 石墨背景、白字，用于保存、提交和不可替代的主操作。
- **Hover / Focus:** 悬停仅表达状态变化，键盘焦点为红色低透明度外环。
- **Secondary:** 白色表面、细边框，用于取消、筛选和次要操作。

### Cards / Containers

- **Corner Style:** 普通面板 16px，紧凑指标 12px。
- **Background:** 白色表面置于冷白画布上。
- **Shadow Strategy:** 静态低阴影，悬停仅在确实可点击的容器上反馈。
- **Border:** `#E5E7EB` 细线，不使用彩色侧边条。
- **Internal Padding:** 18–20px，移动端按内容缩减而非压缩文字。

### Inputs / Fields

- **Style:** 白色或极浅灰背景、8px 圆角、完整可见的边界。
- **Focus:** 红色关联焦点环与清晰输入边界。
- **Error / Disabled:** 以文字、图标和颜色共同表达，不只依赖颜色。

### Navigation

- **Desktop:** 持续可见的侧栏，当前项使用浅红底与红色文本。
- **Mobile:** 首页、三个可配置快捷入口和更多组成固定底栏；更多面板保留全部模块，并在安全区上方打开。
- **Accessibility:** 所有导航目标有文本标签；更多面板支持 Esc、焦点锁定和触发点焦点恢复。

## Do's and Don'ts

### Do:

- **Do** 用 `#111827` 主按钮和 `#EF4444` 当前状态保持操作层级清晰。
- **Do** 将移动端触控目标保持在至少 44px，并给固定底栏加入安全区间距。
- **Do** 在页面自身容器中处理宽表滚动，并让地图与 iframe 使用动态视口工作区。
- **Do** 让所有关键状态同时拥有可读文本、图标或结构提示。

### Don't:

- **Don't** 把后台做成营销落地页、装饰性卡片堆叠或低信息密度仪表盘。
- **Don't** 用大面积红色、渐变文字、玻璃拟态或彩色粗侧边条作为装饰。
- **Don't** 因屏幕变窄而隐藏移动端核心功能，或让 body 级横向滚动承载宽表。
- **Don't** 用模糊兜底掩盖接口失败，也不要在页面或日志中暴露 Cookie、Token、密码、验证码或第三方登录态。
