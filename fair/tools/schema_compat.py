"""Report whether a caller's JSON Schema survives each provider's structured-output API.

FAIR sends the caller's ``expected_schema`` to a provider to steer generation and
validates the answer against the original locally, so a keyword a provider cannot
parse is dropped in transit rather than failing the request (see
``fair.providers.schema_dialects``). Two kinds of problem remain that no transform
can fix without changing what a schema means, and both are rejected by the provider
with an opaque HTTP 400:

* an object with no ``additionalProperties: false``, or a ``required`` list that
  omits a declared property -- OpenAI strict mode refuses both, and supplying them
  here would silently make optional fields mandatory;
* a combinator (``oneOf``, ``allOf``, ``not``) no dialect accepts.

Run this before wiring a new schema into a pipeline::

    python -m fair.tools.schema_compat request.json
    python -m fair.tools.schema_compat request.json --key expected_schema
    python -m fair.tools.schema_compat request.json --emit openai_strict
"""

import argparse
import json
import sys

from fair.providers.schema_dialects import DIALECTS, constraint_notes, transport_schema

_COMBINATORS = ("oneOf", "allOf", "not")
# Keys a document may hide a schema under when it is a request envelope, not a schema.
_SCHEMA_KEYS = ("expected_schema", "schema", "json_schema", "response_schema")


def find_schema(document, key=None):
    """The schema inside ``document``, which may be a request envelope wrapping one."""
    if key is not None:
        if not isinstance(document, dict) or key not in document:
            raise KeyError(key)
        return document[key]
    if isinstance(document, dict) and ("properties" in document or "type" in document):
        return document
    for candidate in _SCHEMA_KEYS:
        if isinstance(document, dict) and isinstance(document.get(candidate), dict):
            return document[candidate]
    raise KeyError("no schema found; pass --key")


def _walk(node, path, visit):
    if isinstance(node, list):
        for item in node:
            _walk(item, path, visit)
        return
    if not isinstance(node, dict):
        return
    visit(node, path)
    for name, item in (node.get("properties") or {}).items():
        _walk(item, f"{path}.{name}", visit)
    if "items" in node:
        _walk(node["items"], f"{path}[]", visit)
    for key in ("anyOf", *_COMBINATORS, "$defs"):
        value = node.get(key)
        if isinstance(value, dict):
            for name, item in value.items():
                _walk(item, f"{path}.{name}", visit)
        elif isinstance(value, list):
            _walk(value, path, visit)


def blocking_findings(schema, dialect):
    """Problems the schema's author must resolve; a transform cannot."""
    spec = DIALECTS[dialect]
    found = []

    def visit(node, path):
        for key in _COMBINATORS:
            if key in node:
                found.append((path, f"{key} is not accepted by any provider dialect"))
        if node.get("type") != "object" and "properties" not in node:
            return
        if spec.keeps_additional_properties and spec.keeps is not None:
            if node.get("additionalProperties") is not False:
                found.append((path, "object needs additionalProperties: false"))
            declared, required = set(node.get("properties") or {}), set(node.get("required") or ())
            missing = sorted(declared - required)
            if missing:
                names = ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else "")
                found.append((path, f"every property must be in required; missing {names}"))

    _walk(schema, "result", visit)
    return found


def dropped_keywords(schema, dialect):
    """Keyword -> paths where transport will drop it. Each stays enforced locally."""
    spec = DIALECTS[dialect]
    dropped: dict[str, list[str]] = {}
    if spec.keeps is None:
        return dropped

    def visit(node, path):
        for key in node:
            if key == "const":
                dropped.setdefault("const (rewritten as enum)", []).append(path)
            elif key == "additionalProperties" and not spec.keeps_additional_properties:
                dropped.setdefault(key, []).append(path)
            elif key not in spec.keeps and key != "additionalProperties":
                dropped.setdefault(key, []).append(path)

    _walk(schema, "result", visit)
    return dropped


def provider_dialects():
    """provider_id -> dialect, read from the adapters rather than restated here."""
    from fair.embedded.module import _CLOUD_PROVIDERS

    mapping: dict[str, list[str]] = {}
    for provider_id, entry in _CLOUD_PROVIDERS.items():
        mapping.setdefault(entry["adapter"].schema_dialect, []).append(provider_id)
    return mapping


def report(schema, stream=sys.stdout):
    """Print a per-dialect verdict. Returns True when nothing needs an author fix."""
    providers = provider_dialects()
    clean = True
    for dialect in (name for name in DIALECTS if DIALECTS[name].keeps is not None):
        users = ", ".join(sorted(providers.get(dialect, []))) or "no configured provider"
        print(f"\n=== {dialect} ({users}) ===", file=stream)
        blocking = blocking_findings(schema, dialect)
        if blocking:
            clean = False
            print(f"  MUST FIX ({len(blocking)}):", file=stream)
            for path, problem in blocking[:20]:
                print(f"    {path}: {problem}", file=stream)
            if len(blocking) > 20:
                print(f"    ... and {len(blocking) - 20} more", file=stream)
        dropped = dropped_keywords(schema, dialect)
        if dropped:
            total = sum(len(paths) for paths in dropped.values())
            print(
                f"  dropped for transport, still enforced on the response ({total}):", file=stream
            )
            for keyword, paths in sorted(dropped.items(), key=lambda item: -len(item[1])):
                print(f"    {keyword:<28} x{len(paths):<4} first: {paths[0]}", file=stream)
        note = constraint_notes(schema, dialect)
        if note is not None:
            print("  restated in the prompt:", file=stream)
            for line in note.splitlines():
                print(f"    {line}", file=stream)
        print(
            f"  VERDICT: {'ACCEPTED after transport' if not blocking else 'REJECTED'}", file=stream
        )
    return clean


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="JSON file holding the schema, or - for stdin")
    parser.add_argument("--key", help="key the schema sits under, when it is not the whole file")
    parser.add_argument("--emit", choices=sorted(DIALECTS), help="print the transported schema")
    args = parser.parse_args(argv)
    text = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read()
    try:
        schema = find_schema(json.loads(text), args.key)
    except (KeyError, ValueError) as error:
        print(f"Could not read a schema: {error}", file=sys.stderr)
        return 2
    if args.emit:
        print(json.dumps(transport_schema(schema, args.emit), indent=2))
        return 0
    return 0 if report(schema) else 1


if __name__ == "__main__":
    raise SystemExit(main())
