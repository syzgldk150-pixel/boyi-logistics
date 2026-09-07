"""Local candidate decision checks; invoked with the extracted candidate on PYTHONPATH."""
from __future__ import annotations
import argparse
import json

from payload.action import _collect_candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected', type=int, choices=(1, 2), required=True)
    args = parser.parse_args()
    rows = [['运单编号', '目的站点', '派送方式', '累计到货件数', '件数'],
        ['SYNTHETIC-REGULAR', '邵阳大祥S站', '自提', '1', '1'],
        ['SYNTHETIC-RESERVED', '邵阳大祥S站', '预约自提', '1', '1'],
        ['SYNTHETIC-DELIVERY', '邵阳大祥S站', '派送', '1', '1'],
        ['SYNTHETIC-INCOMPLETE', '邵阳大祥S站', '自提', '0', '1']]
    candidates, duplicate_count = _collect_candidates(rows, include_daxiang=True, limit=None)
    expected = ['SYNTHETIC-REGULAR'] + (['SYNTHETIC-RESERVED'] if args.expected == 2 else [])
    assert sorted(item['bill_code'] for item in candidates) == expected
    assert duplicate_count == 0
    try:
        _collect_candidates([['运单编号']], include_daxiang=True, limit=None)
    except ValueError:
        pass
    else:
        raise AssertionError('missing source fields must explicitly fail')
    print(json.dumps({'status':'PASS', 'selected':expected, 'missing_fields_rejected':True}))


if __name__ == '__main__':
    raise SystemExit(main())
