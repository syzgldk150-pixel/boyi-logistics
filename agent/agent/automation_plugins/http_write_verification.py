"""Check plugin-declared field comparisons against fresh Host HTTP observations."""

from collections.abc import Mapping


def _at(value, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 1024:
        raise ValueError("invalid JSON pointer")
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def http_verification_error(observations, meta):
    writes = {
        item["evidence_ref"]: (index, item)
        for index, item in enumerate(observations)
        if item["operation"] == "http.request" and item["write_started"]
    }
    proofs = meta.get("http_write_verifications", [])
    if not writes:
        return None if proofs == [] else "HTTP verification claims a write that did not occur"
    if not isinstance(proofs, list) or len(proofs) != len(writes):
        return "HTTP writes require a fresh readback proof for each submission"
    by_ref = {item["evidence_ref"]: (index, item) for index, item in enumerate(observations)}
    covered = set()
    try:
        for proof in proofs:
            if not isinstance(proof, Mapping) or set(proof) != {"write_ref", "read_ref", "matches"}:
                raise ValueError()
            ref = proof["write_ref"]
            if ref in covered:
                raise ValueError()
            write_index, write = writes[ref]
            read_index, read = by_ref[proof["read_ref"]]
            if (
                read_index <= write_index
                or write["action"] != "write_json"
                or read["operation"] != "http.request"
                or read["action"] != "read_json"
                or read["write_started"]
                or read["role"] != write["role"]
            ):
                raise ValueError()
            matches = proof["matches"]
            if not isinstance(matches, list) or not 1 <= len(matches) <= 64:
                raise ValueError()
            for match in matches:
                if not isinstance(match, Mapping) or set(match) != {"write_path", "read_path", "expected"}:
                    raise ValueError()
                expected = match["expected"]
                if type(expected) not in {str, int, float, bool}:
                    raise ValueError()
                for result, key in ((write["result"], "write_path"), (read["result"], "read_path")):
                    actual = _at(result, match[key])
                    if type(actual) is not type(expected) or actual != expected:
                        raise ValueError()
            covered.add(ref)
    except (KeyError, IndexError, TypeError, ValueError):
        return "HTTP write verification does not match fresh Host response fields"
    return None
