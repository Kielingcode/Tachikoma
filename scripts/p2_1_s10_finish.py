import importlib.util, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("sc", ROOT / "scripts" / "p2_1_stage_c.py")
sc = importlib.util.module_from_spec(spec); spec.loader.exec_module(sc)
from tachicoma.resolver import check_segments
from tachicoma.runner import run_episode
from tachicoma.store import MemoryStore
store = MemoryStore(sc.STORE)
payload = json.loads(sc.RESULTS.read_text())
done = {h["variant_id"] for h in payload["heldout"]}
gate = json.loads((sc.OUT/"b1_calibration.json").read_text())["gate"]
baseline_fs, baseline_n = (int(x) for x in gate["rename_false_success"].split("/"))
organic_vp = payload["asserts"].get("vp_organic_promoted", False)
for ref in sc.HELDOUT:
    if ref.split("@")[0] in {v.split("@")[0] for v in done}: continue
    print(f"[s10-finish] {ref}", flush=True)
    r = run_episode(store, ref, arm="s10_heldout", model=sc.MODEL, memory_on=True,
                    workspace_root=sc.WS/"s10c", learn=False, k=1, feedback_level=2,
                    memory_types=("ValidationParity",))
    eid=r["episode_id"]; oracle=sc._oracle(store,eid)
    fs=bool(r["eventual"]) and oracle is False
    ep,_,_=store.episode_view(eid)
    vp_ad=any(a.kind in ("run","test_run") and a.command and sc.CHECK_CMD in check_segments(a.command) for a in ep.actions)
    payload["heldout"].append({"variant_id":r["variant_id"],"injected":r["injected"],
        "eventual_local":r["eventual"],"oracle":oracle,"false_success":fs,"vp_adopted":vp_ad})
    print(f"    inj={r['injected']} oracle={oracle} fs={fs} vp_adopted={vp_ad}", flush=True)
    sc.RESULTS.write_text(json.dumps(payload,indent=2))
n=len(payload["heldout"]); fsc=sum(h["false_success"] for h in payload["heldout"])
adc=sum(h["vp_adopted"] for h in payload["heldout"]); rate=adc/n
payload["asserts"]["s10"]={"report_line":"S10-organic" if organic_vp else "S10-seeded",
  "heldout_false_success":f"{fsc}/{n}","baseline":f"{baseline_fs}/{baseline_n}","vp_adoption_rate":rate,
  "gate_baseline_armed":baseline_fs>=3,"gate_fs_le_1":fsc<=1,"gate_drop_ge_2":(baseline_fs-fsc)>=2,
  "gate_adoption_ge_0.8":rate>=0.8,
  "PASS":baseline_fs>=3 and fsc<=1 and (baseline_fs-fsc)>=2 and rate>=0.8,
  "caveat":None if organic_vp else "seeded VP,非 governed 出生;限定检索/采纳管线可工作,不外推 organic"}
payload["asserts"]["s15"]="可测" if organic_vp else "NOT MEASURABLE(无 organic VP)"
sc.RESULTS.write_text(json.dumps(payload,indent=2))
print(json.dumps(payload["asserts"]["s10"],indent=1,ensure_ascii=False))
