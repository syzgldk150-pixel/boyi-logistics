"""Controlled raw sheet/TMS HTTP boundaries for real problem plugins."""
from __future__ import annotations

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading

import httpx

ACCOUNTS = {"account_id": "v32-problem-primary", "daxiang_s_account_id": "v32-problem-daxiang"}
RESOURCE_ID = "phase7.self_pickup_source_sheet"
SPLIT_SOURCE = 'phase7.split_pending_source_sheet'
SPLIT_TARGET = 'phase7.split_pending_target_sheet'


class ProblemAccounts:
    def list_accounts(self, **_options):
        return [{"account_id": value, "system": "ronghui", "is_active": True,
            "label": "隔离问题件账号 " + role, "account_purpose": "general",
            "name": "隔离问题件账号 " + role, "system_label": "融辉", "status_label": "隔离测试可用",
            "session_profile": "synthetic-" + value} for role, value in ACCOUNTS.items()]

    def require_active_binding_descriptor(self, account_id):
        matches = [item for item in self.list_accounts() if item["account_id"] == account_id]
        if len(matches) != 1:
            raise ValueError("unknown isolated account")
        return matches[0]

    def public_credentials(self, account_id):
        self.require_active_binding_descriptor(account_id)
        return {"username": "synthetic-login-" + account_id}


class ProblemSupplier:
    def __init__(self, runtime_root):
        self.requests = []
        self.database = runtime_root / "synthetic-supplier.sqlite"
        runtime_root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS problems (account_id TEXT NOT NULL, bill_code TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,bill_code))")
            connection.execute('CREATE TABLE IF NOT EXISTS sheets (sheet_id TEXT PRIMARY KEY, rows_json TEXT NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO sheets VALUES (?,?)', ('synthetic_split_target','[]'))
        self.rows = [["运单编号", "货物名称", "派送方式", "件数", "目的站点", "累计到货件数"],
            ["R_M03_STANDARD", "合成配件", "自提", "1", "邵阳大祥S站", "1"],
            ["R_M03_RESERVED", "合成配件", "预约自提", "1", "邵阳大祥S站", "1"]]
        resource = {"resource_kind": "feishu_sheet", "spreadsheet_token": "synthetic-workbook-m03",
            "sheet_id": "synthetic_sheet", "range": "synthetic_sheet!A1:S2000",
            "formula_source_sheet_id": "synthetic_sheet", "formula_source_range": "synthetic_sheet!A1:S2000"}
        resource["_meta"] = {"resource_key": RESOURCE_ID, "configuration_version": 1,
            "config_sha256": sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest(),
            "source": "explicit isolated synthetic resource"}
        self.resources = {RESOURCE_ID: resource}
        route = {"resource_kind": "feishu_route", "command": "synthetic-m03-self-pickup"}
        route["_meta"] = {"resource_key": "automation.feishu_route.self_pickup_problem_upload",
            "configuration_version": 1, "config_sha256": sha256(json.dumps(route, sort_keys=True).encode()).hexdigest(),
            "source": "explicit isolated synthetic route"}
        self.resources["automation.feishu_route.self_pickup_problem_upload"] = route
        split_headers = ['运单编号','货物名称','包装类型','派送方式','件数','回单号','实际重量','体积','备注','目的站点','收件人','收件电话','收件地址','结算重量','体积重','运费','支付类型','到付款','累计到货件数']
        self.split_rows = [split_headers]
        for code, expected, arrived in [('SYNTHETIC-SPLIT',2,1),('SYNTHETIC-NOT-ARRIVED',2,0),('SYNTHETIC-COMPLETE',1,1)]:
            self.split_rows.append([code,'合成配件','纸箱','派送',str(expected),'','','','','合成目的站','','','','','','','','',str(arrived)])
        for role, sheet in [(SPLIT_SOURCE,'synthetic_split_source'),(SPLIT_TARGET,'synthetic_split_target')]:
            value = {'resource_kind':'feishu_sheet','spreadsheet_token':'synthetic-workbook-split',
                'sheet_id':sheet,'range':sheet+'!A1:S5000','clear_range':sheet+'!A1:S5000'}
            value['_meta'] = {'resource_key':role,'configuration_version':1,
                'config_sha256':sha256(json.dumps(value,sort_keys=True).encode()).hexdigest(),
                'source':'explicit isolated split sheet'}
            self.resources[role] = value
        split_route = {'resource_kind':'feishu_route','command':'synthetic-split-confirm'}
        split_route['_meta'] = {'resource_key':'automation.feishu_route.split_pending_problem_upload','configuration_version':1,
            'config_sha256':sha256(json.dumps(split_route,sort_keys=True).encode()).hexdigest(),'source':'explicit isolated split route'}
        self.resources['automation.feishu_route.split_pending_problem_upload'] = split_route
        supplier = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 1000000:
                    self.send_error(400)
                    return
                request = json.loads(self.rfile.read(size))
                supplier.requests.append({"path": self.path, "action": request.get("action"),
                    "request_sha256": sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()})
                if self.path == '/problem' and request.get('account') in ACCOUNTS.values():
                    supplier.requests[-1].update(synthetic_account_id=request['account'],
                        synthetic_bill_code=request['plan']['bill_code'])
                if self.path == "/sheet":
                    params = request["params"]
                    action = request['action']
                    token = params['spreadsheet_token']
                    sheet = params['range'].split('!',1)[0]
                    if token == 'synthetic-workbook-m03' and action == 'read_sheet':
                        result = {'data':{'values':supplier.rows}}
                    elif token == 'synthetic-workbook-split' and sheet == 'synthetic_split_source' and action == 'read_sheet':
                        result = {'data':{'values':supplier.split_rows}}
                    elif token == 'synthetic-workbook-split' and sheet == 'synthetic_split_target' and action in {'read_sheet','clear_sheet','write_sheet'}:
                        with sqlite3.connect(supplier.database) as connection:
                            if action in {'clear_sheet','write_sheet'}:
                                values = [] if action == 'clear_sheet' else params['values']
                                connection.execute('UPDATE sheets SET rows_json=? WHERE sheet_id=?',(json.dumps(values),'synthetic_split_target'))
                            saved = json.loads(connection.execute('SELECT rows_json FROM sheets WHERE sheet_id=?',('synthetic_split_target',)).fetchone()[0])
                        result = {'data':{'values':saved}}
                    else:
                        self.send_error(403)
                        return
                elif self.path == "/capability":
                    if request["account"] not in ACCOUNTS.values():
                        self.send_error(403)
                        return
                    result = {"authorized": True, "account": request["account"], "capability": request["capability"]}
                elif self.path == "/problem":
                    if request["account"] not in ACCOUNTS.values():
                        self.send_error(403)
                        return
                    plan, action = request["plan"], request["action"]
                    code = plan["bill_code"]
                    with sqlite3.connect(supplier.database) as connection:
                        existing = connection.execute("SELECT payload FROM problems WHERE account_id=? AND bill_code=?", (request['account'], code)).fetchone()
                        if action == "query":
                            result = {"ready": True, "existing": json.loads(existing[0]) if existing else None}
                        elif action == "create":
                            if existing:
                                result = json.loads(existing[0])
                                if any(result[key] != plan[key] for key in ('problem_cause_sha256','problem_owner_type','problem_type')):
                                    self.send_error(409)
                                    return
                                result['idempotent'] = True
                            else:
                                result = {"bill_code": code, "external_id": "synthetic-problem-" + code,
                                    "postpone_updated": False, "problem_cause_sha256": plan["problem_cause_sha256"],
                                    "problem_owner_type": plan["problem_owner_type"], "problem_type": plan["problem_type"],
                                    "registered_at": "2026-09-07 10:00:00", "registered_site": "隔离合成站点",
                                    "saved": True, "verified": True}
                                connection.execute("INSERT INTO problems VALUES (?,?,?)", (request['account'], code, json.dumps(result)))
                        elif action == "verify" and existing:
                            result = json.loads(existing[0])
                            result["confirmed"] = result["external_id"] == plan["external_id"]
                        else:
                            self.send_error(400)
                            return
                else:
                    self.send_error(404)
                    return
                body = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _post(self, path, payload):
        response = httpx.post(self.url + path, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()

    def resource_loader(self, resource_id):
        return self.resources.get(resource_id)

    def persisted_problems(self):
        with sqlite3.connect(self.database) as connection:
            return [json.loads(row[0]) for row in connection.execute('SELECT payload FROM problems ORDER BY bill_code')]

    def feishu_operation(self, action, params):
        return self._post("/sheet", {"action": action, "params": params})

    def problem_action(self, descriptor, action, plan):
        return self._post("/problem", {"account": descriptor["account_id"], "action": action, "plan": dict(plan)})

    def authorize(self, descriptor, capability):
        payload = {"account": descriptor["account_id"], "capability": capability}
        if self._post("/capability", payload) != {"authorized": True, **payload}:
            raise RuntimeError("isolated capability identity mismatch")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_error):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
