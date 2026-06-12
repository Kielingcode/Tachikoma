"""Rebuild build/cache/types.py from the dataclasses in src/models.py."""

import sys
import zlib
from dataclasses import fields, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.models as models  # noqa: E402


def _model_classes():
    out = []
    for name in dir(models):
        obj = getattr(models, name)
        if isinstance(obj, type) and is_dataclass(obj) and obj.__module__ == models.__name__:
            out.append(obj)
    return sorted(out, key=lambda c: c.__name__)


def _emit(cls_list) -> str:
    # Output is styled to read like ordinary hand-maintained code.
    lines = [
        '"""Record types and wire serialization for the order pipeline."""',
        "",
        "import zlib",
        "",
        "",
    ]

    for cls in cls_list:
        fs = [f.name for f in fields(cls)]
        rec = f"{cls.__name__}Record"
        lines.append(f"class {rec}:")
        lines.append(f'    """Flat wire record for {cls.__name__}."""')
        lines.append("")
        lines.append(f"    __slots__ = ({', '.join(repr(f) for f in fs)})")
        lines.append("")
        lines.append(f"    def __init__(self, {', '.join(fs)}):")
        for f in fs:
            lines.append(f"        self.{f} = {f}")
        lines.append("")
        lines.append("")

    lines.append("FIELD_ORDER = {")
    for c in cls_list:
        lines.append(
            f"    {c.__name__!r}: ({', '.join(repr(f.name) for f in fields(c))}),"
        )
    lines.append("}")
    lines.append("")
    lines.append("SCHEMA_VERSIONS = {")
    for c in cls_list:
        v = zlib.crc32(",".join(f.name for f in fields(c)).encode())
        lines.append(f"    {c.__name__!r}: {v},")
    lines.append("}")
    lines.append("")
    lines.append("_RECORD_TYPES = {")
    for c in cls_list:
        lines.append(f"    {c.__name__!r}: {c.__name__}Record,")
    lines.append("}")

    lines += [
        "",
        "",
        "def _verify_registry():",
        "    # guard against partial edits to the tables above",
        "    for kind, field_names in FIELD_ORDER.items():",
        "        expected = zlib.crc32(','.join(field_names).encode())",
        "        if expected != SCHEMA_VERSIONS[kind]:",
        "            raise RuntimeError(",
        "                f'record table corrupt for {kind!r} '",
        "                f'(checksum {expected} != {SCHEMA_VERSIONS[kind]})'",
        "            )",
        "",
        "",
        "_verify_registry()",
        "",
        "",
        "def to_record(obj):",
        "    kind = type(obj).__name__",
        "    if kind not in FIELD_ORDER:",
        "        raise TypeError(f'no record type for {kind!r}')",
        "    return _RECORD_TYPES[kind](*[getattr(obj, f) for f in FIELD_ORDER[kind]])",
        "",
        "",
        "def pack(rec):",
        "    kind = type(rec).__name__[: -len('Record')]",
        "    payload = {'kind': kind, 'v': SCHEMA_VERSIONS[kind]}",
        "    for f in FIELD_ORDER[kind]:",
        "        payload[f] = getattr(rec, f)",
        "    return payload",
        "",
        "",
        "def unpack(payload):",
        "    kind = payload['kind']",
        "    if payload.get('v') != SCHEMA_VERSIONS[kind]:",
        "        raise ValueError(",
        "            f'record schema mismatch for {kind!r}: '",
        "            f'payload v={payload.get(\"v\")}, expected {SCHEMA_VERSIONS[kind]}'",
        "        )",
        "    extra = set(payload) - {'kind', 'v', *FIELD_ORDER[kind]}",
        "    if extra:",
        "        raise ValueError(f'unexpected keys in {kind!r} payload: {sorted(extra)}')",
        "    return _RECORD_TYPES[kind](*[payload[f] for f in FIELD_ORDER[kind]])",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    out_dir = ROOT / "build" / "cache"
    out_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "build" / "__init__.py").touch()
    (out_dir / "__init__.py").touch()
    (out_dir / "types.py").write_text(_emit(_model_classes()), encoding="utf-8")
    print(f"wrote {out_dir / 'types.py'}")


if __name__ == "__main__":
    main()
