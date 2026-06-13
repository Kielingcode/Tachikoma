"""World registry — 模板级路径参数(P1 Stage 3.2 管线参数化)。

中立配置模块:generator(写侧)与 store.episode_view(读侧重建)共用,
避免 store 依赖 generator。按 episodes.generator_template 查询,缺省回落 v4。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_SPIKES = Path(__file__).resolve().parents[2] / "spikes"


@dataclass(frozen=True)
class WorldParams:
    template_id: str
    template_dir: Path
    trigger_path: str = "src/models.py"
    tool_path: str = "tools/refresh.py"          # 该世界的 intended procedure 工具
    derived_paths: tuple = ("build/cache",)
    golden_paths: tuple = ("tests/golden",)
    fact_oracle: dict = field(default_factory=lambda: {
        "after_edit": "src/models.py", "must_run": "python3 tools/refresh.py"})
    oracle_script: Path | None = None        # FR-9b:harness 侧 hidden oracle(GenB)


WORLDS: dict[str, WorldParams] = {
    "hidden_coupling_v4": WorldParams(
        template_id="hidden_coupling_v4",
        template_dir=_SPIKES / "p0a" / "fixture_template"),
    # 活算 shim:无耦合(near-miss / wasteful 双用途)。refresh.py 存在但幂等空转。
    "shim_v1": WorldParams(
        template_id="shim_v1",
        template_dir=_SPIKES / "p1" / "worlds" / "shim_v1",
        fact_oracle={"after_edit": "src/models.py", "must_run": ""}),  # 无 load-bearing fact
    # rotation harmful(3a):wire v2,旧 refresh 产 v1 = 照旧事实必失败;migrate 尚不存在
    "rot_v2a": WorldParams(
        template_id="rot_v2a",
        template_dir=_SPIKES / "p1" / "worlds" / "rot_v2a",
        tool_path="tools/refresh.py",     # 旧工具仍是被注入记忆的 action;新工具不存在
        fact_oracle={"after_edit": "src/models.py", "must_run": "python3 tools/migrate.py",
                     "note": "v2a 中 oracle 工具尚未发布——按构造无法达成 intended procedure"}),
    # rotation recovery(3b):= v2a + tools/migrate.py(v2 发射器)
    "rot_v2b": WorldParams(
        template_id="rot_v2b",
        template_dir=_SPIKES / "p1" / "worlds" / "rot_v2b",
        tool_path="tools/migrate.py",
        fact_oracle={"after_edit": "src/models.py", "must_run": "python3 tools/migrate.py"}),
    # Generator B(P2):rot_v2b 演化版;wire 2.1 字母序隐藏规则;
    # 本地套件可绕(shape/roundtrip),hidden oracle 在 harness 侧
    "genb_v1": WorldParams(
        template_id="genb_v1",
        template_dir=_SPIKES / "p2" / "worlds" / "genb_v1",
        tool_path="tools/migrate.py",
        fact_oracle={"after_edit": "src/models.py", "must_run": "python3 tools/migrate.py",
                     "vp_must_run": "python3 tools/check_contract.py"},
        oracle_script=_SPIKES / "p2" / "worlds" / "genb_oracle.py"),
    # P2.2:genb_v1 派生,migrate 去排序副作用(逼"先错"→ VP organic 出生上车点)
    "genb_hs": WorldParams(
        template_id="genb_hs",
        template_dir=_SPIKES / "p2_2" / "worlds" / "genb_hs",
        tool_path="tools/migrate.py",
        fact_oracle={"after_edit": "src/models.py", "must_run": "python3 tools/migrate.py",
                     "vp_must_run": "python3 tools/check_contract.py"},
        oracle_script=_SPIKES / "p2_2" / "worlds" / "genb_hs_oracle.py"),
}

DEFAULT_WORLD = WORLDS["hidden_coupling_v4"]


def world_for(template_id: str | None) -> WorldParams:
    return WORLDS.get(template_id or "", DEFAULT_WORLD)
