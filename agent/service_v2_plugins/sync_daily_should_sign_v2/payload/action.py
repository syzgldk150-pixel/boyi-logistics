"""Publish the complete R13 unsigned snapshot with cumulative statistics."""
from daily_sign_io import use_broker, get_workflow_resource
from business.daily_sign_sync_tool import run_daily_sign_sync

def run_action(arguments, broker):
    with use_broker(broker):
        resource = get_workflow_resource('phase7.daily_sign_sheet')
        return run_daily_sign_sync({**arguments,
            'r13_account_id':'daily_sign_r13', 'base_token':'daily_sign_bitable',
            'table_id':'daily_sign_bitable', **resource})
