"""Run the real daily-sign rules, source pagination and publication checks."""
from daily_sign_io import use_broker, get_workflow_resource
from business.daily_sign_sync_tool import run_daily_sign_sync

def run_action(arguments, broker):
    with use_broker(broker):
        resource = get_workflow_resource('phase7.daily_sign_sheet')
        return run_daily_sign_sync({**arguments, 'account_id':'daily_sign_tms',
            'r13_account_id':'daily_sign_r13', 'base_token':'daily_sign_bitable',
            # The Host tracking endpoint admits one active request. Keep exact
            # checks sequential so requests from this invocation do not collide.
            'table_id':'daily_sign_bitable', 'exact_sign_workers':1, **resource})
