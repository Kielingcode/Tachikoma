"""P1 Stage 5 — frontier smoke N=5(诊断画像 only)。

预先声明(plan):报告只画像、不回灌 accept/reject;full N=20 显式推迟到
P1.5/appendix。arm 前缀 frontier 在 store.relearn 学习排除(双层:learn=False)。
画像维度:发现率(intended_procedure_discovered)+ 成本 + path_class 分布。
降级链:claude-fable-5 → claude-opus-4-8。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tachicoma.runner import run_episode
from tachicoma.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "spikes" / "p1"
STORE = OUT_DIR / "verified.sqlite"     # 只审计入库;arm=frontier* 学习排除
RESULTS = OUT_DIR / "frontier_smoke.json"
WS = Path("/tmp/p1_runs/frontier")

MODELS = ["claude-fable-5", "claude-opus-4-8"]    # 降级链
VARIANTS = ["H1", "H2", "H3", "C1", "C3"]          # v4 held-out 画像集,memory-off


def main() -> None:
    store = MemoryStore(STORE)
    results, model_used = [], None
    for i, vid in enumerate(VARIANTS, 1):
        last_err = None
        for model in ([model_used] if model_used else MODELS):
            try:
                print(f"[frontier {i}/{len(VARIANTS)}] {vid} ({model})", flush=True)
                r = run_episode(store, vid, arm="frontier_smoke", model=model,
                                memory_on=False, workspace_root=WS, learn=False)
                model_used = model
                results.append({k: r[k] for k in
                                ("episode_id", "variant_id", "model", "first_try",
                                 "eventual", "cost_steps", "cost_tokens", "path_class")})
                print(f"    eventual={r['eventual']} steps={r['cost_steps']}"
                      f" discovered={r['path_class'].get('intended_procedure_discovered')}",
                      flush=True)
                last_err = None
                break
            except Exception as exc:           # 降级链:fable 不可用 → opus
                last_err = f"{model}: {exc}"
                print(f"    {last_err}", flush=True)
        if last_err:
            results.append({"variant_id": vid, "error": last_err})
        RESULTS.write_text(json.dumps({"runs": results}, indent=2))

    ok = [r for r in results if "error" not in r]
    profile = {
        "model": model_used,
        "n": len(ok),
        "discovery_rate": (sum(1 for r in ok
                               if r["path_class"].get("intended_procedure_discovered")
                               or r["path_class"].get("intended_procedure_used"))
                           / len(ok)) if ok else None,
        "eventual_rate": sum(1 for r in ok if r["eventual"]) / len(ok) if ok else None,
        "median_steps": sorted(r["cost_steps"] for r in ok)[len(ok) // 2] if ok else None,
        "declaration": "画像 only;不回灌 accept/reject;full N=20 → P1.5/appendix",
    }
    RESULTS.write_text(json.dumps({"runs": results, "profile": profile}, indent=2))
    print(json.dumps(profile, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
