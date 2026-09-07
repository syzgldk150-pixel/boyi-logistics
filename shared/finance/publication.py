"""One publication boundary shared by finance BI and derived facts.

Each source/day is an independent partition. A completed partition in a
partial-failed batch is visible only after that batch is finalized. Incomplete
or interrupted batches never replace the previous published partition.
"""

PUBLISHED_BATCH_PREDICATE = """EXISTS (
    SELECT 1 FROM finance_sync_batches published_batch
    WHERE published_batch.id = finance_sync_runs.batch_id
      AND published_batch.status IN ('success','no_data','partial_failed')
      AND published_batch.finished_at IS NOT NULL
)"""

LATEST_PUBLISHED_RUNS = """
    SELECT selected.platform, selected.account_id, selected.target_date, selected.id AS latest_run_id
    FROM finance_sync_runs selected INNER JOIN (
      SELECT MAX(id) AS latest_run_id FROM finance_sync_runs
      WHERE status IN ('success', 'no_data') AND """ + PUBLISHED_BATCH_PREDICATE + """
      GROUP BY platform, COALESCE(NULLIF(source_site_code,''),CONCAT('legacy-account:',account_id)), target_date
    ) chosen ON chosen.latest_run_id = selected.id
"""

VISIBLE_TRANSACTION_JOIN = """
    INNER JOIN (""" + LATEST_PUBLISHED_RUNS + """) latest
      ON latest.platform = t.platform
     AND latest.account_id = t.account_id
     AND latest.target_date = t.business_date
     AND latest.latest_run_id = t.run_id
"""


def source_alias_filter(source_ids, *, platform_column: str, account_column: str) -> str:
    """Exact historic credential aliases for a selected stable source set.

    Columns are internal SQL identifiers; values remain bound parameters.
    Binary comparison keeps identity exact across historical table collations.
    """
    return ("EXISTS(SELECT 1 FROM module_data_source_accounts selected_source "
        "WHERE selected_source.module='finance' "
        f"AND BINARY selected_source.provider=BINARY {platform_column} "
        f"AND BINARY selected_source.account_id=BINARY {account_column} "
        "AND selected_source.source_id IN (" + ",".join(["%s"] * len(source_ids)) + "))")
