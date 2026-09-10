"""An unknown historical write does not reserve a physical resource forever."""

from tests.v32_acceptance import scan_recovery


def exercise(management, runner, boundary, unrelated, connection_factory):
    boundary.mode = 'SUCCESS'
    preview = scan_recovery.invoke(management, runner)
    actual_scan = scan_recovery.invoke(management, runner, preview_invocation_id=preview['invocation_id'])
    assert actual_scan['status'] == 'COMPLETED', actual_scan
    boundary.mode = 'SHEET_UNKNOWN'
    stopped = scan_recovery.invoke(management, runner, automation_id='arrival_stats')
    assert stopped['status'] == 'WRITE_OUTCOME_UNKNOWN', stopped
    assert boundary.sheet_writes
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT * FROM automation_write_attempt_receipts WHERE invocation_id=%s ORDER BY receipt_id', (stopped['invocation_id'],))
        saved_receipts = cursor.fetchall()
    assert saved_receipts and all(row['orchestration_run_id'] is None for row in saved_receipts)
    assert runner.service.active_invocations() == []
    boundary.mode = 'SUCCESS'
    continued = scan_recovery.invoke(management, runner, automation_id=unrelated)
    assert continued['status'] == 'COMPLETED', continued
    checks = []
    # These are fresh, explicit HTTP invocations of the actual statistics
    # package and its same physical Sheet, not a synthetic lease or queue.
    for number in range(20):
        refreshed = scan_recovery.invoke(management, runner, automation_id='arrival_stats')
        assert refreshed['status'] == 'COMPLETED', refreshed
        assert refreshed['invocation_id'] != stopped['invocation_id']
        assert runner.service.get(stopped['invocation_id'])['status'] == 'WRITE_OUTCOME_UNKNOWN'
        checks.append({'round': number, 'result': refreshed})
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT * FROM automation_write_attempt_receipts WHERE invocation_id=%s ORDER BY receipt_id', (stopped['invocation_id'],))
        assert cursor.fetchall() == saved_receipts
    return {'status': 'PASS', 'original_invocation': stopped,
        'scope': 'Actual installed scan/statistics packages and MySQL Invocation write receipts; every subsequent write executes through the same bound physical Sheet.',
        'history_remains_unknown_and_does_not_hold_resources': True,
        'same_physical_target_checks': checks, 'independent_invocation': continued,
        'historical_receipts_unchanged': True, 'sheet_side_effects': len(boundary.sheet_writes)}
