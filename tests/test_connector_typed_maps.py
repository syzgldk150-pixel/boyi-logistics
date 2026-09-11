"""Frozen Connector schemas must still validate provider field values."""
from types import MappingProxyType
import pytest
from agent.automation_plugins.connector_registry import ConnectorSensitiveDataDenied, validate_connector_public_text
from agent.automation_plugins.connector_schemas import source_record_schema
from agent.tool_registry import validate_schema_instance


def frozen(value):
    if isinstance(value, dict):
        return MappingProxyType({key:frozen(item) for key,item in value.items()})
    if isinstance(value, list):
        return tuple(frozen(item) for item in value)
    return value


@pytest.mark.parametrize("values", [{"field":"x"*16385}, {"field":{"too":"deep"}}, {"field":[1,2]}])
def test_frozen_typed_source_fields_reject_wrong_shape(values):
    with pytest.raises(ValueError):
        validate_schema_instance("source", values, frozen(source_record_schema()))


def test_typed_source_field_bounds_and_valid_nested_data():
    validate_schema_instance("source", {"field":{"amount":"12.50"}}, frozen(source_record_schema(depth=1)))
    for values in ({str(index):0 for index in range(513)}, {"k"*129:0}):
        with pytest.raises(ValueError):
            validate_schema_instance("source", values, frozen(source_record_schema(depth=1)))


@pytest.mark.parametrize("value", ["account:abcdef123456/path", "account:https://private.invalid", "problem:v2:"+"a"*64+"/extra"])
def test_pseudonymous_keys_do_not_permit_uri_suffixes(value):
    with pytest.raises(ConnectorSensitiveDataDenied):
        validate_connector_public_text(value, subject="source")
