-- Immutable domain events plus at-least-once per-consumer outbox delivery state.

CREATE TABLE IF NOT EXISTS domain_events (
    event_id CHAR(36) NOT NULL,
    event_type VARCHAR(96) NOT NULL,
    schema_version SMALLINT UNSIGNED NOT NULL,
    source_system VARCHAR(32) NOT NULL,
    source_event_id VARCHAR(191) NULL,
    entity_type VARCHAR(64) NOT NULL,
    entity_id VARCHAR(191) NOT NULL,
    work_item_id CHAR(36) NULL,
    run_id CHAR(36) NULL,
    step_id CHAR(36) NULL,
    occurred_at DATETIME(6) NOT NULL,
    observed_at DATETIME(6) NOT NULL,
    correlation_id CHAR(36) NOT NULL,
    causation_id CHAR(36) NULL,
    payload_json JSON NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    headers_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    UNIQUE KEY uq_domain_event_source (
        source_system, event_type, source_event_id
    ),
    KEY idx_domain_events_entity (entity_type, entity_id, occurred_at),
    KEY idx_domain_events_correlation (correlation_id, occurred_at),
    KEY idx_domain_events_type_time (event_type, occurred_at),
    KEY idx_domain_events_work_item (work_item_id, occurred_at),
    KEY idx_domain_events_run_step (run_id, step_id),
    CONSTRAINT fk_domain_events_work_item FOREIGN KEY (work_item_id)
        REFERENCES work_items (work_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_domain_events_run FOREIGN KEY (run_id)
        REFERENCES agent_runs (run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_domain_events_step FOREIGN KEY (step_id)
        REFERENCES agent_run_steps (step_id) ON DELETE RESTRICT,
    CONSTRAINT chk_domain_events_schema_version CHECK (schema_version > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS outbox_events (
    outbox_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_id CHAR(36) NOT NULL,
    consumer_name VARCHAR(64) NOT NULL,
    topic VARCHAR(96) NOT NULL,
    partition_key VARCHAR(191) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    attempt_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    max_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 10,
    locked_by VARCHAR(128) NULL,
    locked_until DATETIME(6) NULL,
    last_error_code VARCHAR(64) NULL,
    last_error_summary VARCHAR(500) NULL,
    published_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (outbox_id),
    UNIQUE KEY uq_outbox_event_consumer (event_id, consumer_name),
    KEY idx_outbox_claim (status, available_at, outbox_id),
    KEY idx_outbox_lease (status, locked_until, outbox_id),
    KEY idx_outbox_consumer_status (consumer_name, status, available_at),
    CONSTRAINT fk_outbox_event FOREIGN KEY (event_id)
        REFERENCES domain_events (event_id) ON DELETE RESTRICT,
    CONSTRAINT chk_outbox_status CHECK (
        status IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'DEAD_LETTER')
    ),
    CONSTRAINT chk_outbox_attempts CHECK (attempt_count <= max_attempts),
    CONSTRAINT chk_outbox_max_attempts CHECK (max_attempts > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS event_consumptions (
    consumer_name VARCHAR(64) NOT NULL,
    event_id CHAR(36) NOT NULL,
    processed_at DATETIME(6) NOT NULL,
    result_sha256 CHAR(64) NULL,
    result_summary_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (consumer_name, event_id),
    KEY idx_event_consumptions_event (event_id),
    KEY idx_event_consumptions_processed (processed_at),
    CONSTRAINT fk_event_consumptions_event FOREIGN KEY (event_id)
        REFERENCES domain_events (event_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
