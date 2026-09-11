"""Small closed JSON schemas shared by production infrastructure Connectors."""

TEXT = {"type": "string", "maxLength": 16384}
DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
BOOL = {"type": "boolean"}
COUNT = {"type": "integer", "minimum": 0}
CELL = {"oneOf": [TEXT, {"type": "number"}, {"type": "null"}, BOOL]}
CURSOR = {"oneOf": [{"type": "string", "maxLength": 8192}, {"type": "null"}]}


def object_schema(properties, required=None):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties if required is None else required)}


def array_schema(items, *, maximum=20000):
    return {"type": "array", "items": items, "maxItems": maximum}


def record_schema(fields, *, optional=()):
    return object_schema({key: CELL for key in fields}, [key for key in fields if key not in optional])


def source_record_schema(*, depth=0):
    """Typed provider fields, with finite depth/width and no credential escape."""
    if type(depth) is not int or not 0 <= depth <= 3:
        raise ValueError("source record depth is invalid")
    value = CELL
    if depth:
        nested = source_record_schema(depth=depth-1)
        value = {"oneOf": [CELL, nested, array_schema({"oneOf": [CELL, nested]}, maximum=512)]}
    return {"type": "object", "properties": {}, "required": [], "additionalProperties": value,
            "maxProperties": 512, "propertyNames": {"type": "string", "minLength": 1, "maxLength": 128}}
