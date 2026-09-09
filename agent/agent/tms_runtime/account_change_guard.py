"""Keep live invocation ownership and the existing durable account guard together."""
from contextlib import ExitStack


def compose_account_change_guards(direct_guard, durable_guard):
    def begin(account_id):
        with ExitStack() as held:
            held.callback(direct_guard(account_id))
            held.callback(durable_guard(account_id))
            return held.pop_all().close
    return begin
