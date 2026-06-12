"""P1 Stage 3.3 — 构建三个新世界模板(shim_v1 / rot_v2a / rot_v2b)。

shim_v1   活算 shim,无耦合(near-miss + wasteful 双用途);refresh.py 幂等空转。
rot_v2a   wire v2(salt 校验);旧 refresh.py 仍是 v1 发射器 → 照旧事实 = 真失败;
          migrate.py 尚不存在(G-2 机械隔离:stale harm 不被 rival 遮蔽)。
rot_v2b   = v2a + tools/migrate.py(v2 发射器,含 golden 再生)。

全部由 v4 模板程序化变换而来;沿用密封自查(零实体名约束等由源模板继承)。
"""

import shutil
import sys
import zlib
from dataclasses import fields, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V4 = ROOT / "spikes" / "p0a" / "fixture_template"
OUT = ROOT / "spikes" / "p1" / "worlds"
SALT = "|wire2"

sys.path.insert(0, str(V4))

# ---------------------------------------------------------------- shim ----

SHIM_TYPES = '''\
"""Record types and wire serialization for the order pipeline."""

import dataclasses
import zlib

import src.models as _models

_CLASSES = {
    c.__name__: c
    for c in vars(_models).values()
    if isinstance(c, type) and dataclasses.is_dataclass(c)
    and c.__module__ == _models.__name__
}

FIELD_ORDER = {n: tuple(f.name for f in dataclasses.fields(c)) for n, c in _CLASSES.items()}
SCHEMA_VERSIONS = {n: zlib.crc32(",".join(fs).encode()) for n, fs in FIELD_ORDER.items()}

_RECORD_TYPES = {}


def _record_type(kind):
    if kind not in _RECORD_TYPES:
        _RECORD_TYPES[kind] = type(f"{kind}Record", (), {})
    return _RECORD_TYPES[kind]


def to_record(obj):
    kind = type(obj).__name__
    if kind not in FIELD_ORDER:
        raise TypeError(f"no record type for {kind!r}")
    rec = _record_type(kind)()
    for f in FIELD_ORDER[kind]:
        setattr(rec, f, getattr(obj, f))
    return rec


def pack(rec):
    kind = type(rec).__name__[: -len("Record")]
    payload = {"kind": kind, "v": SCHEMA_VERSIONS[kind]}
    for f in FIELD_ORDER[kind]:
        payload[f] = getattr(rec, f)
    return payload


def unpack(payload):
    kind = payload["kind"]
    if payload.get("v") != SCHEMA_VERSIONS[kind]:
        raise ValueError(
            f"record schema mismatch for {kind!r}: "
            f"payload v={payload.get('v')}, expected {SCHEMA_VERSIONS[kind]}"
        )
    extra = set(payload) - {"kind", "v", *FIELD_ORDER[kind]}
    if extra:
        raise ValueError(f"unexpected keys in {kind!r} payload: {sorted(extra)}")
    rec = _record_type(kind)()
    for f in FIELD_ORDER[kind]:
        setattr(rec, f, payload[f])
    return rec
'''

SHIM_REFRESH = '''\
"""Rebuild the derived record layer from the dataclasses in src/models.py."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SOURCE = Path(__file__).resolve().parent / "_types_source.txt"


def main() -> None:
    out = ROOT / "build" / "cache" / "types.py"
    out.write_text(_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
'''


def make_shim() -> Path:
    dest = OUT / "shim_v1"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(V4, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    (dest / "build" / "cache" / "types.py").write_text(SHIM_TYPES, encoding="utf-8")
    (dest / "build" / "cache" / "_integrity.py").unlink(missing_ok=True)
    # 无 golden / wire_compat(无耦合世界:没有需要再生的快照层)
    shutil.rmtree(dest / "tests" / "golden", ignore_errors=True)
    (dest / "tests" / "test_wire_compat.py").unlink(missing_ok=True)
    # refresh = 幂等空转(把 shim 源原样重写;物化自检要求跑后仍绿)
    (dest / "tools" / "_types_source.txt").write_text(SHIM_TYPES, encoding="utf-8")
    (dest / "tools" / "refresh.py").write_text(SHIM_REFRESH, encoding="utf-8")
    return dest


# ------------------------------------------------------------- v2 emit ----

def _load_v4_emitter():
    import importlib.util
    spec = importlib.util.spec_from_file_location("v4_refresh", V4 / "tools" / "refresh.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V2_INTEGRITY = '''\
"""Consistency guard for artifacts produced by tools/migrate.py."""

import zlib

WIRE_SALT = "|wire2"


def verify_registry(field_order, schema_versions, salt=WIRE_SALT):
    for kind, field_names in field_order.items():
        expected = zlib.crc32((",".join(field_names) + salt).encode())
        if expected != schema_versions.get(kind):
            raise RuntimeError(
                f"record table corrupt for {kind!r} "
                f"(checksum {expected} != {schema_versions.get(kind)})"
            )
'''


def _emit_v2_types(v4mod) -> str:
    """复用 v4 发射器再做 salt 变换:SCHEMA_VERSIONS 全部替换为 salted 值。"""
    src = v4mod._emit(v4mod._model_classes())
    for cls in v4mod._model_classes():
        fs = ",".join(f.name for f in fields(cls))
        old_v = zlib.crc32(fs.encode())
        new_v = zlib.crc32((fs + SALT).encode())
        src = src.replace(f"'{cls.__name__}': {old_v},", f"'{cls.__name__}': {new_v},")
    return src


def _emit_v2_golden(v4mod) -> str:
    import json
    samples = json.loads(v4mod._emit_golden(v4mod._model_classes()))
    for p in samples:
        cls = next(c for c in v4mod._model_classes() if c.__name__ == p["kind"])
        fs = ",".join(f.name for f in fields(cls))
        p["v"] = zlib.crc32((fs + SALT).encode())
    return json.dumps(samples, indent=2) + "\n"


# wire v2 契约测试:动态校验 salt 机制本身(非具体值,字段变更后依然成立)。
# 这是堵住"老 refresh 连 golden 一起再生,把世界拖回 v1 自洽"漏洞的外部消费者:
# v1 工具覆写 _integrity.py(无 WIRE_SALT)→ ImportError;types 无盐 → 校验不等。
# 只有 migrate(或昂贵手改)能满足。现实对应:跨服务的 wire 格式契约钉死在测试里。
CONTRACT_TEST = '''\
import zlib

from build.cache._integrity import WIRE_SALT
from build.cache.types import FIELD_ORDER, SCHEMA_VERSIONS


def test_wire_format_v2_contract():
    assert WIRE_SALT == "|wire2"
    for kind, field_names in FIELD_ORDER.items():
        expected = zlib.crc32((",".join(field_names) + WIRE_SALT).encode())
        assert SCHEMA_VERSIONS[kind] == expected
'''

# migrate.py:复用 v1 发射器逻辑,统一加盐(monkeypatch crc32,免逐调用点手术),
# 并以 v2 _integrity 源替换。自包含,fixture 内可独立运行。
MIGRATE_SOURCE = '''\
"""Rebuild the derived record artifacts (wire format 2) from src/models.py."""

import importlib.util
import zlib
from pathlib import Path

_HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("_v1_emitter", _HERE / "refresh.py")
_v1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_v1)

_orig_crc32 = zlib.crc32
zlib.crc32 = lambda data: _orig_crc32(bytes(data) + b"|wire2")   # wire format 2

_v1._INTEGRITY_SOURCE = {V2_INTEGRITY!r}


def main() -> None:
    _v1.main()


if __name__ == "__main__":
    main()
'''


def make_rot(with_migrate: bool) -> Path:
    dest = OUT / ("rot_v2b" if with_migrate else "rot_v2a")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(V4, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    v4mod = _load_v4_emitter()
    (dest / "build" / "cache" / "_integrity.py").write_text(V2_INTEGRITY, encoding="utf-8")
    (dest / "build" / "cache" / "types.py").write_text(_emit_v2_types(v4mod), encoding="utf-8")
    (dest / "tests" / "golden" / "wire_samples.json").write_text(
        _emit_v2_golden(v4mod), encoding="utf-8")
    (dest / "tests" / "test_wire_contract.py").write_text(CONTRACT_TEST, encoding="utf-8")
    # 旧 refresh.py 原样保留(v1 发射器 = 过期工具)
    if with_migrate:
        (dest / "tools" / "migrate.py").write_text(
            MIGRATE_SOURCE.format(V2_INTEGRITY=V2_INTEGRITY), encoding="utf-8")
    return dest


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for d in (make_shim(), make_rot(False), make_rot(True)):
        print("built", d)
