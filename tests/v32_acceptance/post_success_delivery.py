"""A23 fault ports around actual completed business and production Outbox code."""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from typing import Any
import uuid

import httpx

from agent.orchestration.models import RunStatus
from agent.orchestration.outbox_dispatcher import OutboxDispatcher


def completion_projector():
    # Import only these exact production definitions: main startup must never
    # load deployment configuration or start real Feishu/Scheduler services.
    path = Path(__file__).resolve().parents[2] / 'agent/main.py'
    names = {'_run_plan_steps', '_project_run_completed_event', 'FINANCE_FAILURE_RUN_STATUSES'}
    nodes = []
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets):
            nodes.append(node)
    if len(nodes) != len(names):
        raise AssertionError('Production completion projector definitions changed')
    namespace = {'Any': Any, 'RunStatus': RunStatus, 'datetime': datetime, 'timezone': timezone, 'uuid': uuid}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['_project_run_completed_event']


def exercise_post_success(*, management, connection_factory, run_ids, external_state):
    originals = {key: management.repository.get_run(value) for key, value in run_ids.items()}
    assert all(row['status'] == 'COMPLETED' for row in originals.values())
    before = deepcopy(external_state())
    run_id = run_ids['scan_codes']
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT event_id FROM domain_events WHERE run_id=%s AND event_type='agent.run.status_changed' AND JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.to'))='COMPLETED'", (run_id,))
        events = cursor.fetchall()
        assert len(events) == 1
        event_id = events[0]['event_id']
        # Two test consumers of the *actual* completion event. These are fault
        # ports, not new production subscriptions or fabricated business runs.
        for consumer in ('v32.a23.notification', 'v32.a23.display'):
            cursor.execute("INSERT INTO outbox_events(event_id,consumer_name,topic,partition_key,status,available_at,max_attempts) VALUES(%s,%s,'agent.run.status_changed',%s,'PENDING',NOW(6),10)",
                (event_id, consumer, run_id))
        connection.commit()

    fault = {'notification': True, 'display': True}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body == {'event_id': event_id, 'run_id': run_id}
            status = 503 if fault['notification'] else 200
            requests.append({'event_id': body['event_id'], 'status': status})
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    projector = completion_projector()

    def notify(delivery, _uow):
        httpx.post(f'http://127.0.0.1:{server.server_port}/notify',
            json={'event_id': delivery['event_id'], 'run_id': delivery['run_id']}, timeout=2).raise_for_status()
        return {'delivered': True}

    def display(delivery, uow):
        result = projector(delivery, uow)
        assert result['projected'] is True
        if fault['display']:
            raise RuntimeError('A23 injected projection failure after actual SQL before commit')
        return result

    def business_unchanged():
        assert external_state() == before, 'post-success retry repeated external business writes'
        for key, identity in run_ids.items():
            current = management.repository.get_run(identity)
            assert current['status'] == 'COMPLETED'
            assert current['execution_attempt_count'] == originals[key]['execution_attempt_count']
            assert current['steps'] == originals[key]['steps']

    outcomes = {}
    try:
        for kind, handler in (('notification', notify), ('display', display)):
            consumer = 'v32.a23.' + kind
            dispatcher = OutboxDispatcher(management.repository, worker_id=consumer, consumer_name=consumer,
                handlers={consumer: handler}, handler_uses_uow=(kind == 'display'))
            deliveries = dispatcher._claim()
            assert len(deliveries) == 1
            dispatcher._deliver(deliveries[0])
            business_unchanged()
            with connection_factory() as connection, connection.cursor() as cursor:
                cursor.execute('SELECT status,attempt_count,last_error_code,available_at FROM outbox_events WHERE event_id=%s AND consumer_name=%s', (event_id, consumer))
                failed = cursor.fetchone()
                assert failed['status'] == 'PENDING' and failed['attempt_count'] == 1 and failed['last_error_code']
                cursor.execute("SELECT COUNT(*) AS total FROM domain_events WHERE event_type='agent.run.completed' AND run_id=%s", (run_id,))
                assert cursor.fetchone()['total'] == 0, 'failed display transaction leaked a projection'
            fault[kind] = False
            deadline = time.monotonic() + 8
            retried = []
            while not retried and time.monotonic() < deadline:
                retried = dispatcher._claim()
                if not retried:
                    time.sleep(.05)
            assert len(retried) == 1, 'actual default retry delay failed to become due'
            dispatcher._deliver(retried[0])
            business_unchanged()
            with connection_factory() as connection, connection.cursor() as cursor:
                cursor.execute('SELECT status,attempt_count,last_error_code FROM outbox_events WHERE event_id=%s AND consumer_name=%s', (event_id, consumer))
                recovered = cursor.fetchone()
                assert recovered == {'status': 'PUBLISHED', 'attempt_count': 2, 'last_error_code': None}
            outcomes[kind] = {'failed': failed, 'recovered': recovered}
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM domain_events WHERE event_type='agent.run.completed' AND run_id=%s", (run_id,))
            projections = cursor.fetchone()['total']
            assert projections == 1
        assert [item['status'] for item in requests] == [503, 200]
        return {'status': 'PASS', 'fault_scope': 'two isolated Outbox consumers of one real completed event; loopback notification HTTP and production completion projector',
            'outcomes': outcomes, 'notification_http': requests, 'actual_completed_projections': projections,
            'business_run_ids': run_ids, 'external_state_before': before, 'external_state_after': external_state()}
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
