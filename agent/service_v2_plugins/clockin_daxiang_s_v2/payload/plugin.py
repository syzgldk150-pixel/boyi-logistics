"""Immutable identity and site guard for the 大祥 S 站 v2 package."""

PLUGIN_ID = "clockin_daxiang_s_v2"
SERVICE_NAME = "plugin.clockin_daxiang_s_v2.clock@1"
CONTRIBUTION_ID = "manual_run"
CONTRIBUTION_TARGETS = {
    "console": (CONTRIBUTION_ID, "console"),
    "scheduler": ("daily_clockin", "scheduler"),
    "harness": ("assistant_preview", "harness"),
    "service": ("host.service.invoke", "service"),
}
EXPECTED_SITE_NAME = "邵阳大祥S站"
