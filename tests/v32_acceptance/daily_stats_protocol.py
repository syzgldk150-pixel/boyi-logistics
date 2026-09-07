"""Raw dispatch and sparse sheet HTTP ledger for real arrival statistics."""
from hashlib import sha256
import json
import re
from unittest.mock import patch

from tests.v32_acceptance.daily_protocol import DailyProtocol, MAIN_CODE, STATION


class DailyStatsProtocol(DailyProtocol):
    def __init__(self):
        super().__init__()
        self.sheet_cells = {}
        self.sheet_writes = []
        self.waybills = [{
            'BILL_CODE': MAIN_CODE, 'GOODS_NAME': '合成配件', 'PACK_TYPE': '纸箱', 'DISPATCH_MODE': '自提',
            'PIECE_NUMBER': 2, 'R_BILLCODE': '合成回单', 'BILL_WEIGHT': '10.00', 'VOLUME': '0.100',
            'REMARK': '隔离验收', 'DESTINATION': STATION, 'ACCEPT_MAN': '合成收货方',
            'ACCEPT_MAN_PHONE': 'synthetic-phone', 'ACCEPT_MAN_ADDRESS': '隔离测试区域合成街道一号',
            'SETTLEMENT_WEIGHT': '10.00', 'VOLUME_WEIGHT': '10.00', 'FREIGHT': '20.00',
            'PAYMENT_TYPE': '现付', 'TOPAYMENT': '0.00',
        }]
        for resource_id, sheet in (('phase7.arrive_primary_sheet', 'primary'),
                ('phase7.arrive_secondary_sheet', 'secondary'), ('phase7.stats_archive_sheet', 'archive'),
                ('phase7.split_pending_target_sheet', 'split')):
            self._resource(resource_id, {'resource_kind': 'feishu_sheet', 'spreadsheet_token': 'isolated-daily-workbook',
                'sheet_id': sheet, 'range': f'{sheet}!A1:S30', 'clear_range': f'{sheet}!A1:S30'})
        self._resource('automation.feishu_route.arrival_stats', {'resource_kind': 'feishu_route', 'command': 'synthetic-a01-stats'})
        self._resource('phase7.stats_webhook', {'resource_kind': 'webhook_route', 'path': 'webhook/isolated/a01/stats'})

    def _resource(self, identity, data):
        data['_meta'] = {'resource_key': identity, 'configuration_version': 1,
            'config_sha256': sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
            'source': 'explicit isolated synthetic resource'}
        self.resources[identity] = data

    def handle(self, path, query, arguments):
        if query.get('id', [''])[0] == 'FIND_DISPATCH_FORECAST_CENTER':
            page, size = int(arguments['pageIndex'][0]), int(arguments['pageSize'][0])
            return {'data': self.waybills[page * size:(page + 1) * size], 'total': len(self.waybills)}
        if path == '/sheet':
            return self.sheet_request(arguments['action'], arguments['params'])
        return super().handle(path, query, arguments)

    def sheet_request(self, action, params):
        if params.get('spreadsheet_token') != 'isolated-daily-workbook':
            raise ValueError('unbound isolated workbook')
        matched = re.fullmatch(r'([a-z]+)!([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)', params['range'])
        if matched is None or matched[1] not in {'primary', 'secondary', 'split', 'archive'}:
            raise ValueError('unbound isolated sheet range')
        def column(value):
            result = 0
            for letter in value:
                result = result * 26 + ord(letter) - ord('A') + 1
            return result
        sheet, first_col, first_row, last_col, last_row = matched.groups()
        first_col, last_col = column(first_col), column(last_col)
        first_row, last_row = int(first_row), int(last_row)
        if last_row > 2000 or last_col > 40:
            raise ValueError('isolated sheet range exceeds its boundary')
        with self.lock:
            if action in {'write_sheet', 'clear_sheet'}:
                self.sheet_writes.append({'action': action, 'range': params['range']})
                values = params.get('values') if action == 'write_sheet' else [[''] * (last_col - first_col + 1) for _ in range(last_row - first_row + 1)]
                if not isinstance(values, list) or len(values) > last_row - first_row + 1:
                    raise ValueError('isolated sheet payload differs from range')
                for row_offset, row in enumerate(values):
                    if len(row) > last_col - first_col + 1:
                        raise ValueError('isolated sheet payload has excess columns')
                    for col_offset, value in enumerate(row):
                        self.sheet_cells[sheet, first_row + row_offset, first_col + col_offset] = value
                return {'ok': True, 'code': 0, 'data': {}}
            if action != 'read_sheet':
                raise ValueError('unimplemented isolated sheet operation')
            values = [[self.sheet_cells.get((sheet, row, col), '') for col in range(first_col, last_col + 1)]
                for row in range(first_row, last_row + 1)]
            return {'ok': True, 'code': 0, 'data': {'values': values}}

    def feishu_operation(self, action, params):
        with self.session() as session:
            response = session.post(self.url + '/sheet', json={'action': action, 'params': params}, timeout=10)
            response.raise_for_status()
            return response.json()

    def authentication_boundaries(self):
        stack = super().authentication_boundaries()
        # Only the resource catalog and remote sheet transport are substituted.
        # Production formatting, clear/write order, MySQL and readback remain.
        stack.enter_context(patch('plugin_core_adapters.arrival._load_resource', self.resource_loader))
        stack.enter_context(patch('tools.feishu_cli_tool.feishu_operation', self.feishu_operation))
        return stack
