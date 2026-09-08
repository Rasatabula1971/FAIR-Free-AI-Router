import re

from fair.quality.json_data import strict_json


def resolve_pointer(value, pointer):
    if pointer == "":
        return value
    if not pointer.startswith("/") or len(pointer) > 512:
        raise ValueError("Invalid JSON pointer")
    tokens = pointer[1:].split("/")
    if len(tokens) > 20:
        raise ValueError("JSON pointer exceeds depth budget")
    for token in tokens:
        if re.search(r"~(?![01])", token):
            raise ValueError("Invalid JSON pointer escape")
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            if len(key) > 8 or int(key) >= len(value):
                raise ValueError("JSON pointer index not found")
            value = value[int(key)]
        else:
            raise ValueError("JSON pointer target not found")
    return value


def grounded_result(contract, evidence):
    """Resolve host-selected paths in supplied data, never retrieve or infer source truth."""
    sources = {source.source_id: source.text for source in evidence}
    parsed = {}
    answer, provenance = {}, {}
    for field in contract.fields:
        if field.source_id not in sources:
            raise ValueError("Grounding source not supplied")
        if field.source_id not in parsed:
            parsed[field.source_id] = strict_json(sources[field.source_id])
        answer[field.output_key] = resolve_pointer(parsed[field.source_id], field.pointer)
        provenance[field.output_key] = {"source_id": field.source_id, "pointer": field.pointer}
    return {"answer": answer, "sources": provenance}
