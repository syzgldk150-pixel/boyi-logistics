"""Evidence-backed finance fee mapping baselines.

No fuzzy matching is performed here.  A source name not present in these exact
sets remains pending until a user creates a versioned binding.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.finance.models import (
    Direction,
    FeeItemKey,
    FeeLevel,
    FeeMappingSeed,
    Platform,
)


@dataclass(frozen=True)
class ExactFeeAlias:
    platform: Platform
    primary_fee_name: str
    booking_fee_name: str
    secondary_fee_name: str = ""


@dataclass(frozen=True)
class ExactOperatingFee:
    platform: Platform
    primary_fee_name: str
    secondary_fee_name: str = ""
    expense_is_cost: bool = True


@dataclass(frozen=True)
class ConfirmedFeeRule:
    platform: Platform
    primary_fee_name: str
    subject_code: str
    subject_name: str
    fee_level: FeeLevel
    requires_waybill: bool
    secondary_fee_name: str = ""


RONGHUI_CONFIRMED_FEE_RULES: tuple[ConfirmedFeeRule, ...] = (
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u76f4\u6d3e\u670d\u52a1\u8d39", "direct_delivery_service", "\u76f4\u6d3e\u670d\u52a1\u8d39", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u5305\u4ed3\u8d39", "warehouse_contract_fee", "\u5305\u4ed3\u56fa\u5b9a\u8d39", FeeLevel.OPERATING, False),
    ConfirmedFeeRule(Platform.RONGHUI, "\u4fdd\u9669\u8d39", "insurance_fee", "\u4fdd\u9669\u8d39", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u5bc4\u5230\u4ed8\u6b3e", "cod_freight_income", "\u5230\u4ed8\u8fd0\u8d39\u6536\u5165", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u7535\u5b50\u6807\u7b7e\u670d\u52a1\u8d39", "electronic_label_service", "\u7535\u5b50\u6807\u7b7e\u670d\u52a1\u8d39", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u56fa\u5b9a\u4e2d\u8f6c\u8d39", "fixed_transfer_fee", "\u56fa\u5b9a\u4e2d\u8f6c\u8d39", FeeLevel.OPERATING, False),
    ConfirmedFeeRule(Platform.RONGHUI, "\u77ed\u4fe1\u6263\u8d39", "sms_fee", "\u77ed\u4fe1\u8d39", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u4e2d\u8f6c\u8d39\u8ffd\u52a0", "transfer_fee_adjustment", "\u4e2d\u8f6c\u8d39\u8c03\u6574", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u5230\u4ed8\u6b3e\u624b\u7eed\u8d39", "cod_handling_fee", "\u5230\u4ed8\u6b3e\u624b\u7eed\u8d39", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u7535\u5b50\u56de\u5355\u670d\u52a1\u8d39", "electronic_receipt_service", "\u7535\u5b50\u56de\u5355\u670d\u52a1\u8d39", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u573a\u5730\u8d39\u6298\u8ba9", "site_fee_discount", "\u573a\u5730\u8d39\u6298\u8ba9", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u6d3e\u9001\u8d39\u6298\u8ba9", "delivery_fee_discount", "\u6d3e\u9001\u8d39\u6298\u8ba9", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u672b\u7aef\u8bf7\u8f66\u8d39", "terminal_vehicle_fee", "\u672b\u7aef\u8bf7\u8f66\u8d39", FeeLevel.WAYBILL, True),
    ConfirmedFeeRule(Platform.RONGHUI, "\u6536\u6d3e\u9001\u8d39", "delivery_fee", "\u6d3e\u9001\u8d39", FeeLevel.WAYBILL, True),
)


RONGHUI_STRICT_EXACT_ALIASES: tuple[ExactFeeAlias, ...] = (
    ExactFeeAlias(Platform.RONGHUI, "收中转费", "中转费"),
    ExactFeeAlias(Platform.RONGHUI, "收中转费折让", "中转费折让"),
    ExactFeeAlias(Platform.RONGHUI, "收代收货款手续费", "代收货款手续费"),
    ExactFeeAlias(Platform.RONGHUI, "收场地服务费", "场地服务费"),
    ExactFeeAlias(Platform.RONGHUI, "收干线费", "干线费"),
    ExactFeeAlias(Platform.RONGHUI, "收干线费折让", "干线费折让"),
    ExactFeeAlias(Platform.RONGHUI, "收干线费追加", "干线费追加"),
    ExactFeeAlias(Platform.RONGHUI, "收操作费", "操作费"),
    ExactFeeAlias(Platform.RONGHUI, "收操作费折让", "操作费折让"),
    ExactFeeAlias(Platform.RONGHUI, "收燃油费", "燃油费"),
    ExactFeeAlias(Platform.RONGHUI, "收短驳费", "短驳费"),
)


YUNDA_STRICT_EXACT_ALIASES: tuple[ExactFeeAlias, ...] = tuple(
    ExactFeeAlias(Platform.YUNDA, name, name)
    for name in (
        "中转费",
        "操作费",
        "重货服务费",
        "互助基金",
        "韵准达中转费",
        "平衡基金",
        "卸车服务费",
        "燃油调剂费",
        "韵心达服务费",
        "网建费",
        "特惠一口价",
        "服务保障费",
    )
)


STRICT_EXACT_ALIASES = RONGHUI_STRICT_EXACT_ALIASES + YUNDA_STRICT_EXACT_ALIASES


RONGHUI_BOOKING_FEE_ITEMS = frozenset(
    {
        "到付款",
        "干线费",
        "超重费",
        "等通知发货",
        "代收货款手续费",
        "末端派送费",
        "直送服务费",
        "签回单费",
        "燃油费",
        "保价金额",
        "到付手续费",
        "操作费",
        "保费",
        "中转费",
        "短驳费",
        "场地服务费",
        "大客户总运费",
        "异形件费",
        "派件费",
        "超长费",
        "管理费",
        "税金",
        "信息费",
        "上楼费",
        "围板箱费用",
        "大客户信息费",
        "干线费折让",
        "操作费折让",
        "派费折让",
        "中转费折让",
        "短驳费折让",
        "场地费折让",
        "干线费追加",
    }
)


YUNDA_BOOKING_FEE_ITEMS = frozenset(
    {
        "中转费",
        "操作费",
        "派送费",
        "特惠一口价",
        "进港服务费",
        "集货进港费",
        "出港服务费",
        "集货出港费",
        "韵安达派送费",
        "入仓服务费",
        "信息费",
        "韵准达中转费",
        "家居安装费",
        "到付款手续费",
        "开箱与码货费",
        "代收货款手续费",
        "上楼费",
        "折叠箱费",
        "韵心达服务费",
        "拆托木架费",
        "回单费",
        "韵安达中转费",
        "韵准达派送费",
        "药品类服务费",
        "拆木箱费",
        "子单费",
        "燃油调剂费",
        "重货服务费",
        "平衡基金",
        "网建费",
        "服务保障费",
        "自卸场地费",
        "互助基金",
        "乡镇平衡费",
        "乡镇派费",
        "会务费",
        "卸车服务费",
        "收件管理费",
        "特殊区域平衡费",
    }
)


YUNDA_BOOKING_FEE_GROUPS = frozenset({"集配站费用", "增值服务费", "平台费", "其他"})


def validate_booking_fee_name(
    *, platform: Platform | str, fee_level: FeeLevel | str, booking_fee_name: str
) -> str:
    """Validate a mapping target against the real waybill-entry leaf labels."""

    platform_value = Platform(platform)
    level = FeeLevel(fee_level)
    name = str(booking_fee_name or "").strip()
    if level is FeeLevel.OPERATING:
        if name:
            raise ValueError("operating mapping cannot target a waybill-entry fee item")
        return ""
    if not name:
        return ""
    allowed = (
        RONGHUI_BOOKING_FEE_ITEMS
        if platform_value is Platform.RONGHUI
        else YUNDA_BOOKING_FEE_ITEMS
    )
    if name not in allowed:
        raise ValueError("booking_fee_name is not a verified waybill-entry leaf")
    return name


# These directions and levels were explicitly confirmed against the real Yunda
# pages.  The slash in the user-confirmed meeting-fee rule represents two exact
# raw aliases; it is not a fuzzy prefix rule.
CONFIRMED_YUNDA_WAYBILL_MAPPING_SEEDS: tuple[FeeMappingSeed, ...] = (
    FeeMappingSeed(
        Platform.YUNDA,
        "开箱清点费",
        Direction.EXPENSE,
        FeeLevel.WAYBILL,
        "开箱与码货费",
        True,
    ),
    FeeMappingSeed(
        Platform.YUNDA,
        "进港服务费（带货）s",
        Direction.EXPENSE,
        FeeLevel.WAYBILL,
        "进港服务费",
        True,
    ),
    FeeMappingSeed(
        Platform.YUNDA,
        "收其他费用",
        Direction.EXPENSE,
        FeeLevel.WAYBILL,
        "会务费",
        True,
        "收其他费用-会议费",
    ),
    FeeMappingSeed(
        Platform.YUNDA,
        "派送费s",
        Direction.EXPENSE,
        FeeLevel.WAYBILL,
        "派送费",
        True,
    ),
    FeeMappingSeed(
        Platform.YUNDA,
        "回单费s",
        Direction.EXPENSE,
        FeeLevel.WAYBILL,
        "回单费",
        True,
    ),
)


CONFIRMED_OPERATING_MAPPING_SEEDS: tuple[FeeMappingSeed, ...] = (
    FeeMappingSeed(
        Platform.YUNDA,
        "派送费F(新)",
        Direction.INCOME,
        FeeLevel.OPERATING,
        "",
        False,
    ),
    FeeMappingSeed(
        Platform.YUNDA,
        "回单费f",
        Direction.INCOME,
        FeeLevel.OPERATING,
        "",
        False,
    ),
)


CONFIRMED_MAPPING_SEEDS = (
    CONFIRMED_YUNDA_WAYBILL_MAPPING_SEEDS + CONFIRMED_OPERATING_MAPPING_SEEDS
)


# Exact names verified as absent from the two platforms' waybill-entry cost
# leaves.  Direction is deliberately supplied by the observed fee item rather
# than inferred from prefixes or suffixes.
RONGHUI_VERIFIED_OPERATING_FEES: tuple[ExactOperatingFee, ...] = tuple(
    ExactOperatingFee(
        Platform.RONGHUI,
        name,
        expense_is_cost=name
        not in {"充值", "充值手续费", "预付款充值", "收云呼设备押金"},
    )
    for name in (
        "付仓储费收入",
        "付仲裁奖励",
        "付仲裁理赔款",
        "付其他费用",
        "付卸货费",
        "付品质奖励",
        "付差错奖励",
        "付操作场补贴",
        "付末端服务奖励",
        "付清点费收入",
        "付等候费",
        "付转退件费用",
        "付铁军奖励",
        "充值",
        "充值手续费",
        "增值业务调整费",
        "大货服务基金",
        "成长激励收入",
        "收云呼设备使用费",
        "收云呼设备押金",
        "收仓储费收入",
        "收仲裁理赔款",
        "收仲裁罚款",
        "收其他费用",
        "收包装修复费",
        "收卸货费",
        "收品质罚款",
        "收固定装卸费",
        "收工单罚款",
        "收差错罚款",
        "收操作罚款",
        "收改单费",
        "收查重罚款",
        "收清点费收入",
        "收物料款-工服",
        "收特殊区域加收费收入",
        "收稽查罚款",
        "收签收罚款",
        "收系统费",
        "收融速达理赔",
        "收转退件费用",
        "收进仓服务费",
        "收进仓费",
        "网点协商调账支出",
        "网点政策对赌收入",
        "网点超仓补收",
        "财务服务费-WS",
        "运单调整费",
        "预付款充值",
    )
)


YUNDA_VERIFIED_OPERATING_FEES: tuple[ExactOperatingFee, ...] = (
    ExactOperatingFee(Platform.YUNDA, "支付宝充值", expense_is_cost=False),
    ExactOperatingFee(Platform.YUNDA, "支付宝充值手续费", expense_is_cost=False),
    ExactOperatingFee(Platform.YUNDA, "系统管理费"),
    ExactOperatingFee(Platform.YUNDA, "改单费"),
    ExactOperatingFee(Platform.YUNDA, "转退件费"),
    ExactOperatingFee(Platform.YUNDA, "差错管理费s"),
    ExactOperatingFee(Platform.YUNDA, "收仲裁罚款"),
    ExactOperatingFee(Platform.YUNDA, "付仲裁奖励"),
    ExactOperatingFee(Platform.YUNDA, "网点业务量考核"),
    ExactOperatingFee(Platform.YUNDA, "加盟保证金", expense_is_cost=False),
    ExactOperatingFee(Platform.YUNDA, "装车服务费"),
    ExactOperatingFee(Platform.YUNDA, "测评考核罚款"),
    ExactOperatingFee(Platform.YUNDA, "专线费用"),
    ExactOperatingFee(Platform.YUNDA, "韵呼宝使用费"),
    ExactOperatingFee(Platform.YUNDA, "税额补收"),
    ExactOperatingFee(Platform.YUNDA, "客服类罚款", "快运工单"),
    ExactOperatingFee(Platform.YUNDA, "专线费用", "专线运输费"),
    ExactOperatingFee(Platform.YUNDA, "专线费用", "专线管理费"),
    ExactOperatingFee(Platform.YUNDA, "客服类罚款", "签收率罚款"),
    ExactOperatingFee(Platform.YUNDA, "客服类罚款", "问题件处理超时考核"),
    ExactOperatingFee(Platform.YUNDA, "客服类罚款", "问题件不规范考核"),
    ExactOperatingFee(Platform.YUNDA, "返货服务费f"),
    ExactOperatingFee(Platform.YUNDA, "调拨费F", "调拨服务费f"),
    ExactOperatingFee(Platform.YUNDA, "客服类罚款", "调拨件考核"),
    ExactOperatingFee(Platform.YUNDA, "直营窗口取件费F", "取件费F"),
)


VERIFIED_OPERATING_FEES = (
    RONGHUI_VERIFIED_OPERATING_FEES + YUNDA_VERIFIED_OPERATING_FEES
)


def mapping_seed_for_fee_item(key: FeeItemKey) -> FeeMappingSeed | None:
    """Return an exact confirmed seed for a discovered raw fee item.

    Strict recording-page aliases are classified as waybill-level for either
    observed direction, while only actual expenses enter cost.  This keeps
    direction in the binding key and does not infer it from the name.
    """

    for rule in RONGHUI_CONFIRMED_FEE_RULES:
        if (
            rule.platform is key.platform
            and rule.primary_fee_name == key.primary_fee_name
            and rule.secondary_fee_name == key.secondary_fee_name
        ):
            return FeeMappingSeed(
                platform=key.platform,
                primary_fee_name=key.primary_fee_name,
                secondary_fee_name=key.secondary_fee_name,
                direction=key.direction,
                fee_level=rule.fee_level,
                booking_fee_name="",
                include_in_cost=key.direction is Direction.EXPENSE,
                canonical_subject_code=rule.subject_code,
                canonical_subject_name=rule.subject_name,
                requires_waybill=rule.requires_waybill,
            )
    for seed in CONFIRMED_MAPPING_SEEDS:
        if (
            seed.platform is key.platform
            and seed.primary_fee_name == key.primary_fee_name
            and seed.secondary_fee_name == key.secondary_fee_name
            and seed.direction is key.direction
        ):
            return seed
    for alias in STRICT_EXACT_ALIASES:
        if (
            alias.platform is key.platform
            and alias.primary_fee_name == key.primary_fee_name
            and alias.secondary_fee_name == key.secondary_fee_name
        ):
            return FeeMappingSeed(
                platform=key.platform,
                primary_fee_name=key.primary_fee_name,
                secondary_fee_name=key.secondary_fee_name,
                direction=key.direction,
                fee_level=FeeLevel.WAYBILL,
                booking_fee_name=alias.booking_fee_name,
                include_in_cost=key.direction is Direction.EXPENSE,
            )
    for operating_fee in VERIFIED_OPERATING_FEES:
        if (
            operating_fee.platform is key.platform
            and operating_fee.primary_fee_name == key.primary_fee_name
            and operating_fee.secondary_fee_name == key.secondary_fee_name
        ):
            return FeeMappingSeed(
                platform=key.platform,
                primary_fee_name=key.primary_fee_name,
                secondary_fee_name=key.secondary_fee_name,
                direction=key.direction,
                fee_level=FeeLevel.OPERATING,
                booking_fee_name="",
                include_in_cost=(
                    key.direction is Direction.EXPENSE
                    and operating_fee.expense_is_cost
                ),
            )
    return None
