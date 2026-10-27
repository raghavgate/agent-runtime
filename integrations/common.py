"""Shared result envelope, independent of the model provider."""
from copy import deepcopy

SOURCE_SCHEMA = {"id": "string", "label": "string", "system": "string"}


def source(system, collection, record_id, label):
    return {"id": f"{system}:{collection}/{record_id}", "label": label, "system": system}


def envelope_schema(data_schema):
    from schema import json_schema
    return {"type": "object", "properties": {
        "data": json_schema(data_schema),
        "sources": {"type": "array", "items": json_schema(SOURCE_SCHEMA)},
    }, "required": ["data", "sources"], "additionalProperties": False}


def result(data, sources):
    return {"data": deepcopy(data), "sources": sources}
