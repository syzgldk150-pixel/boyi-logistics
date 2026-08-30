# Knowledge Notes Directory

This directory is for repository-maintained reference notes only. It is not an active runtime knowledge base and is not scanned, embedded, or indexed automatically.

Runtime knowledge retrieval uses the MySQL `knowledge` table through `agent/memory.py`; writes and searches use the authenticated `/internal/v1/knowledge` interfaces. Adding a Markdown or text file here does not make it available to the Agent.

- Keep reference files in UTF-8 Markdown or text format and link them from the relevant documentation index.
- Never place secrets, credentials, raw business exports, or runtime data here.
- If a note must become runtime knowledge, import it through the governed knowledge interface and verify the MySQL record explicitly; do not infer ingestion from filesystem presence.
