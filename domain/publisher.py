"""发布层：不可变报告、最小样本披露、撤回影响标注与原样复现。

* 发布即冻结：报告把当次运行的每个单元（取值、UI、输入、依赖快照、
  公式版本）完整复制，此后登记处如何变化都不改报告数字；
* 最小样本披露：支撑调查样本量低于阈值的单元抑制为
  ``suppressed_small_sample``，且不参与任何汇总；
* 拆分发布：头条数字必须同时给出按性别、年龄段（儿童单列）、疾病的
  拆分及观测/估算构成；
* 撤回不删报告：论文或风险参数撤回只在报告上追加"影响通告"，标出
  受影响单元与结论，旧报告原样保留，仍可按冻结证据复算。
"""

import copy
from collections import defaultdict

from .terms import (
    CELL_ESTIMATED,
    CELL_OBSERVED,
    CELL_SUPPRESSED,
    CHILD_AGE_GROUPS,
    STATUS_SUPERSEDED,
    STATUS_WITHDRAWN,
)

NUMERIC_STATUSES = (CELL_OBSERVED, CELL_ESTIMATED)


class PublicationError(ValueError):
    pass


class Publisher:
    def __init__(self):
        self._reports = {}
        self._notices = defaultdict(list)  # report_id -> [通告...]

    # ------------------------------------------------------------------ #
    # 发布（冻结）
    # ------------------------------------------------------------------ #
    def publish(self, run, *, report_id, title, min_sample_n, by, published_at,
                headline_year=None):
        if report_id in self._reports:
            raise PublicationError(f"报告 {report_id} 已存在，报告不可覆盖")
        headline_year = headline_year or max(run.config["years"])
        cells = {}
        suppressed = unknown_n = 0
        for cell in run.iter_cells():
            frozen = copy.deepcopy(cell)
            sample_n = (frozen.get("estimation") or {}).get("sample_n")
            if frozen["status"] in NUMERIC_STATUSES:
                if sample_n is not None and sample_n < min_sample_n:
                    frozen["published_status"] = CELL_SUPPRESSED
                    frozen["suppression"] = {
                        "rule": "min_sample_n",
                        "threshold": min_sample_n,
                        "supporting_sample_n": sample_n,
                    }
                    # 发布件中不保留被抑制数字（仅保留追溯用依赖与口径）
                    frozen["value"] = None
                    frozen["paf"] = None
                    frozen["ui"] = None
                    suppressed += 1
                else:
                    frozen["published_status"] = frozen["status"]
                    if sample_n is None:
                        unknown_n += 1
            else:
                frozen["published_status"] = frozen["status"]
            cells[tuple(frozen.pop("key"))] = frozen

        report = {
            "report_id": report_id,
            "title": title,
            "run_id": run.run_id,
            "published_at": published_at,
            "published_by": by,
            "scope": {
                "years": run.config["years"],
                "headline_year": headline_year,
                "sexes": run.config["sexes"],
                "age_groups": run.config["age_groups"],
                "countries": run.config["countries"],
                "diseases": run.config["diseases"],
                "estimation_method": run.config["estimation_method"],
                "max_gap_years": run.config["max_gap"],
                "formula_pipeline": run.cells and next(
                    iter(run.cells.values())
                )["formula_version"],
            },
            "disclosure": {
                "min_sample_n": min_sample_n,
                "suppressed_cells": suppressed,
                "unknown_sample_n_cells": unknown_n,
                "rule": (
                    f"支撑调查样本量 n<{min_sample_n} 的单元不予披露，"
                    "且不计入任何汇总"
                ),
            },
            "cells": cells,
            "headline": self._headline(cells, headline_year),
        }
        self._reports[report_id] = report
        return report

    # ------------------------------------------------------------------ #
    # 汇总拆分
    # ------------------------------------------------------------------ #
    def _disclosed_numeric(self, cells):
        return [
            c
            for c in cells.values()
            if c.get("published_status") in NUMERIC_STATUSES
        ]

    def _sum(self, items):
        value = sum(c["value"] for c in items)
        lo = sum((c["ui"][0] if c["ui"] else c["value"]) for c in items)
        hi = sum((c["ui"][1] if c["ui"] else c["value"]) for c in items)
        observed = sum(1 for c in items if c["published_status"] == CELL_OBSERVED)
        return {
            "attributable_deaths": round(value, 3),
            "ui95": [round(lo, 3), round(hi, 3)],
            "cells": len(items),
            "observed_cells": observed,
            "estimated_cells": len(items) - observed,
        }

    def _headline(self, cells, headline_year):
        """构建头条数字。**只汇总 headline_year 一年**，年份口径显式标注。"""
        items = [
            c
            for c in self._disclosed_numeric(cells)
            if c["year"] == headline_year
        ]
        headline = {
            "year": headline_year,
            "global": self._sum(items),
            "by_sex": {},
            "by_age_group": {},
            "by_disease": {},
            "child_age_groups": list(CHILD_AGE_GROUPS),
        }
        for sex in sorted({c["sex"] for c in items}):
            headline["by_sex"][sex] = self._sum(
                [c for c in items if c["sex"] == sex]
            )
        for age in sorted({c["age_group"] for c in items}):
            block = self._sum([c for c in items if c["age_group"] == age])
            block["is_child"] = age in CHILD_AGE_GROUPS
            headline["by_age_group"][age] = block
        for disease in sorted({c["disease"] for c in items}):
            headline["by_disease"][disease] = self._sum(
                [c for c in items if c["disease"] == disease]
            )
        # 各年全球汇总：让"哪些年份含估算值"一目了然
        all_numeric = self._disclosed_numeric(cells)
        headline["by_year"] = {}
        for yr in sorted({c["year"] for c in all_numeric}):
            headline["by_year"][yr] = self._sum(
                [c for c in all_numeric if c["year"] == yr]
            )
        return headline

    # ------------------------------------------------------------------ #
    # 撤回影响通告（只追加，不改正文）
    # ------------------------------------------------------------------ #
    def annotate_withdrawal(self, report_id, registry, *, withdrawn_snapshot_id,
                            reason, at, by):
        """登记处某快照撤回/失效后，标出报告中依赖它的单元与结论。"""
        report = self._reports[report_id]
        impacted = []
        for key, cell in report["cells"].items():
            if withdrawn_snapshot_id in cell.get("dependency_snapshots", []):
                impacted.append(
                    {
                        "cell": list(key),
                        "published_status": cell.get("published_status"),
                        "value": cell.get("value"),
                        "conclusion": (
                            f"{cell.get('country')} {cell.get('year')} "
                            f"{cell.get('sex')} {cell.get('age_group')} "
                            f"{cell.get('disease')} 归因死亡估算"
                        ),
                    }
                )
        notice = {
            "seq": len(self._notices[report_id]) + 1,
            "at": at,
            "by": by,
            "withdrawn_snapshot": withdrawn_snapshot_id,
            "snapshot_state": self._snapshot_state(registry, withdrawn_snapshot_id),
            "reason": reason,
            "impacted_cells": impacted,
            "impacted_count": len(impacted),
            "headline_affected": bool(impacted),
            "action": "标注受影响结论；报告正文与数字不修改、不删除",
        }
        self._notices[report_id].append(notice)
        return notice

    @staticmethod
    def _snapshot_state(registry, snapshot_id):
        try:
            return registry.get(snapshot_id).status
        except KeyError:
            return "未在当前登记处（可能为发布后重建）"

    def notices(self, report_id):
        return list(self._notices[report_id])

    def stale_dependencies(self, report_id, registry):
        """列出报告中所有在当前登记处已失效/已替代的依赖快照。"""
        report = self._reports[report_id]
        stale = {}
        for cell in report["cells"].values():
            for dep in cell.get("dependency_snapshots", []):
                try:
                    state = registry.get(dep).status
                except KeyError:
                    state = "缺失"
                if state in (STATUS_WITHDRAWN, STATUS_SUPERSEDED, "缺失"):
                    stale.setdefault(dep, {"state": state, "cells": 0})
                    stale[dep]["cells"] += 1
        return stale

    # ------------------------------------------------------------------ #
    # 原样复现
    # ------------------------------------------------------------------ #
    def reproduce(self, report_id, formula_registry):
        """用报告冻结的输入与公式版本逐单元重算，并重建头条汇总。

        不接触登记处当前状态——这正是"旧报告按当时证据原样复现"。
        """
        report = self._reports[report_id]
        checked = mismatches = 0
        for key, cell in report["cells"].items():
            if cell.get("published_status") not in NUMERIC_STATUSES:
                continue
            checked += 1
            inp = cell["inputs"]
            paf = formula_registry["paf-v1"].fn(inp["exposure_p"], inp["rr"])
            value = formula_registry["attributable_deaths-v1"].fn(
                paf, inp["mortality_base"]
            )
            if round(value, 6) != cell["value"]:
                mismatches += 1
        rebuilt = self._headline(report["cells"], report["scope"]["headline_year"])
        return {
            "report_id": report_id,
            "formula_pipeline": report["scope"]["formula_pipeline"],
            "numeric_cells_checked": checked,
            "mismatches": mismatches,
            "headline_matches": rebuilt == report["headline"],
            "headline_rebuilt": rebuilt,
            "reproducible": mismatches == 0 and rebuilt == report["headline"],
        }

    def get_report(self, report_id):
        return self._reports[report_id]
