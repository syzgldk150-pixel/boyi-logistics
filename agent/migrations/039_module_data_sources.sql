-- Long-lived source identities do not have lifecycle foreign keys to plugins.
CREATE TABLE IF NOT EXISTS module_data_sources (
 source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
 identity_fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 module VARCHAR(32) NOT NULL,
 provider VARCHAR(32) NOT NULL,
 organization_key VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
 dataset VARCHAR(64) NOT NULL,
 contract_version VARCHAR(32) NOT NULL,
 dedup_contract VARCHAR(191) NOT NULL,
 display_name VARCHAR(191) NOT NULL,
 producer_instance_id VARCHAR(191) NULL,
 producer_generation BIGINT NOT NULL,
 revision BIGINT NOT NULL,
 last_request_id VARCHAR(191) NOT NULL,
 status VARCHAR(32) NOT NULL,
 latest_publication_id CHAR(36) NULL,
 latest_published_at DATETIME(6) NULL,
 created_at DATETIME(6) NOT NULL,
 updated_at DATETIME(6) NOT NULL,
 UNIQUE KEY uq_module_source_identity (identity_fingerprint),
 KEY idx_module_source_list (module, status),
 KEY idx_module_source_producer (producer_instance_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS finance_source_run_bindings (
 run_id BIGINT NOT NULL PRIMARY KEY,
 source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 producer_instance_id VARCHAR(191) NOT NULL,
 producer_generation BIGINT NOT NULL,
 source_revision BIGINT NOT NULL,
 KEY idx_finance_run_source(source_id, run_id),
 CONSTRAINT fk_finance_run_source FOREIGN KEY(source_id) REFERENCES module_data_sources(source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS module_data_source_accounts (
 module VARCHAR(32) NOT NULL,
 provider VARCHAR(32) NOT NULL,
 account_id VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
 source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 PRIMARY KEY(module, provider, account_id),
 KEY idx_source_account_source (source_id),
 CONSTRAINT fk_source_account_source FOREIGN KEY(source_id) REFERENCES module_data_sources(source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS customer_problem_publications (
 publication_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
 source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 run_id VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
 producer_instance_id VARCHAR(191) NOT NULL,
 producer_generation BIGINT NOT NULL,
 source_revision BIGINT NOT NULL,
 record_count BIGINT NOT NULL,
 content_sha256 CHAR(64) NOT NULL,
 published_at DATETIME(6) NOT NULL,
 UNIQUE KEY uq_customer_publication_run(source_id, run_id),
 CONSTRAINT fk_customer_publication_source FOREIGN KEY(source_id) REFERENCES module_data_sources(source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS customer_problem_records (
 source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 external_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL,
 source_direction VARCHAR(32) NOT NULL,
 publication_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 waybill_no VARCHAR(100) NOT NULL,
 source_status VARCHAR(100) NOT NULL,
 resolved TINYINT(1) NOT NULL,
 source_updated_at VARCHAR(64) NOT NULL,
 source_json JSON NOT NULL,
 first_published_at DATETIME(6) NOT NULL,
 last_published_at DATETIME(6) NOT NULL,
 PRIMARY KEY(source_id, external_id, source_direction),
 KEY idx_customer_source_updated(source_id, source_updated_at),
 KEY idx_customer_waybill(waybill_no),
 CONSTRAINT fk_customer_record_source FOREIGN KEY(source_id) REFERENCES module_data_sources(source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS customer_problem_manual_fields (
 source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 external_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL,
 source_direction VARCHAR(32) NOT NULL,
 note TEXT NOT NULL,
 assigned_to VARCHAR(191) NULL,
 revision BIGINT NOT NULL,
 updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY(source_id, external_id, source_direction),
 CONSTRAINT fk_customer_manual_source FOREIGN KEY(source_id) REFERENCES module_data_sources(source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
