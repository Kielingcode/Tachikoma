# P0A 执行日志

> 2026-06-11 · 依据 doc/implementation_plan_p0a_zh.md · fixture tags: fixture-v1 / v2 / v3

## Step 0 — code-kitty pass ✅

- repo:`~/Projects/code-kitty`(copy 自 deepseek_test_01),分支 `code-kitty-pass`,commit `c969367`
- 五项全部落地:自进化总开关(config + builder 双闸)、temperature、token usage(弹性解包,旧 mock 零改动)、max_tokens、provenance(task_start 带 agent_version/model_version)
- 测试:**167 通过**(154 旧零回归 + 13 新)

## Step 1 — fixture ✅(经 3 轮密封迭代)

陷阱机制(全三版相同):models.py(4 个 dataclass)→ build/cache/types.py(派生:
__slots__ + FIELD_ORDER + crc32 SCHEMA_VERSIONS 三重簿记)→ tools/refresh.py(唯一 trail)。
天真改法 → AttributeError(指向 types 内部);手改注册表 → "record table corrupt";refresh → 全绿。
变体:A1/A2(add-field family)、B1(rename family)。

| 版本 | 内容 | 触发的泄漏 |
|---|---|---|
| v1 | 8 文件 | 全量通读:5/5 开局读完整个 repo(含 refresh.py),无信息不对称可言 |
| v2 | +35 filler = 47 文件 | 选择性阅读出现,但 grep 实体名必中 types.py;其机器生成长相 → "找生成器"一跳直达 |
| v3 | types.py 伪装手写(docstring、FIELD_ORDER/SCHEMA_VERSIONS、防御式 _verify_registry) | 残余:手动同步旁路(自算 crc32) |

## Step 2 — 密封探针(最强 agent = Fable 5 subagent)

| run | fixture | variant | first-try | eventual | 动作数 | 泄漏分析 |
|---|---|---|---|---|---|---|
| seal_01 | v1 | A1 | **PASS=漏** | ✓ | 11 | 开局通读 8 文件含 refresh.py → edit+refresh 一步 |
| seal_02 | v1 | A1 | fail | ✓ | 12 | 同样通读,但选择增量验证 |
| seal_03 | v1 | A2 | fail | ✓ | 11 | 同上 |
| seal_04 | v1 | B1 | **PASS=漏** | ✓ | 11 | 通读 + 预判校验和 → edit+refresh 先于首测 |
| seal_05 | v1 | B1 | fail | ✓ | 12 | 通读,增量验证 |
| seal_06 | v2 | A1 | **PASS=漏** | ✓ | 10 | grep "Customer" 命中 types.py → 识别生成 → 读 refresh |
| seal_07 | v2 | A1 | fail | ✓ | 12 | 读了 refresh 但仍增量验证 |
| seal_08 | v2 | A2 | fail | ✓ | 10 | 同上 |
| seal_09 | v2 | B1 | fail | ✓ | 15 | rename 必然 grep "tier" → types.py;仍增量 |
| seal_10 | v2 | B1 | **PASS=漏** | ✓ | 13 | grep → types → refresh → 一步到位 |
| seal_11 | v3 | A1 | fail | ✓ | 7 | **教科书轨迹**:编辑→失败→事后调查→refresh |
| seal_12 | v3 | A1 | fail | ✓ | 13 | 探索过 refresh,仍增量验证 |
| seal_13 | v3 | A2 | fail | ✓ | 15 | **手动同步旁路**:自算 crc32,4 处手改,从未发现 refresh |
| seal_14 | v3 | B1 | **PASS=漏** | ✓ | 14 | 旁路 + 预算校验和,首测即过;从未发现 refresh |
| seal_15 | v3 | B1 | fail | ✓ | 13 | 增量 → 失败 → refresh |

**汇总(vs 最强 agent)**:first_try_hat:v1 2/5 → v2 2/5 → v3 **1/5(0.2)**;eventual 15/15(θ_discover≈1.0)。

**关键质性发现**:
1. v3 的残余"泄漏"(seal_13/14)**不是事实被推断出来**——这两个 agent 从未发现 refresh.py,
   它们用替代解法(手动同步 + 自算校验和)完成任务,代价 14-15 动作 vs 知情者约 5-7。
   **事实本身仍密封;被打破的是 binary first-try 指标,不是信息不对称。**
2. cost-to-solve 不对称性对旁路也成立 → headline 指标用步数差(requirements 既定方向)是对的。
3. 对 FR-5 的反馈:θ_seal 的 0.05 margin 对"过程性依赖"类事实 vs frontier agent 大概率不可达
   (手动旁路永远可计算);建议 P0b 校准时把密封判据改为
   `binary first-try ≤ 0.2(vs frontier)∧ cost 不对称显著` 的复合判据,
   且密封的主对象是学习 agent(Sonnet 级)。

## Step 2b/3 — code-kitty(Sonnet 4.6)runs ✅

| run | variant | memory | LLM 步数 | 工具调用 | tokens(in/out) | refresh? | 结果 |
|---|---|---|---|---|---|---|---|
| ck_a1 | A1 | off | 17 | 21 | 66,007/2,250 | **✗ 旁路** | ✓ 24 passed |
| ck_b1 | B1 | off | 14 | 22 | 57,702/2,041 | **✗ 旁路** | ✓ 23 passed |
| ck_b1_r2 | B1 | off | 13 | 19 | 54,779/1,970 | **✗ 旁路** | ✓ |
| ck_a2_nomem | A2 | off | 16 | 19 | 58,011/2,162 | **✗ 旁路** | ✓ |
| **ck_a2_mem** | A2 | **on (mem_0001)** | **10** | **15** | **41,615/1,339** | **✓ 采纳** | ✓ 24 passed |

**Recall 配对对比(同 A2 任务)**:LLM 步数 16→10(**-37%**),工具调用 -21%,
input tokens **-28%**,且 agent 明确按记忆行动(总结自述 "per the remembered pattern")。
Adoption 机械判定成立:编辑 models.py 后 run 了 refresh.py。

**Sonnet 发现率:0/4** —— 四次 memory-off run 全部走手动同步旁路(自算 crc32 + 多处手改),
从未发现 refresh.py。学习供给(含 refresh 的轨迹)在 v3 fixture 上只来自 Fable(3/5)。

## Step 3 — 手动闭环 ✅(记账见 memory.yaml)

```
提取:seal_11(add-field)+ seal_15(rename)→ 2 条正向 claim,同一 canonical key
晋升:clm_0001 后 families=1 → 正确地不可晋升(S5 人肉验证)
      clm_0002 后 S=2, families=2, F=0 → active_correlational(FR-23 计数规则)✓
注入:FR-34 payload → ck_a2_mem → 采纳 ✓ → 成功 ✓ → 成本大降 ✓
```

caveat:发现轨迹来自 Fable 探针而非 code-kitty(Sonnet 0/4 发现)——闭环真实但
学习供给一侧有星号,P0b 必须解决。

---

# 决策备忘:EXIT(有条件通过)

## Exit criterion 对照

| 判据 | 结果 |
|---|---|
| first-try ~5/5 失败(最强 agent) | v3:4/5 失败。1 例"泄漏"实为手动同步旁路(事实未被推断,见下) |
| recovery 发生 | 15/15(Fable)+ 4/4(Sonnet),eventual 全成 |
| recall 在 variant 上明显生效 | ✓ 配对 -37% 步数 / -28% tokens,且采纳了更优 procedure |

**判定:不触发 kill。** sealed-but-discoverable fixture 可构造,闭环转得起来,
记忆价值可测量。但三个实证发现必须反馈给 spec/P0b:

## 三个实证发现(P0a 的真正产出)

1. **手动同步旁路是结构性的,不是 fixture 缺陷。** "让两个文件一致"永远可以手工完成——
   ProceduralDependency 类事实的违反是可人工恢复的,所以 binary first-try 对 frontier
   agent 的下限不是 0:θ_seal=0.05 的 margin(FR-5)对这类事实不可达,实测 v3 ≈ 0.2。
   **但 cost 不对称对旁路依然成立**(旁路 14-16 步 vs 知情 5-10 步)→
   FR-5 密封判据应改为复合:`binary first-try ≤ ~0.2(vs frontier)∧ 配对成本差显著`。
   requirements 把 headline 定为 cost-to-solve 的决策被实证支持。
2. **密封与旁路存在跷跷板。** v2(机器长相)→ agent 找生成器(预编辑泄漏);
   v3(手写伪装)→ agent 手动同步(学习供给枯竭)。两种失败模式此消彼长。
   P0b 的 fixture-v4 方向:**golden files**(refresh.py 同时再生 build/cache/golden.json,
   基础测试校验打包样本)→ 手动同步从 4 处涨到 ~8 处跨 3 文件,经济性倒向找工具,
   同时保留 v3 的伪装。
3. **学习供给依赖 agent 行为风格。** Sonnet(0/4)从不探索工具目录,Fable(3/5)会。
   若学习 agent 始终旁路,系统会结晶出"手动同步 procedure"——也有 recall 价值,
   但劣于工具 procedure。P0b 校准协议(N=20)必须分别测 first_try 和 discovery-of-tool 率。

## P0b 入口清单(继承自本轮)

- fixture-v4:golden files 反旁路 + 重测 Sonnet 发现率
- FR-5 判据修订(复合密封判据)→ 同步 requirements
- 把本轮人肉动作代码化:generator 模板化(fixture-v3 为基)、提取器(§6.3 已人肉验证)、
  SQLite 八表(memory.yaml 字段 1:1 对应)、配对 demo driver(本轮 run 表即原型)
- 噪声地板:同任务重跑翻转率(本轮 B1 两次 memory-off 都旁路成功——初步迹象:结果层面
  方差低、路径层面方差存在)

