"""Source identity for opt-in model evidence, never scans runtime or credentials."""
from hashlib import sha256
from pathlib import Path


def source_identity():
    root = Path(__file__).resolve().parents[1]
    files = (
        "agent/agent/harness_online.py", "agent/agent/harness_application.py",
        "agent/agent/shipment_conversation.py", "agent/agent/shipment_queries.py",
        "agent/agent/knowledge_answers.py", "agent/tools/feishu_knowledge.py",
        "shared/shipment_metrics.py", "tests/shipment_model_acceptance.py",
        "tests/knowledge_model_acceptance.py", "tests/test_shipment_metrics.py", "tests/test_feishu_knowledge.py",
    )
    return {name: sha256((root/name).read_bytes()).hexdigest() for name in files}
