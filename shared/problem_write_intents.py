"""Durable per-business-target write facts, independent of request/credential IDs.

An absent readback cannot release a started attempt: an accepted remote write
may still arrive. Only its explicit rejection or authoritative applied proof
can settle it. Nothing here schedules or resumes work.
"""
from hashlib import sha256
import json
from uuid import uuid4


class UnresolvedProblemWrite(RuntimeError):
    pass


def problem_target(site_code, plan):
    fields = [site_code, *(plan.get(key) for key in
              ('bill_code', 'problem_type', 'problem_owner_type', 'problem_cause_sha256'))]
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        raise ValueError('authoritative site and complete problem business identity are required')
    return sha256(json.dumps(['ronghui.problem', *fields], ensure_ascii=False,
                             separators=(',', ':')).encode()).hexdigest()


class ProblemWriteIntents:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def reserve(self, target):
        attempt = str(uuid4())
        with self._connect() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('''INSERT IGNORE INTO ronghui_problem_write_intents
                        (target_sha256,attempt_id,outcome,created_at,updated_at)
                        VALUES(%s,%s,'STARTED',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))''', (target, attempt))
                    cursor.execute('SELECT attempt_id,outcome FROM ronghui_problem_write_intents WHERE target_sha256=%s FOR UPDATE', (target,))
                    row = cursor.fetchone()
                    if row['attempt_id'] != attempt:
                        if row['outcome'] != 'NOT_APPLIED':
                            raise UnresolvedProblemWrite('this business target requires authoritative readback; no new write was sent')
                        cursor.execute("UPDATE ronghui_problem_write_intents SET attempt_id=%s,outcome='STARTED',updated_at=UTC_TIMESTAMP(6) WHERE target_sha256=%s", (attempt, target))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return attempt

    def settle(self, target, attempt, outcome):
        if outcome not in {'NOT_APPLIED', 'VERIFIED'}:
            raise ValueError('write settlement must be authoritative')
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute('''UPDATE ronghui_problem_write_intents SET outcome=%s,updated_at=UTC_TIMESTAMP(6)
                WHERE target_sha256=%s AND attempt_id=%s AND outcome='STARTED' ''', (outcome, target, attempt))
            if cursor.rowcount != 1:
                raise UnresolvedProblemWrite('write attempt ownership changed before settlement')
            connection.commit()
