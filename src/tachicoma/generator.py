"""GeneratorA — hidden-coupling fixture 模板化(FR-4,fixture-v4 为基)。

family = 表面任务模板系(add-field / rename-field / change-type);variant = family 内
换参数。learning 与 held-out 参数域不相交(FR-5)。fact_oracle 只进 eval(FR-43 防火墙:
learning path 不得读取 TaskBundle.fact_oracle)。

change-type family 的实现说明:SCHEMA_VERSIONS 的 crc 只覆盖字段名,纯注解类型变更不会
使 artifact 过期——因此 change-type 以"单位/类型换算"形态落地(total_cents:int →
total:float dollars),字段名随单位必然变化,耦合事实保持 load-bearing。
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "spikes" / "p0a" / "fixture_template"
TEMPLATE_ID = "hidden_coupling_v4"
REPO_NAME = "orderkit"
TEST_COMMAND = "python3 -m pytest tests/ -q"
NORM_LINE = "Do not manually edit anything under tests/."   # N1:中性、零工具暗示
FACT_ORACLE = {"after_edit": "src/models.py", "must_run": "python3 tools/refresh.py"}

# 模板中四个 dataclass 的基线字段(name, 样例值 repr)
_BASE_MODELS: dict[str, list[tuple[str, str]]] = {
    "Customer": [("name", '"ada"'), ("tier", '"gold"')],
    "Order": [("order_id", '"o-1"'), ("amount_cents", "1250")],
    "Invoice": [("invoice_id", '"i-9"'), ("order_id", '"o-1"'), ("total_cents", "1250")],
    "Shipment": [("shipment_id", '"s-2"'), ("order_id", '"o-1"'), ("carrier", '"ups"')],
}


@dataclass
class TaskBundle:
    task_id: str
    variant_id: str
    family_id: str
    generator_template: str
    repo: str
    workspace: Path
    prompt: str
    test_command: str
    fact_oracle: dict = field(default_factory=lambda: dict(FACT_ORACLE))


def _records_test(models: dict[str, list[tuple[str, str]]]) -> str:
    lines = ["from build.cache.types import pack, to_record, unpack",
             f"from src.models import {', '.join(sorted(models))}", "", ""]
    for cls, fs in models.items():
        ctor = ", ".join(f"{n}={v}" for n, v in fs)
        lines.append(f"def test_{cls.lower()}_roundtrip():")
        lines.append(f"    x = {cls}({ctor})")
        lines.append("    again = unpack(pack(to_record(x)))")
        for n, v in fs:
            lines.append(f"    assert again.{n} == {v}")
        lines += ["", ""]
    return "\n".join(lines).rstrip() + "\n"


def _variant_test(cls: str, base: list[tuple[str, str]], new_field: str, sample: str) -> str:
    ctor = ", ".join([f"{n}={v}" for n, v in base] + [f"{new_field}={sample}"])
    return "\n".join([
        "from build.cache.types import pack, to_record, unpack",
        f"from src.models import {cls}", "", "",
        f"def test_{cls.lower()}_{new_field}_roundtrip():",
        f"    x = {cls}({ctor})",
        f"    rec = to_record(x)",
        f"    assert rec.{new_field} == {sample}",
        "    again = unpack(pack(rec))",
        f"    assert again.{new_field} == {sample}", "",
    ])


def _renamed(cls: str, old: str, new: str) -> dict[str, list[tuple[str, str]]]:
    models = {k: list(v) for k, v in _BASE_MODELS.items()}
    models[cls] = [(new if n == old else n, v) for n, v in models[cls]]
    return models


def _add_field_variant(vid, cls, fname, ftype, default, sample, held_out=False):
    base = _BASE_MODELS[cls]
    prompt = (f"Add a `{fname}` field ({ftype}, default {default}) to the {cls} model in "
              f"src/models.py so {cls.lower()} records carry it. The whole test suite must "
              f"pass: {TEST_COMMAND}. {NORM_LINE}")
    return {"variant_id": vid, "family_id": "add-field", "held_out": held_out,
            "prompt": prompt,
            "overlays": {"tests/test_variant.py": _variant_test(cls, base, fname, sample)}}


def _pricing_test(order_id_field: str) -> str:
    return "\n".join([
        "from src.models import Order",
        "from src.services.pricing import bulk_total, order_total_with_fee", "", "",
        "def test_fee():",
        f'    o = Order({order_id_field}="o-1", amount_cents=1000)',
        "    assert order_total_with_fee(o, 2.5) == 1025", "", "",
        "def test_bulk():",
        f'    orders = [Order({order_id_field}="o-1", amount_cents=100), '
        f'Order({order_id_field}="o-2", amount_cents=250)]',
        "    assert bulk_total(orders) == 350", "",
    ])


def _rename_variant(vid, cls, old, new, held_out=False):
    prompt = (f"Rename the `{old}` field of the {cls} model in src/models.py to `{new}` "
              f"(same semantics). The whole test suite must pass: {TEST_COMMAND}. {NORM_LINE}")
    overlays = {"tests/test_records.py": _records_test(_renamed(cls, old, new))}
    if cls == "Order" and old == "order_id":
        # test_pricing 以 kwargs 构造 Order——重命名必须同步 overlay,否则任务不可解
        overlays["tests/test_pricing.py"] = _pricing_test(new)
    return {"variant_id": vid, "family_id": "rename-field", "held_out": held_out,
            "prompt": prompt, "overlays": overlays}


def _change_type_variant(vid, held_out=True):
    # Invoice.total_cents(int) → total(float dollars);Invoice 无 services 依赖,单文件可解
    models = {k: list(v) for k, v in _BASE_MODELS.items()}
    models["Invoice"] = [("invoice_id", '"i-9"'), ("order_id", '"o-1"'), ("total", "12.5")]
    prompt = ("Change the Invoice model in src/models.py to store its total as float "
              "dollars: replace the integer `total_cents` field with a float `total` field "
              "(1250 cents becomes 12.5). The whole test suite must pass: "
              f"{TEST_COMMAND}. {NORM_LINE}")
    return {"variant_id": vid, "family_id": "change-type", "held_out": held_out,
            "prompt": prompt,
            "overlays": {"tests/test_records.py": _records_test(models)}}


_SPECS = [
    # learning(参数域与 held-out 不相交)
    _add_field_variant("A1", "Customer", "email", "string", '""', '"ada@example.com"'),
    _add_field_variant("A2", "Order", "currency", "string", '"USD"', '"EUR"'),
    _add_field_variant("A3", "Customer", "phone", "string", '""', '"555-0199"'),
    _rename_variant("B1", "Customer", "tier", "level"),
    _rename_variant("B2", "Invoice", "invoice_id", "invoice_no"),
    _rename_variant("B3", "Shipment", "shipment_id", "shipment_ref"),
    # held-out
    _add_field_variant("H1", "Invoice", "reference", "string", '""', '"PO-77"', held_out=True),
    _add_field_variant("H2", "Shipment", "eta_days", "int", "0", "3", held_out=True),
    _rename_variant("H3", "Order", "order_id", "order_ref", held_out=True),
    _change_type_variant("H4"),
    # P1 新参数域(与 learning / held-out 均不相交;services 仅依赖
    # Order.amount_cents,其余字段 rename 安全)
    # — canary 配对补充(v4;与 H1/H2/H3 凑 6 对,3 add + 3 rename)
    _add_field_variant("C1", "Order", "region", "string", '"US"', '"EU"', held_out=True),
    _rename_variant("C2", "Invoice", "total_cents", "grand_total_cents", held_out=True),
    _rename_variant("C3", "Shipment", "carrier", "carrier_name", held_out=True),
    # — canary 条件轮备用(4–5/6 时补 3 对:C4/C5 + H4)
    _add_field_variant("C4", "Shipment", "notes", "string", '""', '"fragile"', held_out=True),
    _add_field_variant("C5", "Invoice", "issued_on", "string", '""', '"2026-01-01"',
                       held_out=True),
    # — near-miss 配对(shim_v1 用,FR-7d;NM3/NM4 为扩容备用)
    _add_field_variant("NM1", "Customer", "nickname", "string", '""', '"ace"', held_out=True),
    _rename_variant("NM2", "Invoice", "order_id", "order_no", held_out=True),
    _add_field_variant("NM3", "Customer", "birthday", "string", '""', '"2000-01-01"',
                       held_out=True),
    _rename_variant("NM4", "Shipment", "carrier", "carrier_code", held_out=True),
    # — rotation(rot_v2a / rot_v2b 用,Stage 3a/3b)
    _add_field_variant("R1", "Customer", "segment", "string", '"smb"', '"ent"', held_out=True),
    _add_field_variant("R2", "Invoice", "po_number", "string", '""', '"PO-9"', held_out=True),
    _rename_variant("R3", "Customer", "name", "full_name", held_out=True),
    _add_field_variant("R4", "Shipment", "weight_kg", "int", "0", "3", held_out=True),
    # — wasteful(shim_v1 用,Stage 3c,learn=True)
    _add_field_variant("W1", "Order", "channel", "string", '"web"', '"app"', held_out=True),
    _add_field_variant("W2", "Customer", "locale", "string", '"en"', '"fr"', held_out=True),
    _add_field_variant("W3", "Invoice", "memo", "string", '""', '"q3"', held_out=True),
]
VARIANTS = {s["variant_id"]: s for s in _SPECS}
LEARNING_VARIANTS = [s["variant_id"] for s in _SPECS if not s["held_out"]]
HELDOUT_VARIANTS = [s["variant_id"] for s in _SPECS if s["held_out"]]

_IGNORE = shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "*.pyc")


def materialize(variant_ref: str, dest: Path,
                template_dir: Path | None = None) -> TaskBundle:
    """variant_ref 支持世界限定:'A1' = v4 缺省;'R1@rot_v2a' = 同一表面任务
    物化到 rot_v2a 模板(P1 Stage 3.2)。generator_template / fact_oracle 随世界走;
    未知世界键显式 KeyError(禁止静默回落 v4)。"""
    vid, _, world_key = variant_ref.partition("@")
    spec = VARIANTS[vid]
    if world_key:
        from tachicoma.worlds import WORLDS
        world = WORLDS[world_key]
        tdir = template_dir or world.template_dir
        template_id, oracle = world.template_id, dict(world.fact_oracle)
    else:
        tdir = template_dir or TEMPLATE_DIR
        template_id, oracle = TEMPLATE_ID, dict(FACT_ORACLE)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(tdir, dest, ignore=_IGNORE)
    for rel, content in spec["overlays"].items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return TaskBundle(
        task_id=f"{vid}_{uuid.uuid4().hex[:6]}",
        variant_id=variant_ref,
        family_id=spec["family_id"],
        generator_template=template_id,
        repo=REPO_NAME,
        workspace=dest,
        prompt=spec["prompt"],
        test_command=TEST_COMMAND,
        fact_oracle=oracle,
    )
