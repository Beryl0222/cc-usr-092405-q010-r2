"""领域契约测试：覆盖证据登记 → 计算 → 发布 → 更新 → 复现的完整链路。

场景（对应需求叙述）：

1. 同一来源重复导入合并；原始数据更正只产生新快照，旧快照保留；
2. 冲突来源保留竞争关系、待裁定前不可计算，裁定后可用；
3. 缺失年份按明示方法估算（estimated），绝不冒充观测值；无支撑为 no_input；
4. 头条数字（约 170 万死亡）可拆分到性别/儿童年龄段/疾病，且每个数字
   都能找到输入与公式版本；
5. 最小样本披露：小样本单元抑制且不计入汇总；
6. v1 发布后迟到调查补入 + 一个风险参数撤回：只有依赖分支重算；
7. 旧报告不修改、不删除，可按当时冻结证据原样复现；
8. 论文撤回只追加影响通告，标出受影响结论。
"""

import unittest

from domain.engine import (
    BURDEN_PIPELINE,
    ENGINE_FORMULAS,
    Engine,
    INDICATOR_SH,
)
from domain.publisher import Publisher
from domain.registry import Registry
from domain.terms import (
    CELL_CONFLICT,
    CELL_ESTIMATED,
    CELL_NO_INPUT,
    CELL_OBSERVED,
    CELL_SUPPRESSED,
    CELL_VOID,
    KIND_GEO_MAPPING,
    KIND_INDICATOR,
    KIND_LITERATURE,
    KIND_MORTALITY,
    KIND_RISK_PARAM,
    KIND_SURVEY,
    STATUS_PENDING_ADJUDICATION,
    STATUS_SUPERSEDED,
    STATUS_WITHDRAWN,
)

# --------------------------------------------------------------------- #
# 测试夹具构造
# --------------------------------------------------------------------- #
DISEASES = ["IHD", "LC", "COPD", "STROKE"]
AGES = ["0-4", "5-14", "15-59", "60+"]
YEARS = [2020, 2021, 2022]
MIN_SAMPLE = 50


def exp_slot(country, year, sex, age):
    return (INDICATOR_SH, country, year, sex, age)


def rr_slot(disease, sex, age):
    return ("RR", INDICATOR_SH, disease, sex, age)


def death_slot(country, year, disease, sex, age):
    return ("DEATHS", country, year, disease, sex, age)


def seed_indicator_and_geo(reg):
    reg.import_record(
        KIND_INDICATOR, "WHO-SHS-DEF",
        {"indicator_id": INDICATOR_SH,
         "definition": "非吸烟者家庭或工作场所二手烟暴露（每周≥1天）",
         "unit": "prevalence",
         "population": "非吸烟者"},
        provenance={"citation": "指标口径手册 2019"},
    )
    for iso3, name in (("CHN", "中国"), ("USA", "United States"), ("IND", "India")):
        reg.import_record(
            KIND_GEO_MAPPING, f"GEO-{iso3}",
            {"from": name, "to": iso3, "scheme": "ISO3166-1-alpha3"},
        )


def seed_rr(reg):
    # IHD/中风/COPD/肺癌的 RR（ALL,ALL 回退值），各带 95% UI
    rr_values = {
        "IHD": (1.27, (1.10, 1.44)),
        "STROKE": (1.23, (1.07, 1.40)),
        "LC": (1.20, (1.03, 1.45)),
        "COPD": (1.40, (1.18, 1.65)),
    }
    for disease, (value, ui) in rr_values.items():
        reg.import_record(
            KIND_RISK_PARAM, f"RR-{disease}-GBD2019",
            {"slot": rr_slot(disease, "ALL", "ALL"), "value": value,
             "uncertainty": list(ui), "outcome": disease,
             "reference": "GBD 2019 二手烟 RR 综合分析"},
            provenance={"citation": "Lancet 2020;396"},
        )


def seed_mortality(reg):
    # 死亡基数：每个国家/年/疾病/性别/年龄，量级使全球归因死亡约 170 万
    # 三个汇总区域的死亡基数（量级代表全球主要人口区域），使单年全球
    # 归因死亡落在约 170 万的现实口径
    base_scale = {
        "CHN": {"IHD": 2_500_000, "STROKE": 2_700_000, "LC": 1_100_000,
                "COPD": 1_500_000},
        "IND": {"IHD": 2_000_000, "STROKE": 1_350_000, "LC": 420_000,
                "COPD": 1_180_000},
        "USA": {"IHD": 1_180_000, "STROKE": 460_000, "LC": 380_000,
                "COPD": 340_000},
    }
    sex_split = {"MALE": 0.55, "FEMALE": 0.45}
    age_split = {"0-4": 0.002, "5-14": 0.002, "15-59": 0.346, "60+": 0.65}
    for country, diseases in base_scale.items():
        for year in YEARS:
            for d, total in diseases.items():
                for sex, sf in sex_split.items():
                    for age, af in age_split.items():
                        n = total * sf * af
                        reg.import_record(
                            KIND_MORTALITY,
                            f"WHO-MORT-{country}-{year}-{d}-{sex}-{age}",
                            {"slot": death_slot(country, year, d, sex, age),
                             "value": round(n, 1),
                             "uncertainty": [round(n * 0.94, 1),
                                             round(n * 1.06, 1)]},
                            provenance={"source": "WHO 死亡数据库"},
                        )


def seed_surveys(reg):
    """暴露调查：2020/2022 有观测，2021 缺失（供插值）；USA 一个小样本。"""
    profile = {
        "CHN": {("MALE", "15-59"): 0.55, ("MALE", "60+"): 0.50,
                ("FEMALE", "15-59"): 0.52, ("FEMALE", "60+"): 0.48,
                ("MALE", "0-4"): 0.40, ("MALE", "5-14"): 0.38,
                ("FEMALE", "0-4"): 0.41, ("FEMALE", "5-14"): 0.39},
        "IND": {("MALE", "15-59"): 0.45, ("MALE", "60+"): 0.42,
                ("FEMALE", "15-59"): 0.60, ("FEMALE", "60+"): 0.55,
                ("MALE", "0-4"): 0.35, ("MALE", "5-14"): 0.33,
                ("FEMALE", "0-4"): 0.36, ("FEMALE", "5-14"): 0.34},
        "USA": {("MALE", "15-59"): 0.22, ("MALE", "60+"): 0.20,
                ("FEMALE", "15-59"): 0.21, ("FEMALE", "60+"): 0.19,
                ("MALE", "0-4"): 0.15, ("MALE", "5-14"): 0.14,
                ("FEMALE", "0-4"): 0.16, ("FEMALE", "5-14"): 0.15},
    }
    seq = 0
    for country, cells in profile.items():
        for (sex, age), p in cells.items():
            # IND 女性只有 2020 一轮调查：2022 须靠临近结转估算，
            # 使头条年同时包含观测与估算两种构成
            survey_years = (2020,) if (country == "IND" and sex == "FEMALE") else (2020, 2022)
            for year in survey_years:
                seq += 1
                n = 30 if (country == "USA" and age in ("0-4", "5-14")) else 400
                # 小幅年度变化，让插值与端点不同
                pp = p + (0.0 if year == 2020 else 0.02)
                reg.import_record(
                    KIND_SURVEY,
                    f"SURV-{country}-{sex}-{age}-{year}",
                    {"slot": exp_slot(country, year, sex, age),
                     "value": round(pp, 4),
                     "uncertainty": [round(pp - 0.03, 4), round(pp + 0.03, 4)],
                     "sampling": {"design": "分层多阶段整群抽样",
                                  "sample_n": n, "response_rate": 0.91}},
                    provenance={"sample_n": n, "survey": f"{country} 成人+儿童烟草调查"},
                )
    return seq


def build_v1():
    reg = Registry()
    seed_indicator_and_geo(reg)
    seed_rr(reg)
    seed_mortality(reg)
    seed_surveys(reg)
    engine = Engine()
    run = engine.run(reg, YEARS, run_id="run-v1", label="第一版全球估算",
                     estimation_method="linear_interpolation", max_gap=3)
    pub = Publisher()
    report = pub.publish(
        run, report_id="GBD-SHS-2020-v1", title="全球二手烟疾病负担（第一版）",
        min_sample_n=MIN_SAMPLE, by="报告发布者", published_at="2023-06-01",
    )
    return reg, engine, pub, run, report


# --------------------------------------------------------------------- #
class RegistryContractTest(unittest.TestCase):
    def setUp(self):
        self.reg = Registry()
        seed_indicator_and_geo(self.reg)

    def test_duplicate_import_is_merged(self):
        content = {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 1.27,
                   "uncertainty": [1.1, 1.44]}
        snap1, changed1 = self.reg.import_record(
            KIND_RISK_PARAM, "RR-X", content, provenance={"citation": "A"})
        snap2, changed2 = self.reg.import_record(
            KIND_RISK_PARAM, "RR-X", content, provenance={"citation": "A 再次导入"})
        self.assertTrue(changed1)
        self.assertFalse(changed2, "同一来源重复导入必须合并，不产生新版本")
        self.assertEqual(snap1.snapshot_id, snap2.snapshot_id)
        self.assertEqual(len(self.reg.history(kind=KIND_RISK_PARAM,
                                              source_id="RR-X")), 1)
        actions = [e["action"] for e in self.reg.events()]
        self.assertIn("merge", actions)

    def test_correction_appends_snapshot_and_keeps_old(self):
        content_v1 = {"slot": exp_slot("CHN", 2020, "MALE", "15-59"),
                      "value": 0.55, "uncertainty": [0.52, 0.58]}
        content_v2 = dict(content_v1, value=0.57)
        s1, _ = self.reg.import_record(KIND_SURVEY, "S1", content_v1)
        s2, changed = self.reg.import_record(KIND_SURVEY, "S1", content_v2)
        self.assertTrue(changed)
        self.assertEqual(s2.version, 2)
        self.assertEqual(s2.supersedes, s1.snapshot_id)
        history = self.reg.history(kind=KIND_SURVEY, source_id="S1")
        self.assertEqual([s.version for s in history], [1, 2])
        self.assertEqual(history[0].status, STATUS_SUPERSEDED,
                         "旧快照必须保留（已替代），不得物理删除")
        # 旧快照仍可取回，数值是更正前的
        self.assertEqual(self.reg.get(s1.snapshot_id).content["value"], 0.55)

    def test_conflicting_sources_remain_competing_until_adjudication(self):
        slot = exp_slot("CHN", 2020, "MALE", "15-59")
        self.reg.import_record(
            KIND_SURVEY, "SA",
            {"slot": slot, "value": 0.55, "uncertainty": [0.5, 0.6]})
        self.reg.import_record(
            KIND_SURVEY, "SB",
            {"slot": slot, "value": 0.70, "uncertainty": [0.65, 0.75]})
        self.assertIsNone(self.reg.resolve_claim(slot),
                          "未裁定冲突不可解析为单一取值")
        self.assertEqual(self.reg.claim_status(slot), STATUS_PENDING_ADJUDICATION)
        conflicts = self.reg.pending_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual({c["snapshot_id"] for c in conflicts[0]["claims"]},
                         {"survey:SA@v1", "survey:SB@v1"})

        self.reg.adjudicate(slot, "survey:SB@v1",
                            reason="SB 抽样框架覆盖全国，SA 仅城市", by="审核人")
        winner = self.reg.resolve_claim(slot)
        self.assertEqual(winner.snapshot_id, "survey:SB@v1")
        self.assertEqual(winner.value, 0.70)
        # 落败声明仍保留可追溯
        self.assertEqual(len(self.reg.pending_conflicts()), 0)

    def test_withdraw_does_not_delete(self):
        self.reg.import_record(
            KIND_RISK_PARAM, "RR-BAD",
            {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 9.9})
        snap = self.reg.withdraw(kind=KIND_RISK_PARAM, source_id="RR-BAD",
                                 reason="参数来源论文撤回", by="方法学人员")
        self.assertEqual(snap.status, STATUS_WITHDRAWN)
        # 快照仍可按 id 取回
        self.assertEqual(self.reg.get("risk_parameter:RR-BAD@v1").status,
                         STATUS_WITHDRAWN)
        self.assertEqual(self.reg.resolve_claim(
            rr_slot("IHD", "ALL", "ALL")), "absent")


class EstimationContractTest(unittest.TestCase):
    def test_missing_year_is_estimated_and_never_observed(self):
        reg, engine, pub, run, report = build_v1()
        # 2021 没有任何调查观测
        cell = run.get(("CHN", 2021, "MALE", "15-59", "IHD"))
        self.assertEqual(cell["status"], CELL_ESTIMATED)
        self.assertEqual(cell["estimation"]["method"], "linear_interpolation")
        self.assertEqual(cell["estimation"]["anchor_years"], [2020, 2022])
        # 插值点取值恰为两端点均值
        p = cell["inputs"]["exposure_p"]
        lo = run.get(("CHN", 2020, "MALE", "15-59", "IHD"))["inputs"]["exposure_p"]
        hi = run.get(("CHN", 2022, "MALE", "15-59", "IHD"))["inputs"]["exposure_p"]
        self.assertAlmostEqual(p, (lo + hi) / 2, places=6)
        # 端点年仍是观测
        self.assertEqual(
            run.get(("CHN", 2020, "MALE", "15-59", "IHD"))["status"],
            CELL_OBSERVED)

    def test_no_support_year_is_no_input_not_a_number(self):
        reg = Registry()
        seed_indicator_and_geo(reg)
        seed_rr(reg)
        seed_mortality(reg)
        # 只导入 2020 一个观测年，2022 超出 max_gap
        reg.import_record(
            KIND_SURVEY, "S",
            {"slot": exp_slot("CHN", 2020, "MALE", "15-59"), "value": 0.5,
             "uncertainty": [0.47, 0.53]})
        engine = Engine()
        run = engine.run(reg, [2020, 2022], run_id="r", max_gap=1,
                         estimation_method="nearest_carry")
        far = run.get(("CHN", 2022, "MALE", "15-59", "IHD"))
        self.assertEqual(far["status"], CELL_NO_INPUT)
        self.assertIsNone(far["value"], "缺乏支撑的年份不得产出数字")

    def test_unadjudicated_conflict_blocks_computation(self):
        reg = Registry()
        seed_indicator_and_geo(reg)
        seed_rr(reg)
        seed_mortality(reg)
        seed_surveys(reg)
        # 对一个 RR 槽位注入竞争声明且不裁定
        reg.import_record(
            KIND_LITERATURE, "RR-IHD-ALT",
            {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 1.8,
             "uncertainty": [1.5, 2.1]})
        engine = Engine()
        run = engine.run(reg, YEARS, run_id="r")
        cell = run.get(("CHN", 2020, "MALE", "15-59", "IHD"))
        self.assertEqual(cell["status"], CELL_CONFLICT)
        self.assertIsNone(cell["value"])


class TraceabilityContractTest(unittest.TestCase):
    def test_every_number_has_inputs_and_formula_version(self):
        reg, engine, pub, run, report = build_v1()
        for cell in run.iter_cells():
            if cell["status"] in (CELL_OBSERVED, CELL_ESTIMATED):
                self.assertTrue(cell["dependency_snapshots"])
                self.assertEqual(cell["formula_version"], BURDEN_PIPELINE["id"])
                self.assertIn("paf-v1", cell["formula_steps"])
                for name in ("exposure_p", "rr", "mortality_base"):
                    self.assertIn(name, cell["inputs"])
                self.assertTrue(cell["fingerprint"])

    def test_headline_is_around_1_7m_and_split_with_uncertainty(self):
        reg, engine, pub, run, report = build_v1()
        h = report["headline"]["global"]
        self.assertTrue(
            1_000_000 < h["attributable_deaths"] < 3_000_000,
            f"全球归因死亡量级异常: {h['attributable_deaths']}")
        self.assertEqual(len(h["ui95"]), 2)
        self.assertLess(h["ui95"][0], h["attributable_deaths"])
        self.assertLess(h["attributable_deaths"], h["ui95"][1])
        # 拆分：性别合计、疾病合计应与头条一致（抑制单元除外，见另测）
        sex_sum = sum(v["attributable_deaths"]
                      for v in report["headline"]["by_sex"].values())
        dis_sum = sum(v["attributable_deaths"]
                      for v in report["headline"]["by_disease"].values())
        self.assertAlmostEqual(sex_sum, h["attributable_deaths"], places=2)
        self.assertAlmostEqual(dis_sum, h["attributable_deaths"], places=2)
        # 儿童年龄段单独拆分且有标记
        for age in ("0-4", "5-14"):
            block = report["headline"]["by_age_group"][age]
            self.assertTrue(block["is_child"])

    def test_headline_declares_observed_vs_estimated_mix(self):
        reg, engine, pub, run, report = build_v1()
        h = report["headline"]["global"]
        self.assertGreater(h["observed_cells"], 0)
        self.assertGreater(h["estimated_cells"], 0,
                           "2021 年为插值估算，必须在头条口径中可见")


class DisclosureContractTest(unittest.TestCase):
    def test_small_sample_cells_are_suppressed_and_excluded(self):
        reg, engine, pub, run, report = build_v1()
        # USA 儿童单元样本 n=30 < 50
        cell = report["cells"][("USA", 2020, "MALE", "0-4", "IHD")]
        self.assertEqual(cell["published_status"], CELL_SUPPRESSED)
        self.assertIsNone(cell["value"])
        self.assertEqual(cell["suppression"]["threshold"], MIN_SAMPLE)
        self.assertEqual(cell["suppression"]["supporting_sample_n"], 30)
        # 抑制数计入披露元数据
        self.assertGreaterEqual(report["disclosure"]["suppressed_cells"], 8)
        # 抑制单元不进汇总：头条年 0-4 岁汇总单元数应等于实际披露单元数
        headline_year = report["scope"]["headline_year"]
        child = report["headline"]["by_age_group"]["0-4"]
        contributing = [
            c for c in report["cells"].values()
            if c.get("published_status") in (CELL_OBSERVED, CELL_ESTIMATED)
            and c["age_group"] == "0-4"
            and c["year"] == headline_year
        ]
        self.assertEqual(child["cells"], len(contributing))


class IncrementalUpdateContractTest(unittest.TestCase):
    def test_late_survey_and_rr_withdrawal_recompute_only_dependent_branch(self):
        reg, engine, pub, run_v1, report_v1 = build_v1()
        v1_cells = {k: c["fingerprint"] for k, c in run_v1.cells.items()}

        # ---- 更新 1：迟到调查（IND 2021 实际观测，此前为插值）----
        late_snap, _ = reg.import_record(
            KIND_SURVEY, "SURV-IND-LATE-2021",
            {"slot": exp_slot("IND", 2021, "MALE", "15-59"), "value": 0.47,
             "uncertainty": [0.44, 0.50],
             "sampling": {"design": "分层抽样", "sample_n": 350}},
            provenance={"sample_n": 350, "note": "迟到回收的邦级调查"})
        run_v2 = engine.run(reg, YEARS, run_id="run-v2", label="补入迟到调查",
                            estimation_method="linear_interpolation", max_gap=3)
        changed = [k for k, fp in v1_cells.items()
                   if run_v2.get(k)["fingerprint"] != fp]
        # 只有依赖 IND 男性15-59 暴露的单元（各疾病×3年中2021状态翻转；
        # 2020/2022 指纹不变）发生变化
        affected_keys = [k for k in changed]
        self.assertTrue(all(k[0] == "IND" and k[2] == "MALE" and k[3] == "15-59"
                            for k in affected_keys))
        self.assertTrue(any(k[1] == 2021 for k in affected_keys))
        self.assertTrue(all(k[1] != 2020 for k in affected_keys),
                        "2020 观测单元不依赖迟到调查，不应重算")
        late_cell = run_v2.get(("IND", 2021, "MALE", "15-59", "IHD"))
        self.assertEqual(late_cell["status"], CELL_OBSERVED)
        self.assertEqual(late_cell["estimation"]["method"], "observed")
        self.assertEqual(run_v2.config["summary"]["reused"],
                         run_v2.config["summary"]["cells"] - len(changed))

        # ---- 更新 2：撤回一个风险参数（IHD RR 论文撤回）----
        withdrawn = reg.withdraw(kind=KIND_RISK_PARAM,
                                 source_id="RR-IHD-GBD2019",
                                 reason="RR 综合分析原始研究被撤回",
                                 by="方法学负责人")
        v2_cells = {k: c["fingerprint"] for k, c in run_v2.cells.items()}
        run_v3 = engine.run(reg, YEARS, run_id="run-v3",
                            label="撤回 IHD 风险参数",
                            estimation_method="linear_interpolation", max_gap=3)
        # IHD 全部单元变 void（无替代 RR），其他疾病指纹沿 v2 不变
        for key, cell in run_v3.cells.items():
            if key[4] == "IHD":
                self.assertEqual(cell["status"], CELL_VOID,
                                 f"{key} 依赖的 RR 已撤回，应作废")
                self.assertIsNone(cell["value"])
            else:
                self.assertEqual(cell["fingerprint"], v2_cells[key],
                                 f"{key} 不依赖 IHD-RR，不应重算")
        # IHD 单元的依赖里仍记录着被撤回快照，便于追溯"为何作废"
        sample = run_v3.get(("CHN", 2020, "FEMALE", "60+", "IHD"))
        self.assertIn("risk_parameter:RR-IHD-GBD2019@v1",
                      sample["dependency_snapshots"])

    def test_replacement_rr_revives_only_ihd_branch(self):
        reg, engine, pub, run_v1, report_v1 = build_v1()
        reg.withdraw(kind=KIND_RISK_PARAM, source_id="RR-IHD-GBD2019",
                     reason="撤回", by="方法学负责人")
        run_void = engine.run(reg, YEARS, run_id="run-void",
                              estimation_method="linear_interpolation", max_gap=3)
        # 补入替代 RR（新来源）
        reg.import_record(
            KIND_RISK_PARAM, "RR-IHD-REPL-2024",
            {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 1.30,
             "uncertainty": [1.12, 1.49]},
            provenance={"citation": "2024 重新荟萃分析"})
        run_fix = engine.run(reg, YEARS, run_id="run-fix",
                             estimation_method="linear_interpolation", max_gap=3)
        for key, cell in run_fix.cells.items():
            if key[4] == "IHD":
                self.assertEqual(cell["first_run"], "run-fix")
                self.assertIn("risk_parameter:RR-IHD-REPL-2024@v1",
                              cell["dependency_snapshots"])
            else:
                # 非 IHD 分支一路沿用 v1，从未重算
                self.assertEqual(cell["first_run"], "run-v1")
                self.assertEqual(cell["reused_from"], "run-void")


class PublicationImmutabilityTest(unittest.TestCase):
    def test_old_report_is_immutable_and_reproducible(self):
        reg, engine, pub, run_v1, report_v1 = build_v1()
        before_global = report_v1["headline"]["global"]["attributable_deaths"]
        before_cell = report_v1["cells"][("CHN", 2021, "MALE", "15-59", "IHD")]
        self.assertEqual(before_cell["status"], CELL_ESTIMATED)

        # 登记处此后发生迟到导入、撤回、重算
        reg.import_record(
            KIND_SURVEY, "SURV-IND-LATE-2021",
            {"slot": exp_slot("IND", 2021, "MALE", "15-59"), "value": 0.47,
             "uncertainty": [0.44, 0.50]},
            provenance={"sample_n": 350})
        reg.withdraw(kind=KIND_RISK_PARAM, source_id="RR-IHD-GBD2019",
                     reason="撤回", by="方法学负责人")
        engine.run(reg, YEARS, run_id="run-v2",
                   estimation_method="linear_interpolation", max_gap=3)

        # 旧报告数字一字不改
        self.assertEqual(
            report_v1["headline"]["global"]["attributable_deaths"], before_global)
        # 报告不可覆盖发布
        with self.assertRaises(ValueError):
            pub.publish(engine.latest_run(), report_id="GBD-SHS-2020-v1",
                        title="x", min_sample_n=MIN_SAMPLE, by="x",
                        published_at="2024-01-01")

        # 按冻结输入+公式版本原样复现，不看登记处现状
        result = pub.reproduce("GBD-SHS-2020-v1", ENGINE_FORMULAS)
        self.assertTrue(result["reproducible"], result)
        self.assertEqual(result["mismatches"], 0)
        self.assertTrue(result["headline_matches"])
        # 且复现能识别陈旧依赖（报告引用了已撤回快照，但数字仍可原样重算）
        stale = pub.stale_dependencies("GBD-SHS-2020-v1", reg)
        self.assertIn("risk_parameter:RR-IHD-GBD2019@v1", stale)

    def test_literature_withdrawal_annotates_affected_conclusions(self):
        reg, engine, pub, run_v1, report_v1 = build_v1()
        # 一篇支撑某暴露值的文献撤回（用调查源模拟"论文来源"）
        target_snap = "survey:SURV-CHN-MALE-15-59-2020@v1"
        notice = pub.annotate_withdrawal(
            "GBD-SHS-2020-v1", reg,
            withdrawn_snapshot_id=target_snap,
            reason="该调查结果的分析论文被期刊撤回（原始调查数据待核）",
            at="2024-03-01", by="方法学负责人")
        self.assertGreater(notice["impacted_count"], 0)
        self.assertTrue(notice["headline_affected"])
        conc = notice["impacted_cells"][0]
        self.assertIn("CHN", conc["conclusion"])
        # 通告只追加：正文数字与已发布状态不变
        cell = report_v1["cells"][("CHN", 2020, "MALE", "15-59", "IHD")]
        self.assertEqual(cell["published_status"], CELL_OBSERVED)
        self.assertIsNotNone(cell["value"])
        self.assertEqual(len(pub.notices("GBD-SHS-2020-v1")), 1)
        # 旧报告仍可原样复现
        self.assertTrue(
            pub.reproduce("GBD-SHS-2020-v1", ENGINE_FORMULAS)["reproducible"])


if __name__ == "__main__":
    unittest.main()
