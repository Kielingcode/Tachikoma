"""P1 Stage 5 — Beta gate-v3 纯 shadow(G-4,0 run)。

对 P0+P1 全部 store 的每条 memory,按 episodes.started_at 重放 evidence 前缀,
对比两个 gate 在每个前缀点的晋升判定:
  gate-v2(现行,counting):fam≥2 ∧ s≥2 ∧ s≥3f
  gate-v3(shadow,Beta): fam≥2 ∧ posterior_lb(s, f) ≥ θ_promote
其中 s = organic 正向(evidence-class 边界:adoption_outcome 不进晋升),
f = 全部负向(P9 不对称),posterior_lb = Beta(1+s, 1+f) 的 5% 分位
(Jeffreys 改 uniform 先验 α0=β0=1;分位数用 stdlib 连分数 betainc + 二分)。
θ_promote 在网格上扫,报告每 θ 的一致率与分歧案例;**不替换 gate-v2,
不阻塞 P1 EXIT**(切换是 P2 决定)。
"""

import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORES = [ROOT / "spikes" / "p0b" / "demo" / "arm_b.sqlite",
          ROOT / "spikes" / "p0b" / "demo" / "arm_c.sqlite",
          ROOT / "spikes" / "p1" / "verified.sqlite",
          ROOT / "spikes" / "p1" / "wasteful.sqlite"]
OUT = ROOT / "spikes" / "p1" / "beta_shadow.json"
THETAS = [0.30, 0.40, 0.50, 0.60, 0.70]
QUANTILE = 0.05


# ---------------- stdlib Beta 分位数(连分数 I_x(a,b) + 二分)----------------

def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def betainc_reg(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def beta_quantile(a: float, b: float, q: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if betainc_reg(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def posterior_lb(s: int, f: int) -> float:
    return beta_quantile(1.0 + s, 1.0 + f, QUANTILE)


# ---------------------------------- 重放 ----------------------------------

def gate_v2(s: int, f: int, fam: int) -> bool:
    return fam >= 2 and s >= 2 and s >= 3 * f

def gate_v3(s: int, f: int, fam: int, theta: float) -> bool:
    return fam >= 2 and posterior_lb(s, f) >= theta


def memory_prefixes(con, mid: str) -> list[dict]:
    """按 started_at 重放该 memory 的 evidence 前缀点(s/f/fam 累积)。"""
    rows = con.execute(
        "SELECT e.polarity, e.evidence_source, ep.family_id, ep.started_at"
        " FROM evidence_links e JOIN claims c ON e.claim_id=c.claim_id"
        " JOIN episodes ep ON c.episode_id=ep.episode_id"
        " WHERE e.memory_id=? ORDER BY ep.started_at", (mid,)).fetchall()
    out, s, f, fams = [], 0, 0, set()
    for r in rows:
        if r["polarity"] > 0 and r["evidence_source"] == "organic_task":
            s += 1
            if r["family_id"]:
                fams.add(r["family_id"])
        elif r["polarity"] < 0:
            f += 1
        out.append({"s": s, "f": f, "fam": len(fams), "at": r["started_at"]})
    return out


def main() -> None:
    report = {"quantile": QUANTILE, "prior": "uniform(1,1)",
              "inputs": "s=organic positives, f=all negatives (P9), fam gate kept",
              "stores": {}, "by_theta": {}}
    points = []
    for path in STORES:
        if not path.exists():
            report["stores"][path.name] = "MISSING (skipped)"
            continue
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        mids = [r["memory_id"] for r in
                con.execute("SELECT memory_id FROM memory_items")]
        n_pts = 0
        for mid in mids:
            for p in memory_prefixes(con, mid):
                points.append({**p, "store": path.name, "memory_id": mid})
                n_pts += 1
        report["stores"][path.name] = {"memories": len(mids), "prefix_points": n_pts}
        con.close()

    for theta in THETAS:
        agree, div = 0, []
        for p in points:
            v2 = gate_v2(p["s"], p["f"], p["fam"])
            v3 = gate_v3(p["s"], p["f"], p["fam"], theta)
            if v2 == v3:
                agree += 1
            else:
                div.append({**{k: p[k] for k in ("store", "memory_id", "s", "f", "fam")},
                            "v2": v2, "v3": v3,
                            "posterior_lb": round(posterior_lb(p["s"], p["f"]), 4)})
        report["by_theta"][str(theta)] = {
            "agreement": f"{agree}/{len(points)}",
            "agreement_rate": round(agree / len(points), 4) if points else None,
            "divergences": div[:20],
            "n_divergences": len(div)}

    best = max(report["by_theta"].items(),
               key=lambda kv: kv[1]["agreement_rate"] or 0)
    report["best_theta"] = {"theta": best[0], **{k: best[1][k] for k in
                                                 ("agreement", "n_divergences")}}
    report["decision"] = ("P2 才考虑切换;完全一致且无反例才切" if best[1]["n_divergences"]
                          else "完全一致——P2 可讨论切换")
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("stores", "best_theta", "decision")},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
