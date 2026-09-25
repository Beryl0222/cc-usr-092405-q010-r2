"""情景分析层：从可复现报告快照派生假设情景并受控比较。

研究团队在正式报告之外比较若干假设情景（例如控烟措施落地后的暴露
变化），同时保证临时参数绝不污染已发布的疾病负担结论：

* **派生自可复现报告快照**：情景以发布层冻结的报告为基底，只读不
  写；情景计算不回写登记处、引擎或发布层；
* **只调整有来源说明的三类参数**：暴露比例（``exposure``）、相对风
  险选择（``rr``）、归因窗口（``window``）；每次调整必须携带
  ``source`` 来源说明；引用登记处快照（``source_snapshot``）时校验
  取值与之一致，并跟踪其后的撤回；
* **每次调整形成新版本，只重算依赖变化的分支**：版本沿父链继承调
  整；单元指纹 = 基底单元指纹 + 命中调整链 + 公式流水线，指纹不变
  的单元直接复用父版本结果；
* **基底规则继续沿用**：缺失年估算标记、未裁定冲突、最小样本抑制
  在情景中原样保留——调整不复活无输入 / 冲突 / 抑制单元；
* **并行编辑 + 另一角色复核**：同一父版本可并行起草；提交后须由
  「审核人」角色且非作者本人复核；同一父版本至多一个批准子版本，
  落选分支打 ``conflict`` 失效标记；只有「已批准且未失效」的版本
  进入对比摘要，原报告与其他情景始终不可变；
* **对比接口**：每个入选版本同时给出与基底报告的差值、输入谱系
  （调整链 + 证据快照 + 公式版本）与不确定性来源分解；任意版本可
  :meth:`ScenarioStore.replay` 重放，重放结果与冻结结果一致；
* **失效与重启一致**：调整来源被撤回 → 版本（含下游版本链）打
  ``invalidated`` 失效标记；全部状态变更记入只追加日志，
  :meth:`ScenarioStore.restore` 重放日志后，对比结果与失效标记保
  持一致。
"""

import copy

from .engine import BURDEN_PIPELINE, ENGINE_FORMULAS
from .registry import canonical_hash
from .terms import CELL_ESTIMATED, CELL_OBSERVED, STATUS_WITHDRAWN

# 可调整的三类参数（除此之外一律拒绝）
ADJ_EXPOSURE = "exposure"  # 暴露比例
ADJ_RR = "rr"              # 相对风险选择
ADJ_WINDOW = "window"      # 归因窗口
ADJUSTMENT_TYPES = (ADJ_EXPOSURE, ADJ_RR, ADJ_WINDOW)

# 版本工作流状态
V_DRAFT = "draft"
V_SUBMITTED = "submitted"
V_APPROVED = "approved"
V_REJECTED = "rejected"
V_CONFLICT = "conflict"    # 并发编辑冲突：明确失效标记

# 有效性（与工作流正交）：调整来源撤回等导致的失效
VALIDITY_VALID = "valid"
VALIDITY_INVALIDATED = "invalidated"

ROLE_REVIEWER = "审核人"

_NUMERIC = (CELL_OBSERVED, CELL_ESTIMATED)
_EXPOSURE_SCOPE_KEYS = frozenset({"country", "sex", "age_group", "year"})
_RR_SCOPE_KEYS = frozenset({"disease", "sex", "age_group"})


class ScenarioError(ValueError):
    """情景层通用错误。"""


class AdjustmentError(ScenarioError):
    """调整类型越界、缺少来源说明或取值与所引快照不一致。"""


class ScenarioStateError(ScenarioError):
    """工作流状态不允许当前操作。"""


class ScenarioConflictError(ScenarioError):
    """同一父版本已存在批准分支，当前版本被标记为冲突。"""


class ScenarioStore:
    """情景存储：版本链、复核工作流、对比摘要与重放。

    ``reports``：``{report_id: 发布层冻结报告}``，只读使用；
    ``registry``：证据登记处（可选），用于校验所引快照并跟踪撤回。
    """

    def __init__(self, reports, registry=None, formulas=None):
        self._reports = reports
        self._registry = registry
        self._formulas = formulas or ENGINE_FORMULAS
        self._scenarios = {}
        self._versions = {}
        self._events = []
        self._seq = 0

    # ------------------------------------------------------------------ #
    # 情景与版本（只追加）
    # ------------------------------------------------------------------ #
    def create_scenario(self, scenario_id, base_report_id, *, label="",
                        by="", at=""):
        """从一个可复现报告快照派生新情景。"""
        if scenario_id in self._scenarios:
            raise ScenarioStateError(f"情景 {scenario_id} 已存在，不可覆盖")
        if base_report_id not in self._reports:
            raise ScenarioError(f"未知基底报告 {base_report_id}")
        self._record_and_apply({
            "action": "scenario_created",
            "scenario_id": scenario_id,
            "base_report_id": base_report_id,
            "label": label,
            "by": by,
            "at": at,
        })
        return self._scenarios[scenario_id]

    def create_version(self, scenario_id, *, version_id, parent_version_id=None,
                       adjustments=(), author="", label="", at=""):
        """在情景中追加一个版本；每次调整形成新版本。

        ``parent_version_id=None`` 表示直接派生自基底报告。调整在创建时
        校验并冻结，此后不可修改——继续调整请在其上追加新版本。
        """
        scenario = self._scenario(scenario_id)
        if version_id in self._versions:
            raise ScenarioStateError(f"版本 {version_id} 已存在，版本只追加不覆盖")
        if parent_version_id is not None:
            parent = self._version(parent_version_id)
            if parent["scenario_id"] != scenario_id:
                raise ScenarioStateError("父版本属于其他情景：情景之间不可串链")
        validated = self._validate_adjustments(scenario, version_id, adjustments)
        self._record_and_apply({
            "action": "version_created",
            "scenario_id": scenario_id,
            "version_id": version_id,
            "parent_version_id": parent_version_id,
            "adjustments": validated,
            "author": author,
            "label": label,
            "at": at,
        })
        return self._versions[version_id]

    # ------------------------------------------------------------------ #
    # 复核工作流：提交 → 另一角色复核 → 批准 / 拒绝
    # ------------------------------------------------------------------ #
    def submit(self, version_id, *, by="", at=""):
        version = self._version(version_id)
        if version["status"] != V_DRAFT:
            raise ScenarioStateError(
                f"版本 {version_id} 状态为 {version['status']}，不可提交")
        self._record_and_apply(
            {"action": "submitted", "version_id": version_id, "by": by, "at": at})
        return version

    def approve(self, version_id, *, by, role="", at=""):
        """批准在审版本。同一父版本至多一个批准分支，落选者打冲突标记。"""
        version = self._version(version_id)
        if version["status"] != V_SUBMITTED:
            raise ScenarioStateError(
                f"版本 {version_id} 状态为 {version['status']}，不可批准")
        if role != ROLE_REVIEWER:
            raise ScenarioStateError(f"复核须由「{ROLE_REVIEWER}」角色执行")
        if by == version["author"]:
            raise ScenarioStateError("作者不得复核本人版本：须交由另一角色复核")
        parent_id = version["parent_version_id"]
        if parent_id is not None:
            parent = self._version(parent_id)
            if parent["status"] != V_APPROVED:
                raise ScenarioStateError(
                    f"父版本 {parent_id} 尚未批准，须沿版本链顺序批准")
        for sibling in self._children_of(version["scenario_id"], parent_id):
            if sibling["version_id"] == version_id:
                continue
            if sibling["status"] == V_APPROVED:
                reason = (f"父版本 {parent_id} 的批准分支已存在："
                          f"{sibling['version_id']}，并发编辑产生版本冲突")
                self._record_and_apply({
                    "action": "conflict_marked",
                    "version_id": version_id,
                    "conflict_with": sibling["version_id"],
                    "reason": reason,
                    "at": at,
                })
                raise ScenarioConflictError(
                    f"版本 {version_id} 与已批准的 {sibling['version_id']} "
                    f"冲突（同一父版本），已标记 conflict")
        self._record_and_apply({
            "action": "approved",
            "version_id": version_id,
            "by": by,
            "role": role,
            "at": at,
        })
        # 同父的在审兄弟版本失去合并位置：立即打明确失效标记
        for sibling in self._children_of(version["scenario_id"], parent_id):
            if sibling["version_id"] == version_id:
                continue
            if sibling["status"] == V_SUBMITTED:
                self._record_and_apply({
                    "action": "conflict_marked",
                    "version_id": sibling["version_id"],
                    "conflict_with": version_id,
                    "reason": f"兄弟版本 {version_id} 已批准，本版本失去合并位置",
                    "at": at,
                })
        return version

    def reject(self, version_id, *, by, role="", reason="", at=""):
        version = self._version(version_id)
        if version["status"] != V_SUBMITTED:
            raise ScenarioStateError(
                f"版本 {version_id} 状态为 {version['status']}，不可拒绝")
        if role != ROLE_REVIEWER:
            raise ScenarioStateError(f"复核须由「{ROLE_REVIEWER}」角色执行")
        if by == version["author"]:
            raise ScenarioStateError("作者不得复核本人版本：须交由另一角色复核")
        self._record_and_apply({
            "action": "rejected",
            "version_id": version_id,
            "by": by,
            "role": role,
            "reason": reason,
            "at": at,
        })
        return version

    # ------------------------------------------------------------------ #
    # 失效标记：调整来源被撤回（沿版本链传播，幂等）
    # ------------------------------------------------------------------ #
    def refresh_validity(self, registry=None):
        """按登记处现状重算所有版本的有效性标记（确定性的，可反复调用）。"""
        registry = registry or self._registry
        if registry is None:
            return
        for version in self._versions.values():
            markers = []
            seen = set()
            for adj in self._effective_adjustments(version):
                snap_id = adj.get("source_snapshot")
                if not snap_id or snap_id in seen:
                    continue
                seen.add(snap_id)
                try:
                    status = registry.get(snap_id).status
                except KeyError:
                    markers.append({
                        "kind": "source_missing",
                        "snapshot": snap_id,
                        "adjustment": adj["adjustment_id"],
                        "detail": "调整引用的证据快照不在登记处",
                    })
                    continue
                if status == STATUS_WITHDRAWN:
                    markers.append({
                        "kind": "source_withdrawn",
                        "snapshot": snap_id,
                        "adjustment": adj["adjustment_id"],
                        "detail": "调整来源已被撤回",
                    })
            version["validity"] = {
                "status": VALIDITY_INVALIDATED if markers else VALIDITY_VALID,
                "markers": markers,
            }

    # ------------------------------------------------------------------ #
    # 对比摘要：差值 + 输入谱系 + 不确定性来源；只有批准且未失效版本入选
    # ------------------------------------------------------------------ #
    def compare(self, scenario_id=None):
        """构建对比摘要。

        ``entries``：已批准且未失效的版本（含差值、谱系、不确定性来源）；
        ``excluded``：未入选版本及其明确失效/排除标记。
        """
        if self._registry is not None:
            self.refresh_validity()
        entries, excluded, base_ids = [], [], set()
        for version in sorted(self._versions.values(), key=lambda v: v["seq"]):
            if scenario_id and version["scenario_id"] != scenario_id:
                continue
            scenario = self._scenarios[version["scenario_id"]]
            base_ids.add(scenario["base_report_id"])
            markers = []
            if version["status"] == V_CONFLICT:
                markers.append({
                    "kind": "version_conflict",
                    "detail": copy.deepcopy(version["conflict"]),
                })
            elif version["status"] != V_APPROVED:
                markers.append({
                    "kind": "not_approved",
                    "detail": version["status"],
                })
            if version["validity"]["status"] == VALIDITY_INVALIDATED:
                markers.extend(copy.deepcopy(version["validity"]["markers"]))
            if markers:
                excluded.append({
                    "scenario_id": version["scenario_id"],
                    "version_id": version["version_id"],
                    "status": version["status"],
                    "validity": version["validity"]["status"],
                    "markers": markers,
                })
                continue
            entries.append(self._entry(version, scenario))
        return {
            "summary_of": "approved_valid_versions",
            "base_reports": {
                rid: self._base_block(rid) for rid in sorted(base_ids)
            },
            "entries": entries,
            "excluded": excluded,
        }

    def _entry(self, version, scenario):
        report = self._reports[scenario["base_report_id"]]
        year = version["headline_year"]
        total = version["totals"][year]
        base_block = report["headline"]["by_year"].get(year)
        delta = None
        if base_block and base_block["attributable_deaths"]:
            diff = round(
                total["attributable_deaths"] - base_block["attributable_deaths"], 3)
            delta = {
                "year": year,
                "attributable_deaths": diff,
                "percent": round(
                    100.0 * diff / base_block["attributable_deaths"], 2),
                "ui95": [
                    round(total["ui95"][0] - base_block["ui95"][1], 3),
                    round(total["ui95"][1] - base_block["ui95"][0], 3),
                ],
            }
        return {
            "scenario_id": version["scenario_id"],
            "version_id": version["version_id"],
            "label": version["label"],
            "author": version["author"],
            "base_report_id": scenario["base_report_id"],
            "window": list(version["window"]),
            "headline_year": year,
            "total": total,
            "totals_by_year": version["totals"],
            "delta_vs_base": delta,
            "uncertainty_sources": self._uncertainty_sources(
                version["cells"], year),
            "lineage": self._lineage(version, scenario, report),
            "compute": dict(version["summary"]),
        }

    def _base_block(self, report_id):
        report = self._reports[report_id]
        return {
            "report_id": report_id,
            "run_id": report["run_id"],
            "headline_year": report["scope"]["headline_year"],
            "total": report["headline"]["global"],
            "totals_by_year": report["headline"]["by_year"],
        }

    def _lineage(self, version, scenario, report):
        """输入谱系：基底报告 + 调整链（含来源说明）+ 证据快照 + 公式版本。"""
        adjustments = []
        for chain_version in self._chain(version):
            for adj in chain_version["adjustments"]:
                view = {
                    "adjustment_id": adj["adjustment_id"],
                    "type": adj["type"],
                    "source": adj["source"],
                    "introduced_in": chain_version["version_id"],
                    "author": chain_version["author"],
                }
                for key in ("scope", "mode", "value", "ui", "years",
                            "source_snapshot"):
                    if key in adj:
                        view[key] = copy.deepcopy(adj[key])
                adjustments.append(view)
        deps = sorted({
            dep
            for cell in version["cells"].values()
            for dep in cell.get("dependency_snapshots", [])
        })
        return {
            "base_report_id": scenario["base_report_id"],
            "base_run_id": report["run_id"],
            "formula_version": BURDEN_PIPELINE["id"],
            "adjustments": adjustments,
            "dependency_snapshots": deps,
        }

    def _uncertainty_sources(self, cells, year):
        """按输入类别分解不确定性：每次只让一个输入取区间端点。"""
        items = [
            c for c in cells.values()
            if c["year"] == year
            and c.get("published_status") in _NUMERIC
            and c.get("value") is not None
        ]
        paf_fn = self._formulas["paf-v1"].fn

        def total_with(source, bound):
            idx = 0 if bound == "lo" else 1
            total = 0.0
            for cell in items:
                inp = cell["inputs"]
                p = inp["exposure_p"]
                rr = inp["rr"]
                deaths = inp["mortality_base"]
                if source == "exposure":
                    p = inp["exposure_ui"][idx]
                elif source == "rr":
                    rr = inp["rr_ui"][idx]
                else:
                    deaths = inp["mortality_ui"][idx]
                total += paf_fn(p, rr) * deaths
            return total

        return {
            source: [round(total_with(source, "lo"), 3),
                     round(total_with(source, "hi"), 3)]
            for source in ("exposure", "rr", "mortality")
        }

    # ------------------------------------------------------------------ #
    # 重放：任一版本按基底报告 + 调整链原样复算
    # ------------------------------------------------------------------ #
    def replay(self, version_id):
        """重放任一版本：从基底报告与调整链重算，校验与冻结结果一致。"""
        version = self._version(version_id)
        scenario = self._scenarios[version["scenario_id"]]
        report = self._reports[scenario["base_report_id"]]
        eff_adj = self._effective_adjustments(version)
        mismatches = []
        for key, base in report["cells"].items():
            stored = version["cells"].get(key)
            if stored is None:
                mismatches.append(list(key))
                continue
            pub_status = base.get("published_status", base.get("status"))
            if pub_status in _NUMERIC:
                applicable = [a for a in eff_adj if self._matches(a, key)]
                fingerprint = canonical_hash({
                    "base": base["fingerprint"],
                    "adjustments": [a["adjustment_id"] for a in applicable],
                    "pipeline": BURDEN_PIPELINE["id"],
                })
                fresh = self._compute_numeric_cell(base, applicable, fingerprint)
                ok = (
                    stored["fingerprint"] == fingerprint
                    and stored["value"] == fresh["value"]
                    and stored["ui"] == fresh["ui"]
                    and stored["paf"] == fresh["paf"]
                    and stored["inputs"] == fresh["inputs"]
                )
            else:
                fingerprint = canonical_hash({
                    "carry": base["fingerprint"],
                    "published_status": pub_status,
                })
                ok = stored["fingerprint"] == fingerprint
            if not ok:
                mismatches.append(list(key))
        totals = self._totals(version["cells"], version["window"])
        totals_match = totals == version["totals"]
        return {
            "version_id": version_id,
            "scenario_id": version["scenario_id"],
            "cells_checked": len(report["cells"]),
            "mismatches": len(mismatches),
            "mismatch_cells": mismatches,
            "totals_match": totals_match,
            "consistent": not mismatches and totals_match,
        }

    # ------------------------------------------------------------------ #
    # 日志与重启恢复
    # ------------------------------------------------------------------ #
    def journal(self):
        """只追加事件日志（创建/提交/批准/拒绝/冲突标记）。"""
        return copy.deepcopy(self._events)

    @classmethod
    def restore(cls, reports, registry=None, events=(), formulas=None):
        """服务重启后按日志重放恢复；随后按登记处现状重算失效标记。"""
        store = cls(reports, registry=registry, formulas=formulas)
        for event in events:
            event = copy.deepcopy(event)
            store._events.append(event)
            store._apply(event)
        store.refresh_validity()
        return store

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_version(self, version_id):
        return self._version(version_id)

    def versions(self):
        return sorted(self._versions.values(), key=lambda v: v["seq"])

    def versions_of(self, scenario_id):
        return [v for v in self.versions() if v["scenario_id"] == scenario_id]

    def get_scenario(self, scenario_id):
        return self._scenario(scenario_id)

    # ------------------------------------------------------------------ #
    # 内部：事件应用（恢复与在线共用同一路径，保证重放一致）
    # ------------------------------------------------------------------ #
    def _record_and_apply(self, event):
        event = {"seq": len(self._events) + 1, **event}
        self._events.append(event)
        self._apply(event)

    def _apply(self, event):
        action = event["action"]
        if action == "scenario_created":
            self._scenarios[event["scenario_id"]] = {
                "scenario_id": event["scenario_id"],
                "base_report_id": event["base_report_id"],
                "label": event.get("label", ""),
                "created_by": event.get("by", ""),
                "created_at": event.get("at", ""),
                "versions": [],
            }
        elif action == "version_created":
            self._apply_version_created(event)
        elif action == "submitted":
            self._versions[event["version_id"]]["status"] = V_SUBMITTED
        elif action == "approved":
            version = self._versions[event["version_id"]]
            version["status"] = V_APPROVED
            version["approved_by"] = event.get("by", "")
            version["approved_at"] = event.get("at", "")
        elif action == "rejected":
            version = self._versions[event["version_id"]]
            version["status"] = V_REJECTED
            version["rejected_by"] = event.get("by", "")
            version["reject_reason"] = event.get("reason", "")
        elif action == "conflict_marked":
            version = self._versions[event["version_id"]]
            version["status"] = V_CONFLICT
            version["conflict"] = {
                "with": event["conflict_with"],
                "reason": event["reason"],
                "at": event.get("at", ""),
            }
        else:  # pragma: no cover - 日志被篡改时显式失败
            raise ScenarioError(f"未知日志事件 {action}")

    def _apply_version_created(self, event):
        scenario = self._scenarios[event["scenario_id"]]
        parent = (self._versions.get(event["parent_version_id"])
                  if event.get("parent_version_id") else None)
        adjustments = copy.deepcopy(event["adjustments"])
        self._seq += 1
        version = {
            "version_id": event["version_id"],
            "scenario_id": event["scenario_id"],
            "parent_version_id": event.get("parent_version_id"),
            "seq": self._seq,
            "author": event.get("author", ""),
            "label": event.get("label", ""),
            "created_at": event.get("at", ""),
            "adjustments": adjustments,
            "status": V_DRAFT,
            "validity": {"status": VALIDITY_VALID, "markers": []},
            "conflict": None,
        }
        eff_adj = self._effective_adjustments(version)
        report = self._reports[scenario["base_report_id"]]
        version["window"] = self._window_of(report, eff_adj)
        cells, summary = self._compute_cells(version, parent, eff_adj)
        version["cells"] = cells
        version["summary"] = summary
        version["totals"] = self._totals(cells, version["window"])
        headline_year = report["scope"]["headline_year"]
        version["headline_year"] = (
            headline_year if headline_year in version["window"]
            else max(version["window"])
        )
        self._versions[version["version_id"]] = version
        scenario["versions"].append(version["version_id"])

    # ------------------------------------------------------------------ #
    # 内部：增量计算（指纹不变即复用父版本单元）
    # ------------------------------------------------------------------ #
    def _compute_cells(self, version, parent, eff_adj):
        scenario = self._scenarios[version["scenario_id"]]
        report = self._reports[scenario["base_report_id"]]
        parent_cells = parent["cells"] if parent else None
        cells = {}
        recomputed = reused = 0
        for key, base in report["cells"].items():
            pub_status = base.get("published_status", base.get("status"))
            if pub_status in _NUMERIC:
                applicable = [a for a in eff_adj if self._matches(a, key)]
                fingerprint = canonical_hash({
                    "base": base["fingerprint"],
                    "adjustments": [a["adjustment_id"] for a in applicable],
                    "pipeline": BURDEN_PIPELINE["id"],
                })
            else:
                # 无输入 / 冲突 / 抑制等单元：基底规则沿用，调整不复活
                applicable = None
                fingerprint = canonical_hash({
                    "carry": base["fingerprint"],
                    "published_status": pub_status,
                })
            prior = parent_cells.get(key) if parent_cells else None
            if prior is not None and prior["fingerprint"] == fingerprint:
                cell = copy.deepcopy(prior)
                cell["reused_from_version"] = parent["version_id"]
                reused += 1
            else:
                if applicable is None:
                    cell = self._carry_cell(base, fingerprint)
                else:
                    cell = self._compute_numeric_cell(
                        base, applicable, fingerprint)
                cell["first_version"] = version["version_id"]
                cell["reused_from_version"] = None
                recomputed += 1
            cells[key] = cell
        return cells, {
            "cells": len(cells),
            "recomputed": recomputed,
            "reused": reused,
        }

    def _carry_cell(self, base, fingerprint):
        cell = copy.deepcopy(base)
        cell["fingerprint"] = fingerprint
        cell["adjustments_applied"] = []
        return cell

    def _compute_numeric_cell(self, base, applicable, fingerprint):
        inputs = base["inputs"]
        p = float(inputs["exposure_p"])
        p_ui = [float(inputs["exposure_ui"][0]), float(inputs["exposure_ui"][1])]
        rr = float(inputs["rr"])
        rr_ui = [float(inputs["rr_ui"][0]), float(inputs["rr_ui"][1])]
        deaths = float(inputs["mortality_base"])
        deaths_ui = [float(inputs["mortality_ui"][0]),
                     float(inputs["mortality_ui"][1])]
        applied, extra_deps = [], []
        for adj in applicable:
            applied.append(adj["adjustment_id"])
            if adj.get("source_snapshot"):
                extra_deps.append(adj["source_snapshot"])
            if adj["type"] == ADJ_EXPOSURE:
                if adj["mode"] == "scale":
                    factor = adj["value"]
                    p = p * factor
                    p_ui = [p_ui[0] * factor, p_ui[1] * factor]
                else:  # set
                    new_p = adj["value"]
                    if adj.get("ui"):
                        p_ui = list(adj["ui"])
                    elif p > 0:
                        ratio = new_p / p
                        p_ui = [p_ui[0] * ratio, p_ui[1] * ratio]
                    else:
                        p_ui = [new_p, new_p]
                    p = new_p
            elif adj["type"] == ADJ_RR:
                rr, rr_ui = adj["value"], list(adj["ui"])
        p = min(max(p, 0.0), 1.0)
        p_ui = [min(max(x, 0.0), 1.0) for x in p_ui]
        paf_version = self._formulas["paf-v1"]
        paf = paf_version.fn(p, rr)
        paf_lo, paf_hi = paf_version.ui_fn(p, p_ui, rr, rr_ui)
        value = self._formulas["attributable_deaths-v1"].fn(paf, deaths)
        cell = copy.deepcopy(base)
        cell.update({
            "value": round(value, 6),
            "paf": round(paf, 8),
            "ui": [round(paf_lo * deaths_ui[0], 6),
                   round(paf_hi * deaths_ui[1], 6)],
            "inputs": {
                "exposure_p": p,
                "exposure_ui": p_ui,
                "rr": rr,
                "rr_ui": rr_ui,
                "mortality_base": deaths,
                "mortality_ui": deaths_ui,
            },
            "adjustments_applied": applied,
            "dependency_snapshots": sorted(
                set(base.get("dependency_snapshots", [])) | set(extra_deps)),
            "fingerprint": fingerprint,
        })
        return cell

    @staticmethod
    def _matches(adj, key):
        country, year, sex, age, disease = key
        scope = adj.get("scope") or {}
        if adj["type"] == ADJ_EXPOSURE:
            checks = (("country", country), ("sex", sex),
                      ("age_group", age), ("year", year))
        elif adj["type"] == ADJ_RR:
            checks = (("disease", disease), ("sex", sex), ("age_group", age))
        else:
            return False  # 归因窗口只影响汇总口径，不改单元取值
        for field, actual in checks:
            want = scope.get(field)
            if want is not None and want != actual:
                return False
        return True

    @staticmethod
    def _window_of(report, eff_adj):
        years = sorted(report["scope"]["years"])
        for adj in eff_adj:
            if adj["type"] == ADJ_WINDOW:
                years = list(adj["years"])
        return years

    def _totals(self, cells, years):
        return {year: self._sum([
            c for c in cells.values()
            if c["year"] == year
            and c.get("published_status") in _NUMERIC
            and c.get("value") is not None
        ]) for year in years}

    @staticmethod
    def _sum(items):
        value = sum(c["value"] for c in items)
        lo = sum((c["ui"][0] if c["ui"] else c["value"]) for c in items)
        hi = sum((c["ui"][1] if c["ui"] else c["value"]) for c in items)
        observed = sum(1 for c in items if c.get("published_status") == CELL_OBSERVED)
        return {
            "attributable_deaths": round(value, 3),
            "ui95": [round(lo, 3), round(hi, 3)],
            "cells": len(items),
            "observed_cells": observed,
            "estimated_cells": len(items) - observed,
        }

    # ------------------------------------------------------------------ #
    # 内部：调整校验
    # ------------------------------------------------------------------ #
    def _validate_adjustments(self, scenario, version_id, adjustments):
        report = self._reports[scenario["base_report_id"]]
        report_years = {int(y) for y in report["scope"]["years"]}
        validated = []
        for i, raw in enumerate(adjustments):
            adj = copy.deepcopy(raw)
            atype = adj.get("type")
            if atype not in ADJUSTMENT_TYPES:
                raise AdjustmentError(
                    f"不允许的调整类型 {atype!r}：只能调整暴露比例、"
                    "相对风险选择或归因窗口")
            if not str(adj.get("source") or "").strip():
                raise AdjustmentError("每次调整必须携带 source 来源说明")
            adj["adjustment_id"] = (
                adj.get("adjustment_id") or f"{version_id}:a{i}")
            if atype == ADJ_EXPOSURE:
                self._validate_exposure(adj)
            elif atype == ADJ_RR:
                self._validate_rr(adj)
            else:
                self._validate_window(adj, report_years)
            self._verify_source_snapshot(adj)
            validated.append(adj)
        return validated

    @staticmethod
    def _check_scope(adj, allowed):
        scope = dict(adj.get("scope") or {})
        unknown = sorted(set(scope) - allowed)
        if unknown:
            raise AdjustmentError(f"调整范围含未知字段: {unknown}")
        adj["scope"] = scope

    def _validate_exposure(self, adj):
        self._check_scope(adj, _EXPOSURE_SCOPE_KEYS)
        if "year" in adj["scope"]:
            adj["scope"]["year"] = int(adj["scope"]["year"])
        mode = adj.get("mode", "scale")
        if mode not in ("scale", "set"):
            raise AdjustmentError("暴露比例调整 mode 只能是 scale / set")
        adj["mode"] = mode
        value = adj.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise AdjustmentError("暴露比例调整缺少数值 value")
        if mode == "scale" and value <= 0:
            raise AdjustmentError("scale 因子必须为正")
        if mode == "set" and not 0.0 <= value <= 1.0:
            raise AdjustmentError("暴露比例设定值必须在 [0,1]")
        ui = adj.get("ui")
        if ui is not None:
            if len(ui) != 2 or not 0.0 <= ui[0] <= ui[1] <= 1.0:
                raise AdjustmentError("暴露区间 ui 必须满足 0≤lo≤hi≤1")
            adj["ui"] = [float(ui[0]), float(ui[1])]
        adj["value"] = float(value)

    def _validate_rr(self, adj):
        self._check_scope(adj, _RR_SCOPE_KEYS)
        if not adj["scope"].get("disease"):
            raise AdjustmentError("相对风险选择必须指定 disease")
        value = adj.get("value")
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or value <= 0):
            raise AdjustmentError("相对风险选择缺少正的数值 value")
        ui = adj.get("ui") or [value, value]
        if len(ui) != 2 or not 0 < ui[0] <= ui[1]:
            raise AdjustmentError("RR 区间 ui 必须满足 0<lo≤hi")
        adj["value"] = float(value)
        adj["ui"] = [float(ui[0]), float(ui[1])]

    @staticmethod
    def _validate_window(adj, report_years):
        years = adj.get("years")
        if not isinstance(years, (list, tuple)) or not years:
            raise AdjustmentError("归因窗口调整必须给出非空 years")
        years = sorted({int(y) for y in years})
        unknown = [y for y in years if y not in report_years]
        if unknown:
            raise AdjustmentError(f"归因窗口超出基底报告年份: {unknown}")
        adj["years"] = years

    def _verify_source_snapshot(self, adj):
        """引用登记处快照时：快照须存在，取值/区间须与之一致。"""
        snap_id = adj.get("source_snapshot")
        if not snap_id or self._registry is None:
            return
        try:
            snap = self._registry.get(snap_id)
        except KeyError:
            raise AdjustmentError(f"引用的证据快照不存在: {snap_id}")
        check_value = adj["type"] == ADJ_RR or (
            adj["type"] == ADJ_EXPOSURE and adj["mode"] == "set")
        if not check_value:
            return
        content_value = snap.content.get("value")
        if (content_value is not None
                and abs(float(content_value) - adj["value"]) > 1e-9):
            raise AdjustmentError(
                f"调整取值 {adj['value']} 与所引快照 {snap_id} "
                f"取值 {content_value} 不一致")
        content_ui = snap.content.get("uncertainty")
        adj_ui = adj.get("ui")
        if content_ui and adj_ui:
            if (abs(float(content_ui[0]) - adj_ui[0]) > 1e-9
                    or abs(float(content_ui[1]) - adj_ui[1]) > 1e-9):
                raise AdjustmentError("调整区间与所引快照不一致")

    # ------------------------------------------------------------------ #
    # 内部：谱系与导航
    # ------------------------------------------------------------------ #
    def _chain(self, version):
        chain = []
        node = version
        while node is not None:
            chain.append(node)
            node = self._versions.get(node["parent_version_id"])
        chain.reverse()
        return chain

    def _effective_adjustments(self, version):
        out = []
        for chain_version in self._chain(version):
            out.extend(chain_version["adjustments"])
        return out

    def _children_of(self, scenario_id, parent_version_id):
        return [
            self._versions[vid]
            for vid in self._scenarios[scenario_id]["versions"]
            if self._versions[vid]["parent_version_id"] == parent_version_id
        ]

    def _scenario(self, scenario_id):
        if scenario_id not in self._scenarios:
            raise KeyError(f"未知情景 {scenario_id}")
        return self._scenarios[scenario_id]

    def _version(self, version_id):
        if version_id not in self._versions:
            raise KeyError(f"未知版本 {version_id}")
        return self._versions[version_id]
