"""Convert the original shorthand schemas; keep validation provider independent."""
from copy import deepcopy
from jsonschema import Draft202012Validator


def json_schema(schema):
    if isinstance(schema, str):
        result = {"type": schema}
    elif isinstance(schema, list):
        if len(schema) != 1:
            raise ValueError("Array shorthand must contain exactly one item schema")
        result = {"type": "array", "items": json_schema(schema[0])}
    elif isinstance(schema, dict):
        if isinstance(schema.get("type"), (str, list)) or any(
            key in schema for key in ("anyOf", "oneOf", "$ref", "$defs")
        ):
            result = deepcopy(schema)
        else:
            result = {
                "type": "object",
                "properties": {key: json_schema(value) for key, value in schema.items()},
                "required": list(schema),
                "additionalProperties": False,
            }
    else:
        raise ValueError("Schema must be a dictionary, type string, or one-item list")
    Draft202012Validator.check_schema(result)
    return result


def validate(value, schema):
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ValueError("Invalid value at {} ({} validation)".format(path, error.validator))
