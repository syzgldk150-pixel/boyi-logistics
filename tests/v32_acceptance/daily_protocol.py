"""Loopback-only source/scan HTTP boundary for the actual daily scripts.

The production scan code calls the same menu, page, query and ``saveTables``
interfaces as the real 发件扫描 page; readback uses the same server ledger.
No production business function is replaced.
"""
from __future__ import annotations

from contextlib import ExitStack
from email.parser import BytesParser
from email.policy import HTTP
from hashlib import sha256
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse, quote
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests


LOGIN = {"loginUserName": "isolated", "loginUserAccount": "v32-scan",
    "loginSiteName": "隔离操作场", "loginSiteCode": "73901"}
MAIN_CODE = "R12345678901"
CHILD_CODE = MAIN_CODE + "0001"
STATION = "隔离目的站"
ACCOUNT_ID = "v32-daily-account"


class DailyAccounts:
    def list_accounts(self, **_options):
        return [{"account_id": ACCOUNT_ID, "system": "ronghui", "is_active": True,
            "label": "隔离日常账号", "account_purpose": "general", "session_profile": "v32-daily-profile"}]

    def require_active_binding_descriptor(self, account_id):
        if account_id != ACCOUNT_ID:
            raise ValueError("unknown isolated daily account")
        return self.list_accounts()[0]

    def public_credentials(self, account_id):
        self.require_active_binding_descriptor(account_id)
        return {"username": LOGIN['loginUserAccount']}

MENU = {'result': {'data': [{'text': '扫描管理', 'children': [{'text': '发件扫描',
    'url': '/widget/home?authenticationKey=isolated-public-page&pageId=isolated-page'}]}]}}
SCAN_PAGE = '<!doctype html><meta charset="utf-8"><script>new $Z.Parameter("TAB_SCAN_SEND_ADD")</script>'


def _multipart_fields(content_type, body):
    message = BytesParser(policy=HTTP).parsebytes(b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body)
    return {part.get_param('name', header='content-disposition'): part.get_payload(decode=True).decode('utf-8')
            for part in message.iter_parts()}


class DailyProtocol:
    def __init__(self):
        self.requests = []
        self.ledger = []
        stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        self.source_rows = [{'BILL_CODE': CHILD_CODE, 'DESTINATION': STATION, 'SCAN_TYPE': '到件',
            'SCAN_DATE': stamp, 'SCAN_SITE': LOGIN['loginSiteName']}]
        route_id = 'automation.feishu_route.scan_codes'
        route = {'resource_kind': 'feishu_route', 'command': 'synthetic-a01-scan'}
        route['_meta'] = {'resource_key': route_id, 'configuration_version': 1,
            'config_sha256': sha256(json.dumps(route, sort_keys=True).encode()).hexdigest(),
            'source': 'explicit isolated synthetic route'}
        self.resources = {route_id: route}
        webhook = {'resource_kind': 'webhook_route', 'path': 'webhook/isolated/a01/scan'}
        webhook['_meta'] = {'resource_key': 'phase7.scan_webhook', 'configuration_version': 1,
            'config_sha256': sha256(json.dumps(webhook, sort_keys=True).encode()).hexdigest(),
            'source': 'explicit isolated synthetic route'}
        self.resources['phase7.scan_webhook'] = webhook
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                location = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                content_type = self.headers.get('Content-Type', '')
                if body and 'multipart/form-data' in content_type:
                    arguments = _multipart_fields(content_type, body)
                elif body and 'application/json' in content_type:
                    arguments = json.loads(body)
                else:
                    arguments = parse_qs(body.decode())
                with owner.lock:
                    owner.requests.append({'method': self.command, 'path': location.path, 'action': parse_qs(location.query).get('id', [''])[0]})
                try:
                    result = owner.handle(location.path, parse_qs(location.query), arguments)
                    payload = result.encode() if isinstance(result, str) else json.dumps(result, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8' if isinstance(result, str) else 'application/json')
                except Exception as error:
                    payload = json.dumps({'error': type(error).__name__ + ': ' + str(error)}).encode()
                    self.send_response(400)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def handle(self, path, query, arguments):
        if path == '/menuTreeExtend/loadMenu':
            return MENU
        if path == '/widget/home':
            return SCAN_PAGE
        action = query.get('id', [''])[0]
        if action == 'FIND_SCAN_SEND_SITE_COMBOBOX_NEW':
            if query.get('key') != [STATION]:
                raise ValueError('station lookup differs from the isolated source')
            return {'data': [{'SITE_NAME': STATION, 'SITE_CODE': 'isolated-station-code'}], 'total': 1}
        if action == 'FIND_TMS_SYS_SHARE_SET':
            return [{'SHARE_VALUE': '^R[0-9]+$'}]
        if action == 'FIND_WAYBILL_SIGN_STATE':
            return []
        if path == '/dataOperation/saveTables':
            operations = json.loads(arguments['params']) if isinstance(arguments, dict) and isinstance(arguments.get('params'), str) else None
            if not isinstance(operations, list) or len(operations) != 1 or operations[0].get('operationKey') != 'TAB_SCAN_SEND_ADD':
                raise ValueError('unexpected scan mutation contract')
            rows = operations[0]['data']
            for row in rows:
                if row.get('BILL_CODE') != CHILD_CODE or row.get('PRE_OR_NEXT_STATION') != STATION:
                    raise ValueError('scan target differs from the isolated source')
                if row.get('SCAN_SITE_CODE') != LOGIN['loginSiteCode'] or row.get('SCAN_TYPE') != '发件':
                    raise ValueError('scan context differs from the selected account')
            with self.lock:
                for row in rows:
                    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
                    self.ledger.append({**row, 'ROW_ID': f'isolated-row-{len(self.ledger)+1}', 'SCAN_DATE': stamp, 'REGISTER_DATE': stamp})
            return {'success': True, 'message': 'isolated scan rows committed'}
        if action == 'FIND_SEND_SCAN_RECORD':
            codes = set(arguments['BILL_CODE'][0].split(','))
            rows = [row for row in self.ledger if row['BILL_CODE'] in codes]
            return {'data': rows, 'total': len(rows)}
        if action == 'FIND_COME_SCAN_RECORD':
            page = int(arguments['pageIndex'][0])
            size = int(arguments['pageSize'][0])
            return {'data': self.source_rows[page * size:(page + 1) * size], 'total': len(self.source_rows)}
        if path == '/capability':
            if arguments['account_id'] != ACCOUNT_ID:
                raise ValueError('unbound isolated capability account')
            return {'authorized': True, 'account_id': ACCOUNT_ID, 'capability': arguments['capability']}
        raise ValueError('unimplemented isolated protocol path: ' + path + '/' + action)

    def session(self):
        server_url = self.url

        class Session(requests.Session):
            def request(self, method, url, **kwargs):
                target = urlparse(url)
                if target.hostname not in {'127.0.0.1', 'tms.ronghuiwl.com'}:
                    raise RuntimeError('isolated protocol refuses unapproved external host')
                local_url = server_url + target.path + (('?' + target.query) if target.query else '')
                kwargs['allow_redirects'] = False
                return super().request(method, local_url, **kwargs)

        session = Session()
        session.trust_env = False
        session.cookies.set('userInfo', quote(json.dumps(LOGIN, ensure_ascii=False)))
        return session

    def authorize(self, descriptor, capability):
        account_id = descriptor.get('account_id')
        with self.session() as session:
            response = session.post(self.url + '/capability', json={'account_id': account_id, 'capability': capability}, timeout=10)
            response.raise_for_status()
            result = response.json()
        if result != {'authorized': True, 'account_id': ACCOUNT_ID, 'capability': capability}:
            raise ValueError('isolated capability response differs')

    def resource_loader(self, resource_id):
        return self.resources.get(resource_id)

    def authentication_boundaries(self):
        from agent.tms_runtime.scripts import scan_next
        stack = ExitStack()
        owner = self

        class Auth:
            def __init__(self, *, profile=None, **_kwargs):
                if profile != 'v32-daily-profile':
                    raise RuntimeError('unbound isolated account profile')

            def login_and_get_session(self):
                return owner.session()

        stack.enter_context(patch('agent.tms_runtime.scripts.login_manager.TMSAuth', Auth))
        stack.enter_context(patch.object(scan_next, 'TMSAuth', Auth))
        return stack

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


def probe_scan_http():
    from plugin_core_adapters.host_primitives import _scan_next_submit, _scan_next_verify
    with DailyProtocol() as boundary, boundary.authentication_boundaries():
        descriptor = {'session_profile': 'v32-daily-profile'}
        items = [{'bill_code': CHILD_CODE, 'station_name': STATION}]
        result = _scan_next_submit(descriptor, items)
        if result.get('ok') is not True:
            raise AssertionError(result)
        verified = _scan_next_verify(descriptor, items, result['write_started_at'], result['write_finished_at'])
        assert len(boundary.ledger) == 1
        return {'executed_at': datetime.now(timezone.utc).isoformat(), 'result': result, 'verified': verified,
            'actual_http_requests': boundary.requests, 'side_effect_rows': len(boundary.ledger)}


if __name__ == '__main__':
    output = Path(__file__).resolve().parents[2] / '.task_tmp' / 'v32' / 'reliability' / 'scan-http-boundary.json'
    output.write_text(json.dumps(probe_scan_http(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
