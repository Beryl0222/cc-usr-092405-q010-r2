"""情景分析层契约测试：派生、调整约束、增量重算、复核工作流、对比与
重放、失效标记与重启一致性。

场景（对应需求叙述）：

1. 情景从可复现报告快照派生，只能调整有来源说明的暴露比例、相对风险
   选择或归因窗口，临时参数不污染已发布结论；
2. 每次调整形成新版本，只重算依赖发生变化的分支；
3. 缺失年份、冲突文献、小样本披露规则在情景中继续沿用；
4. 多人并行编辑，提交后由另一角色复核，只有批准版本进入对比摘要，
   并发冲突打明确失效标记；
5. 对比接口同时交代差值、输入谱系与不确定性来源，可按任一版本重放；
6. 调整来源被撤回 → 版本（含下游链）打 invalidated 失效标记；
7. 服务重启后按日志恢复，复算结果与失效标记保持一致。
"""

import unittest

from domain.engine import ENGINE_FORMULAS, Engine
from domain.publisher import Publisher
from domain.registry import Registry
from domain.scenario import (
    ADJUSTMENT_TYPES,
    AdjustmentError,
    ScenarioConflictError,
    ScenarioStateError,
    ScenarioStore,
    VALIDITY_INVALIDATED,
    V_APPROVED,
    V_CONFLICT,
    V_DRAFT,
    V_REJECTED,
    V_SUBMITTED,
)
from domain.terms import (
    CELL_CONFLICT,
    CELL_ESTIMATED,
    CELL_NO_INPUT,
    CELL_SUPPRESSED,
    KIND_LITERATURE,
    KIND_RISK_PARAM,
    KIND_SURVEY,
)
from domain_contract import (
    MIN_SAMPLE,
    YEARS,
    build_v1,
    exp_slot,
    rr_slot,
    seed_indicator_and_geo,
    seed_mortality,
    seed_rr,
    seed_surveys,
)

REPORT_ID = "GBD-SHS-2020-v1"
REVIEWER = "审核人"


def exposure_adj(scope, factor, source, **extra):
    return {"type": "exposure", "scope": scope, "mode": "scale",
            "value": factor, "source": source, **extra}


def rr_adj(disease, value, ui, source, **extra):
    return {"type": "rr", "scope": {"disease": disease}, "value": value,
            "ui": list(ui), "source": source, **extra}


def make_store(reg, *reports):
    return ScenarioStore({r["report_id"]: r for r in reports}, registry=reg)


def approve_chain(store, version_ids, reviewer="审核人丙"):
    for vid in version_ids:
        store.submit(vid, by="提交人")
        store.approve(vid, by=reviewer, role=REVIEWER)


class AdjustmentValidationTest(unittest.TestCase):
    """只能调整有来源说明的三类参数。"""

    def setUp(self):
        self.reg, self.engine, self.pub, self.run, self.report = build_v1()
        self.store = make_store(self.reg, self.report)
        self.store.create_scenario("SCN", REPORT_ID, by="方法学甲")

    def create(self, adjustments):
        return self.store.create_version(
            "SCN", version_id=f"SCN-v{len(adjustments)}-{id(adjustments)}",
            parent_version_id=None, adjustments=adjustments,
            author="方法学甲")

    def test_adjustment_types_are_limited_to_the_three_sourced_kinds(self):
        self.assertEqual(set(ADJUSTMENT_TYPES), {"exposure", "rr", "window"})
        with self.assertRaises(AdjustmentError):
            self.create([{"type": "mortality", "value": 1.1,
                          "source": "试图调整死亡基数"}])
        with self.assertRaises(AdjustmentError):
            self.create([{"type": "formula", "value": 2,
                          "source": "试图改公式"}])

    def test_every_adjustment_requires_a_source(self):
        with self.assertRaises(AdjustmentError):
            self.create([{"type": "exposure", "scope": {"country": "CHN"},
                          "mode": "scale", "value": 0.8}])
        with self.assertRaises(AdjustmentError):
            self.create([{"type": "rr", "scope": {"disease": "IHD"},
                          "value": 1.3, "source": "  "}])

    def test_adjustment_payload_is_validated(self):
        # RR 必须指定疾病
        with self.assertRaises(AdjustmentError):
            self.create([{"type": "rr", "scope": {}, "value": 1.3,
                          "source": "x"}])
        # 暴露设定值越界
        with self.assertRaises(AdjustmentError):
            self.create([{"type": "exposure", "scope": {}, "mode": "set",
                          "value": 1.5, "source": "x"}])
        # 归因窗口不得超出基底报告年份
        with self.assertRaises(AdjustmentError):
            self.create([{"type": "window", "years": [2022, 2025],
                          "source": "x"}])
        # 范围字段拼写错误
        with self.assertRaises(AdjustmentError):
            self.create([{"type": "exposure",
                          "scope": {"contry": "CHN"}, "mode": "scale",
                          "value": 0.8, "source": "x"}])

    def test_rr_selection_must_match_cited_snapshot(self):
        self.reg.import_record(
            KIND_RISK_PARAM, "RR-IHD-REPL-2024",
            {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 1.30,
             "uncertainty": [1.12, 1.49]},
            provenance={"citation": "2024 重新荟萃分析"})
        # 取值与所引快照不一致 → 拒绝
        with self.assertRaises(AdjustmentError):
            self.create([rr_adj("IHD", 1.50, [1.3, 1.7], "2024 重新荟萃分析",
                                source_snapshot="risk_parameter:RR-IHD-REPL-2024@v1")])
        # 引用不存在的快照 → 拒绝
        with self.assertRaises(AdjustmentError):
            self.create([rr_adj("IHD", 1.30, [1.12, 1.49], "x",
                                source_snapshot="risk_parameter:NOPE@v1")])
        # 与快照一致 → 接受
        version = self.create([rr_adj(
            "IHD", 1.30, [1.12, 1.49], "2024 重新荟萃分析",
            source_snapshot="risk_parameter:RR-IHD-REPL-2024@v1")])
        self.assertEqual(version["status"], V_DRAFT)


class IncrementalRecomputeTest(unittest.TestCase):
    """每次调整形成新版本，只重算依赖发生变化的分支。"""

    def setUp(self):
        self.reg, self.engine, self.pub, self.run, self.report = build_v1()
        self.store = make_store(self.reg, self.report)
        self.store.create_scenario("SCN", REPORT_ID, by="方法学甲")
        self.v0 = self.store.create_version(
            "SCN", version_id="SCN-v0", parent_version_id=None,
            adjustments=[], author="方法学甲", label="基线派生")

    def test_baseline_version_mirrors_report_totals(self):
        self.assertEqual(self.v0["summary"]["recomputed"], 288)
        self.assertEqual(self.v0["summary"]["reused"], 0)
        for year in YEARS:
            self.assertEqual(self.v0["totals"][year],
                             self.report["headline"]["by_year"][year],
                             f"空调整基线情景 {year} 年汇总应与报告一致")

    def test_exposure_adjustment_recomputes_only_matching_branch(self):
        v1 = self.store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id="SCN-v0",
            adjustments=[exposure_adj(
                {"country": "CHN", "sex": "MALE", "age_group": "15-59"},
                0.8, "假设：控烟立法使工作场所暴露下降 20%（示范文献 A）")],
            author="方法学甲")
        # 命中分支：CHN × MALE × 15-59 × 3 年 × 4 病 = 12 个单元
        self.assertEqual(v1["summary"]["recomputed"], 12)
        self.assertEqual(v1["summary"]["reused"], 288 - 12)
        changed = [k for k, c in v1["cells"].items()
                   if c["reused_from_version"] is None]
        self.assertTrue(all(
            k[0] == "CHN" and k[2] == "MALE" and k[3] == "15-59"
            for k in changed))
        # 数值校验：暴露 ×0.8 后按同一公式版本重算
        cell = v1["cells"][("CHN", 2022, "MALE", "15-59", "IHD")]
        self.assertAlmostEqual(cell["inputs"]["exposure_p"], 0.57 * 0.8,
                               places=9)
        expected_paf = ENGINE_FORMULAS["paf-v1"].fn(0.57 * 0.8, 1.27)
        expected = round(expected_paf * 475750.0, 6)
        self.assertEqual(cell["value"], expected)
        self.assertEqual(cell["formula_version"], "shs_burden-v1")
        self.assertEqual(cell["adjustments_applied"], ["SCN-v1:a0"])
        # 缺失年估算标记沿用：2021 仍是插值估算
        est = v1["cells"][("CHN", 2021, "MALE", "15-59", "IHD")]
        self.assertEqual(est["status"], CELL_ESTIMATED)
        self.assertEqual(est["estimation"]["method"], "linear_interpolation")
        # 未命中分支沿用父版本结果
        stroke = v1["cells"][("USA", 2022, "FEMALE", "60+", "STROKE")]
        self.assertEqual(stroke["reused_from_version"], "SCN-v0")
        self.assertEqual(stroke["first_version"], "SCN-v0")
        # 汇总随之下降
        self.assertLess(v1["totals"][2022]["attributable_deaths"],
                        self.v0["totals"][2022]["attributable_deaths"])

    def test_rr_selection_recomputes_only_that_disease_branch(self):
        self.reg.import_record(
            KIND_RISK_PARAM, "RR-IHD-REPL-2024",
            {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 1.30,
             "uncertainty": [1.12, 1.49]})
        self.store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id="SCN-v0",
            adjustments=[exposure_adj(
                {"country": "CHN", "sex": "MALE", "age_group": "15-59"},
                0.8, "示范文献 A")], author="方法学甲")
        v2 = self.store.create_version(
            "SCN", version_id="SCN-v2", parent_version_id="SCN-v1",
            adjustments=[rr_adj(
                "IHD", 1.30, [1.12, 1.49], "2024 重新荟萃分析",
                source_snapshot="risk_parameter:RR-IHD-REPL-2024@v1")],
            author="方法学乙")
        # IHD 数值单元：3 国 × 3 年 × 2 性别 × 4 年龄 = 72，减去 USA 儿童
        # 抑制 12 个（沿用不重算）→ 60
        self.assertEqual(v2["summary"]["recomputed"], 60)
        self.assertEqual(v2["summary"]["reused"], 288 - 60)
        # 继承父链调整：CHN 男性 15-59 的暴露缩放仍然生效
        cell = v2["cells"][("CHN", 2022, "MALE", "15-59", "IHD")]
        self.assertAlmostEqual(cell["inputs"]["exposure_p"], 0.57 * 0.8,
                               places=9)
        self.assertEqual(cell["inputs"]["rr"], 1.30)
        self.assertEqual(cell["adjustments_applied"],
                         ["SCN-v1:a0", "SCN-v2:a0"])
        # 所引快照进入依赖谱系
        self.assertIn("risk_parameter:RR-IHD-REPL-2024@v1",
                      cell["dependency_snapshots"])
        # 非 IHD 分支指纹与父版本一致（未重算）
        self.assertEqual(
            v2["cells"][("CHN", 2022, "MALE", "15-59", "STROKE")]["fingerprint"],
            self.store.get_version("SCN-v1")["cells"][
                ("CHN", 2022, "MALE", "15-59", "STROKE")]["fingerprint"])

    def test_window_only_version_recomputes_nothing_but_reaggregates(self):
        self.store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id="SCN-v0",
            adjustments=[exposure_adj({"country": "CHN"}, 0.9, "示范文献 B")],
            author="方法学甲")
        v2 = self.store.create_version(
            "SCN", version_id="SCN-v2", parent_version_id="SCN-v1",
            adjustments=[{"type": "window", "years": [2022],
                          "source": "决策部门只评估 2022 目标年"}],
            author="方法学甲")
        self.assertEqual(v2["summary"]["recomputed"], 0)
        self.assertEqual(v2["summary"]["reused"], 288)
        self.assertEqual(sorted(v2["totals"]), [2022])
        self.assertEqual(v2["headline_year"], 2022)
        self.assertEqual(v2["window"], [2022])


class CarryOverRulesTest(unittest.TestCase):
    """缺失年份、冲突文献、小样本披露规则在情景中继续沿用。"""

    def test_conflict_estimated_and_suppressed_cells_carry_over(self):
        reg = Registry()
        seed_indicator_and_geo(reg)
        seed_rr(reg)
        seed_mortality(reg)
        seed_surveys(reg)
        # IHD 相对风险存在未裁定竞争声明
        reg.import_record(
            KIND_LITERATURE, "RR-IHD-ALT",
            {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 1.8,
             "uncertainty": [1.5, 2.1]})
        engine = Engine()
        run = engine.run(reg, YEARS, run_id="run-conflict")
        pub = Publisher()
        report = pub.publish(run, report_id="R-CONFLICT", title="含冲突基底",
                             min_sample_n=MIN_SAMPLE, by="报告发布者",
                             published_at="2024-01-01")
        store = make_store(reg, report)
        store.create_scenario("SCN", "R-CONFLICT", by="方法学甲")
        v1 = store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id=None,
            adjustments=[exposure_adj(
                {"country": "CHN", "sex": "MALE", "age_group": "15-59"},
                0.9, "示范文献 C")], author="方法学甲")
        # 冲突单元：调整不复活，仍为 conflict 且无数字
        conflict_cell = v1["cells"][("CHN", 2022, "MALE", "15-59", "IHD")]
        self.assertEqual(conflict_cell["published_status"], CELL_CONFLICT)
        self.assertIsNone(conflict_cell["value"])
        self.assertEqual(conflict_cell["adjustments_applied"], [])
        # 同范围非冲突疾病正常重算
        stroke = v1["cells"][("CHN", 2022, "MALE", "15-59", "STROKE")]
        self.assertIsNotNone(stroke["value"])
        # 小样本抑制沿用：USA 儿童单元无数字、不进汇总
        suppressed = v1["cells"][("USA", 2022, "MALE", "0-4", "STROKE")]
        self.assertEqual(suppressed["published_status"], CELL_SUPPRESSED)
        self.assertIsNone(suppressed["value"])
        # 2022 年汇总单元数 = 96 − 12 抑制（USA 儿童非 IHD）− 24 冲突 = 60
        self.assertEqual(v1["totals"][2022]["cells"], 60)

    def test_no_input_cells_are_not_resurrected_by_adjustments(self):
        reg = Registry()
        seed_indicator_and_geo(reg)
        seed_rr(reg)
        seed_mortality(reg)
        reg.import_record(
            KIND_SURVEY, "S",
            {"slot": exp_slot("CHN", 2020, "MALE", "15-59"), "value": 0.5,
             "uncertainty": [0.47, 0.53],
             "sampling": {"design": "分层抽样", "sample_n": 400}})
        engine = Engine()
        run = engine.run(reg, [2020, 2022], run_id="r-gap", max_gap=1,
                         estimation_method="nearest_carry")
        pub = Publisher()
        report = pub.publish(run, report_id="R-GAP", title="缺口基底",
                             min_sample_n=MIN_SAMPLE, by="报告发布者",
                             published_at="2024-01-01")
        store = make_store(reg, report)
        store.create_scenario("SCN", "R-GAP", by="方法学甲")
        v1 = store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id=None,
            adjustments=[{"type": "exposure",
                          "scope": {"country": "CHN", "sex": "MALE",
                                    "age_group": "15-59"},
                          "mode": "set", "value": 0.4,
                          "source": "假设暴露降至 40%（示范文献 D）"}],
            author="方法学甲")
        # 有观测的 2020 被重算为新暴露
        self.assertAlmostEqual(
            v1["cells"][("CHN", 2020, "MALE", "15-59", "IHD")]["inputs"]["exposure_p"],
            0.4, places=9)
        # 无支撑的 2022 仍是 no_input，不产出数字
        gap = v1["cells"][("CHN", 2022, "MALE", "15-59", "IHD")]
        self.assertEqual(gap["published_status"], CELL_NO_INPUT)
        self.assertIsNone(gap["value"])


class ReviewWorkflowTest(unittest.TestCase):
    """并行编辑、另一角色复核、冲突失效标记、只有批准版本进对比。"""

    def setUp(self):
        self.reg, self.engine, self.pub, self.run, self.report = build_v1()
        self.store = make_store(self.reg, self.report)
        self.store.create_scenario("SCN", REPORT_ID, by="方法学甲")

    def test_parallel_editing_and_review_roles(self):
        # 多人并行编辑：同一父版本（基底报告）上并行起草
        self.store.create_version(
            "SCN", version_id="SCN-vA", parent_version_id=None,
            adjustments=[exposure_adj({"country": "USA"}, 0.9, "示范文献 E")],
            author="方法学甲")
        self.store.create_version(
            "SCN", version_id="SCN-vB", parent_version_id=None,
            adjustments=[exposure_adj({"country": "CHN"}, 0.85, "示范文献 F")],
            author="方法学乙")
        self.store.submit("SCN-vA", by="方法学甲")
        self.store.submit("SCN-vB", by="方法学乙")
        # 作者不得复核本人版本
        with self.assertRaises(ScenarioStateError):
            self.store.approve("SCN-vA", by="方法学甲", role=REVIEWER)
        # 非审核人角色不得复核
        with self.assertRaises(ScenarioStateError):
            self.store.approve("SCN-vA", by="方法学乙", role="方法学人员")
        # 审核人批准 vA → vB 自动打冲突失效标记
        self.store.approve("SCN-vA", by="审核人丙", role=REVIEWER)
        vB = self.store.get_version("SCN-vB")
        self.assertEqual(vB["status"], V_CONFLICT)
        self.assertEqual(vB["conflict"]["with"], "SCN-vA")
        # 迟到分支再走流程：批准时显式冲突
        self.store.create_version(
            "SCN", version_id="SCN-vC", parent_version_id=None,
            adjustments=[exposure_adj({"country": "IND"}, 0.95, "示范文献 G")],
            author="方法学乙")
        self.store.submit("SCN-vC", by="方法学乙")
        with self.assertRaises(ScenarioConflictError):
            self.store.approve("SCN-vC", by="审核人丙", role=REVIEWER)
        self.assertEqual(self.store.get_version("SCN-vC")["status"],
                         V_CONFLICT)
        # 对比摘要：只有批准的 vA 入选，冲突版本带明确失效标记
        summary = self.store.compare(scenario_id="SCN")
        self.assertEqual([e["version_id"] for e in summary["entries"]],
                         ["SCN-vA"])
        excluded = {e["version_id"]: e for e in summary["excluded"]}
        self.assertEqual(
            excluded["SCN-vB"]["markers"][0]["kind"], "version_conflict")
        self.assertEqual(
            excluded["SCN-vC"]["markers"][0]["kind"], "version_conflict")

    def test_reject_and_parent_ordering_rules(self):
        self.store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id=None,
            adjustments=[exposure_adj({"country": "CHN"}, 0.9, "示范文献 H")],
            author="方法学甲")
        self.store.submit("SCN-v1", by="方法学甲")
        self.store.reject("SCN-v1", by="审核人丙", role=REVIEWER,
                          reason="来源不足以支撑假设幅度")
        self.assertEqual(self.store.get_version("SCN-v1")["status"],
                         V_REJECTED)
        # 父版本未批准时，子版本不得批准
        self.store.create_version(
            "SCN", version_id="SCN-v2", parent_version_id="SCN-v1",
            adjustments=[exposure_adj({"country": "CHN"}, 0.8, "示范文献 I")],
            author="方法学甲")
        self.store.submit("SCN-v2", by="方法学甲")
        with self.assertRaises(ScenarioStateError):
            self.store.approve("SCN-v2", by="审核人丙", role=REVIEWER)

    def test_only_approved_versions_enter_comparison(self):
        self.store.create_version(
            "SCN", version_id="SCN-v0", parent_version_id=None,
            adjustments=[], author="方法学甲")
        self.store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id="SCN-v0",
            adjustments=[exposure_adj({"country": "CHN"}, 0.8, "示范文献 J")],
            author="方法学甲")
        approve_chain(self.store, ["SCN-v0", "SCN-v1"])
        # 在审与草稿版本不进对比摘要
        self.store.create_version(
            "SCN", version_id="SCN-v2", parent_version_id="SCN-v1",
            adjustments=[exposure_adj({"country": "IND"}, 0.9, "示范文献 K")],
            author="方法学乙")
        self.store.submit("SCN-v2", by="方法学乙")
        self.store.create_version(
            "SCN", version_id="SCN-v3", parent_version_id="SCN-v1",
            adjustments=[], author="方法学乙")
        summary = self.store.compare(scenario_id="SCN")
        self.assertEqual([e["version_id"] for e in summary["entries"]],
                         ["SCN-v0", "SCN-v1"])
        excluded = {e["version_id"]: e["markers"][0]["detail"]
                    for e in summary["excluded"]}
        self.assertEqual(excluded["SCN-v2"], V_SUBMITTED)
        self.assertEqual(excluded["SCN-v3"], V_DRAFT)


class ComparisonInterfaceTest(unittest.TestCase):
    """对比接口同时交代差值、输入谱系与不确定性来源，可按任一版本重放。"""

    def setUp(self):
        self.reg, self.engine, self.pub, self.run, self.report = build_v1()
        self.store = make_store(self.reg, self.report)
        self.store.create_scenario("SCN", REPORT_ID, label="控烟情景",
                                   by="方法学甲")
        self.store.create_version(
            "SCN", version_id="SCN-v0", parent_version_id=None,
            adjustments=[], author="方法学甲", label="基线")
        self.store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id="SCN-v0",
            adjustments=[exposure_adj(
                {"country": "CHN"}, 0.8,
                "假设：全面控烟立法使中国暴露下降 20%（示范文献 L）")],
            author="方法学甲", label="中国立法情景")
        approve_chain(self.store, ["SCN-v0", "SCN-v1"])

    def test_compare_shows_delta_lineage_and_uncertainty(self):
        summary = self.store.compare(scenario_id="SCN")
        base = summary["base_reports"][REPORT_ID]
        self.assertEqual(base["total"],
                         self.report["headline"]["global"])
        entries = {e["version_id"]: e for e in summary["entries"]}
        # 基线条目差值为 0
        self.assertEqual(entries["SCN-v0"]["delta_vs_base"]["attributable_deaths"], 0.0)
        # 立法情景：差值为负且带区间
        delta = entries["SCN-v1"]["delta_vs_base"]
        self.assertLess(delta["attributable_deaths"], 0)
        self.assertLess(delta["percent"], 0)
        self.assertEqual(len(delta["ui95"]), 2)
        # 输入谱系：基底报告 + 调整链（含来源说明）+ 公式版本 + 证据快照
        lineage = entries["SCN-v1"]["lineage"]
        self.assertEqual(lineage["base_report_id"], REPORT_ID)
        self.assertEqual(lineage["formula_version"], "shs_burden-v1")
        self.assertEqual(len(lineage["adjustments"]), 1)
        adj = lineage["adjustments"][0]
        self.assertEqual(adj["type"], "exposure")
        self.assertEqual(adj["introduced_in"], "SCN-v1")
        self.assertIn("示范文献 L", adj["source"])
        self.assertTrue(lineage["dependency_snapshots"])
        # 不确定性来源：三类输入各自对总量的区间贡献，均包住点估计
        sources = entries["SCN-v1"]["uncertainty_sources"]
        total = entries["SCN-v1"]["total"]["attributable_deaths"]
        for name in ("exposure", "rr", "mortality"):
            lo, hi = sources[name]
            self.assertLessEqual(lo, total)
            self.assertLessEqual(total, hi)
        # 汇总口径随版本窗口交代
        self.assertEqual(entries["SCN-v1"]["window"], YEARS)
        self.assertEqual(entries["SCN-v1"]["headline_year"], 2022)

    def test_any_version_can_be_replayed(self):
        for vid in ("SCN-v0", "SCN-v1"):
            result = self.store.replay(vid)
            self.assertEqual(result["mismatches"], 0, result)
            self.assertTrue(result["totals_match"])
            self.assertTrue(result["consistent"])
            self.assertEqual(result["cells_checked"], 288)


class WithdrawalInvalidationTest(unittest.TestCase):
    """调整来源被撤回 → 明确失效标记，沿版本链传播，复算保持一致。"""

    def setUp(self):
        self.reg, self.engine, self.pub, self.run, self.report = build_v1()
        self.reg.import_record(
            KIND_RISK_PARAM, "RR-IHD-REPL-2024",
            {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 1.30,
             "uncertainty": [1.12, 1.49]},
            provenance={"citation": "2024 重新荟萃分析"})
        self.store = make_store(self.reg, self.report)
        self.store.create_scenario("SCN", REPORT_ID, by="方法学甲")
        self.store.create_version(
            "SCN", version_id="SCN-v0", parent_version_id=None,
            adjustments=[], author="方法学甲")
        self.store.create_version(
            "SCN", version_id="SCN-v1", parent_version_id="SCN-v0",
            adjustments=[rr_adj(
                "IHD", 1.30, [1.12, 1.49], "2024 重新荟萃分析",
                source_snapshot="risk_parameter:RR-IHD-REPL-2024@v1")],
            author="方法学甲")
        self.store.create_version(
            "SCN", version_id="SCN-v2", parent_version_id="SCN-v1",
            adjustments=[exposure_adj({"country": "CHN"}, 0.95, "示范文献 M")],
            author="方法学乙")
        approve_chain(self.store, ["SCN-v0", "SCN-v1", "SCN-v2"])

    def test_withdrawal_marks_invalidation_and_propagates_downstream(self):
        before = self.store.compare(scenario_id="SCN")
        self.assertEqual(len(before["entries"]), 3)
        # 来源被撤回
        self.reg.withdraw(kind=KIND_RISK_PARAM, source_id="RR-IHD-REPL-2024",
                          reason="荟萃分析原始研究被撤回", by="方法学负责人")
        self.store.refresh_validity()
        # 直接引用版本与下游版本均失效
        for vid in ("SCN-v1", "SCN-v2"):
            validity = self.store.get_version(vid)["validity"]
            self.assertEqual(validity["status"], VALIDITY_INVALIDATED)
            self.assertEqual(validity["markers"][0]["kind"],
                             "source_withdrawn")
            self.assertEqual(validity["markers"][0]["snapshot"],
                             "risk_parameter:RR-IHD-REPL-2024@v1")
        # 未引用该来源的基线版本不受影响
        self.assertEqual(self.store.get_version("SCN-v0")["validity"]["status"],
                         "valid")
        # 失效版本退出对比摘要，并带明确失效标记
        summary = self.store.compare(scenario_id="SCN")
        self.assertEqual([e["version_id"] for e in summary["entries"]],
                         ["SCN-v0"])
        excluded = {e["version_id"]: e for e in summary["excluded"]}
        self.assertEqual(excluded["SCN-v1"]["validity"], VALIDITY_INVALIDATED)
        self.assertEqual(excluded["SCN-v2"]["markers"][0]["kind"],
                         "source_withdrawn")
        # 复算保持一致：重复刷新标记不变，重放结果不变
        self.store.refresh_validity()
        self.assertEqual(self.store.compare(scenario_id="SCN"), summary)
        for vid in ("SCN-v0", "SCN-v1", "SCN-v2"):
            self.assertTrue(self.store.replay(vid)["consistent"])
        # 已发布报告不受污染：数字不变、仍可原样复现
        self.assertTrue(self.pub.reproduce(REPORT_ID, ENGINE_FORMULAS)
                        ["reproducible"])


class RestartConsistencyTest(unittest.TestCase):
    """服务重启后按日志恢复：失效标记明确，复算结果保持一致。"""

    def test_restore_from_journal_replays_identical_results(self):
        reg, engine, pub, run, report = build_v1()
        reg.import_record(
            KIND_RISK_PARAM, "RR-IHD-REPL-2024",
            {"slot": rr_slot("IHD", "ALL", "ALL"), "value": 1.30,
             "uncertainty": [1.12, 1.49]})
        store = make_store(reg, report)
        # 情景一：批准链 + 将被撤回的来源
        store.create_scenario("SCN-1", REPORT_ID, label="立法情景",
                              by="方法学甲")
        store.create_version(
            "SCN-1", version_id="SCN-1-v0", parent_version_id=None,
            adjustments=[], author="方法学甲")
        store.create_version(
            "SCN-1", version_id="SCN-1-v1", parent_version_id="SCN-1-v0",
            adjustments=[rr_adj("IHD", 1.30, [1.12, 1.49], "2024 重新荟萃分析",
                                source_snapshot="risk_parameter:RR-IHD-REPL-2024@v1")],
            author="方法学甲")
        approve_chain(store, ["SCN-1-v0", "SCN-1-v1"])
        # 情景二：并行编辑产生版本冲突
        store.create_scenario("SCN-2", REPORT_ID, label="税收情景",
                              by="方法学甲")
        store.create_version(
            "SCN-2", version_id="SCN-2-vA", parent_version_id=None,
            adjustments=[exposure_adj({"country": "USA"}, 0.9, "示范文献 N")],
            author="方法学甲")
        store.create_version(
            "SCN-2", version_id="SCN-2-vB", parent_version_id=None,
            adjustments=[exposure_adj({"country": "USA"}, 0.85, "示范文献 O")],
            author="方法学乙")
        store.submit("SCN-2-vA", by="方法学甲")
        store.submit("SCN-2-vB", by="方法学乙")
        store.approve("SCN-2-vA", by="审核人丙", role=REVIEWER)
        # 来源撤回 → 失效标记
        reg.withdraw(kind=KIND_RISK_PARAM, source_id="RR-IHD-REPL-2024",
                     reason="撤回", by="方法学负责人")
        store.refresh_validity()
        before = store.compare()
        journal = store.journal()

        # —— 服务重启：从日志恢复 ——
        restored = ScenarioStore.restore({REPORT_ID: report}, reg, journal)
        after = restored.compare()
        self.assertEqual(after, before, "重启恢复后对比摘要必须逐字节一致")
        self.assertEqual(restored.journal(), journal, "日志本身也必须一致")
        # 失效标记在重启后依然明确
        kinds = {m["kind"] for e in after["excluded"] for m in e["markers"]}
        self.assertIn("source_withdrawn", kinds)
        self.assertIn("version_conflict", kinds)
        # 任一版本重放结果一致
        for version in store.versions():
            vid = version["version_id"]
            self.assertTrue(restored.replay(vid)["consistent"], vid)
            self.assertEqual(restored.get_version(vid)["summary"],
                             version["summary"], vid)


class ImmutabilityTest(unittest.TestCase):
    """原报告与其他情景始终不可变，临时参数不污染已发布结论。"""

    def test_report_registry_and_sibling_scenarios_are_untouched(self):
        reg, engine, pub, run, report = build_v1()
        headline_before = report["headline"]["global"]["attributable_deaths"]
        events_before = len(reg.events())
        store = make_store(reg, report)
        store.create_scenario("SCN-1", REPORT_ID, by="方法学甲")
        store.create_version(
            "SCN-1", version_id="SCN-1-v0", parent_version_id=None,
            adjustments=[], author="方法学甲")
        store.create_version(
            "SCN-1", version_id="SCN-1-v1", parent_version_id="SCN-1-v0",
            adjustments=[exposure_adj({"country": "CHN"}, 0.5, "示范文献 P")],
            author="方法学甲")
        approve_chain(store, ["SCN-1-v0", "SCN-1-v1"])
        entries_before = store.compare(scenario_id="SCN-1")["entries"]

        # 兄弟情景的全部操作不影响 SCN-1 的对比结果
        store.create_scenario("SCN-2", REPORT_ID, by="方法学乙")
        store.create_version(
            "SCN-2", version_id="SCN-2-v0", parent_version_id=None,
            adjustments=[rr_adj("COPD", 1.9, [1.5, 2.3], "示范文献 Q")],
            author="方法学乙")
        approve_chain(store, ["SCN-2-v0"])
        store.compare()
        self.assertEqual(store.compare(scenario_id="SCN-1")["entries"],
                         entries_before)

        # 报告数字一字不改、仍可原样复现；登记处未新增任何事件
        self.assertEqual(
            report["headline"]["global"]["attributable_deaths"],
            headline_before)
        self.assertTrue(pub.reproduce(REPORT_ID, ENGINE_FORMULAS)
                        ["reproducible"])
        self.assertEqual(len(reg.events()), events_before,
                         "情景层的临时参数不得写回证据登记处")

        # 版本只追加：同 id 拒绝覆盖；情景之间不可串链
        with self.assertRaises(ScenarioStateError):
            store.create_version(
                "SCN-1", version_id="SCN-1-v1",
                parent_version_id="SCN-1-v0", adjustments=[],
                author="方法学甲")
        with self.assertRaises(ScenarioStateError):
            store.create_version(
                "SCN-2", version_id="SCN-2-vX",
                parent_version_id="SCN-1-v0", adjustments=[],
                author="方法学乙")


if __name__ == "__main__":
    unittest.main()
