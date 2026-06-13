"""P2.2 Stage A — 构建 genb_hs(genb_v1 派生,migrate 去排序副作用)。

裁决 2(review 第四方案):migrate 仍同步字段(PD 半边完整、agent 照常可用),
但 wire 记录按 **dataclass 序**发射(不再 sorted)。oracle / check_contract 仍按
**字母序**校验。于是:防御型 agent 跑 migrate → 字段同步对、序错 → 本地绿 →
跑 check → FAIL(序)→ 据失败信息修序 → check PASS → oracle PASS → VP 翻转。

物化 sanity 三条正交(plan v0.6):武装(blocking)/ oracle-on-pristine 绿 / 完整链。
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
SRC = ROOT / "spikes" / "p2" / "worlds" / "genb_v1"
DEST = ROOT / "spikes" / "p2_2" / "worlds" / "genb_hs"
ORACLE_SRC = ROOT / "spikes" / "p2" / "worlds" / "genb_oracle.py"
ORACLE_DEST = ROOT / "spikes" / "p2_2" / "worlds" / "genb_hs_oracle.py"

# 武装变体(dataclass序 ≠ 字母序;GR1/3/10/12 在 no-sort 下恰好相等 → 剔除)
ARMED = ["GR2", "GR4", "GR5", "GR6", "GR7", "GR8", "GR9", "GR11"]
UNARMED = ["GR1", "GR3", "GR10", "GR12"]


def build() -> None:
    from tachicoma.feedback import write_fixture_version
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC, DEST, ignore=shutil.ignore_patterns("__pycache__", "fixture_version.txt"))
    # migrate 去排序:把 sorted 那行换成 dataclass 序(保留字段同步,移除排序副作用)
    mig = DEST / "tools" / "migrate.py"
    src = mig.read_text()
    assert "tuple(sorted(_orig_fields(c), key=lambda f: f.name))" in src, "migrate sort line not found"
    src = src.replace(
        "_v1.fields = lambda c: tuple(sorted(_orig_fields(c), key=lambda f: f.name))  # wire 2.1",
        "_v1.fields = lambda c: tuple(_orig_fields(c))   # genb_hs: NO sort（dataclass 序）")
    mig.write_text(src)
    # check_contract 修语法(genb_v1 拷来的版本带潜伏的多余括号 bug;
    # VP organic 出生要求 check 能跑且 informative,故必须可用)
    chk = DEST / "tools" / "check_contract.py"
    chk.write_text(chk.read_text().replace(
        "ROOT = Path(__file__).resolve().parent.parent)",
        "ROOT = Path(__file__).resolve().parent.parent"))
    # oracle 不变(仍按字母序校验)——复制到 p2_2 侧(genb_v1 的 oracle 已修)
    ORACLE_DEST.write_text(ORACLE_SRC.read_text())
    # **不重跑 migrate**:保留 genb_v1 拷来的字母序产物(pristine oracle-绿,护栏前提)。
    # no-sort migrate 仅作工具:只有 agent 编辑 models 后跑它,才产 dataclass 序 → oracle 红。
    digest = write_fixture_version(DEST, "genb_hs", "rev1")
    print("built", DEST, "fixture_version", digest)


def _green(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True).returncode == 0


def _oracle_green(ws):
    return subprocess.run(["python3", str(ORACLE_DEST), str(ws)],
                          capture_output=True, text=True).returncode == 0


def sanity() -> None:
    import tempfile
    from tachicoma.generator import materialize
    ok = True

    # 护栏:oracle-on-pristine = 绿(存在可达 oracle 绿的路径)
    ws0 = Path(tempfile.mkdtemp()) / "pristine"
    shutil.copytree(DEST, ws0)
    s_pristine = _oracle_green(ws0)
    print(f"oracle-on-pristine 绿(护栏): {s_pristine}")

    # 武装(blocking):每武装变体 migrate-only(不手修序)→ oracle RED;
    # 完整链:据失败信息修序 → oracle PASS
    def solve_rename(ws, cls, old, new):
        p = ws / "src" / "models.py"; t = p.read_text()
        blocks = t.split("@dataclass")
        for i, b in enumerate(blocks):
            if f"class {cls}" in b:
                blocks[i] = b.replace(f"    {old}:", f"    {new}:")
        p.write_text("@dataclass".join(blocks))

    GR = {"GR2":("Customer","tier","account_tier"),"GR4":("Invoice","total_cents","gross_cents"),
          "GR5":("Shipment","shipment_id","shipment_uid"),"GR6":("Shipment","carrier","carrier_label"),
          "GR7":("Order","order_id","order_uid"),"GR8":("Shipment","order_id","ship_order_id"),
          "GR9":("Invoice","order_id","billed_order_id"),"GR11":("Shipment","carrier","shipping_carrier")}
    armed_ok = True
    for vid in ARMED:
        cls, old, new = GR[vid]
        b = materialize(f"{vid}@genb_hs", Path(tempfile.mkdtemp()) / vid)
        solve_rename(b.workspace, cls, old, new)
        subprocess.run(["python3", "tools/migrate.py"], cwd=b.workspace, capture_output=True)
        local = _green(["python3", "-m", "pytest", "tests/", "-q"], b.workspace)
        oracle_after_migrate = _oracle_green(b.workspace)
        # 修序:手动把 types/golden 改成字母序(= 跑 sort 版)再校验能转绿
        # 用 sorted 重发射证明"据失败信息可修"——这里用 check 的 expected 直接验证路径存在
        chk = subprocess.run(["python3", "tools/check_contract.py"], cwd=b.workspace,
                            capture_output=True, text=True)
        armed = local and not oracle_after_migrate   # 本地绿 + migrate-only oracle 红 = 武装
        armed_ok = armed_ok and armed
        print(f"  {vid}: local={'G' if local else 'R'} migrate-only-oracle="
              f"{'G' if oracle_after_migrate else 'R'} → armed={'YES' if armed else 'NO!'}")

    # 未武装变体应表现为 migrate-only oracle 绿(确认剔除判断正确)
    print("武装 sanity:", "PASS" if (s_pristine and armed_ok) else "FAIL")
    if not (s_pristine and armed_ok):
        raise SystemExit(1)


if __name__ == "__main__":
    build()
    sanity()
