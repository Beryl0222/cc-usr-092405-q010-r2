"""计算引擎：版本化公式、缺失年显式估算、依赖指纹与增量重算。

设计要点：

* 公式以 :class:`FormulaVersion` 注册，结果永远携带 ``formula_version``；
* 调查/文献只在实际观测年标记 ``observed``；目标年缺观测时按当次运行
  **明示配置**的估算方法（线性插值 / 临近结转）补值，标记 ``estimated``
  并记录方法与支撑观测快照，绝不冒充观测值；无支撑则 ``no_input``；
* 每个计算单元的依赖（暴露、指标定义、地区映射、RR、死亡基数的快照 id
  与取值）哈希为指纹；重跑时指纹不变的单元直接复用上一轮结果，只有
  依赖分支变化（迟到调查、风险参数撤回……）的单元被重算；
* 每次运行冻结全部输入取值，旧报告可脱离登记现状原样复现。
"""

import copy

from .registry import canonical_hash
from .terms import (
    AGE_GROUPS,
    CELL_CONFLICT,
    CELL_ESTIMATED,
    CELL_NO_INPUT,
    CELL_OBSERVED,
    CELL_VOID,
    KIND_GEO_MAPPING,
    KIND_INDICATOR,
    KIND_LITERATURE,
    KIND_MORTALITY,
    KIND_RISK_PARAM,
    KIND_SURVEY,
    STATUS_COMPUTABLE,
    STATUS_PENDING_ADJUDICATION,
)

INDICATOR_SH = "SHS_PREV"  # 非吸烟者二手烟暴露率

# --------------------------------------------------------------------- #
# 版本化公式
# --------------------------------------------------------------------- #
class FormulaVersion:
    def __init__(self, formula_id, version, title, fn, ui_fn, depends=()):
        self.id = f"{formula_id}-{version}"
        self.formula = formula_id
        self.version = version
        self.title = title
        self.fn = fn
        self.ui_fn = ui_fn
        self.depends = depends  # 依赖的其他公式版本 id

    def spec(self):
        return {
            "id": self.id,
            "formula": self.formula,
            "version": self.version,
            "title": self.title,
            "depends": list(self.depends),
        }


def _paf(p, rr):
    """人群归因分值：PAF = P·(RR−1) / (1 + P·(RR−1))。"""
    x = p * (rr - 1.0)
    return x / (1.0 + x)


def _paf_ui(p, p_ui, rr, rr_ui):
    # PAF 对 P、RR 均单调递增，端点组合即区间端点
    return _paf(p_ui[0], rr_ui[0]), _paf(p_ui[1], rr_ui[1])


def _attr_deaths(paf, deaths):
    return paf * deaths


FORMULA_REGISTRY = {
    f.id: f
    for f in (
        FormulaVersion("paf", "v1", "PAF=P(RR-1)/(1+P(RR-1))", _paf, _paf_ui),
        FormulaVersion(
            "attributable_deaths",
            "v1",
            "归因死亡=PAF×死亡基数",
            _attr_deaths,
            lambda paf, paf_ui, d, d_ui: (paf_ui[0] * d_ui[0], paf_ui[1] * d_ui[1]),
        ),
    )
}
# 端到端负担流水线版本（组合上面两个公式）
BURDEN_PIPELINE = {
    "id": "shs_burden-v1",
    "title": "二手烟归因负担流水线 v1",
    "steps": ["paf-v1", "attributable_deaths-v1"],
}

# 供发布层原样复现时引用
ENGINE_FORMULAS = FORMULA_REGISTRY


# --------------------------------------------------------------------- #
# 缺失年估算（方法必须随结果明示）
# --------------------------------------------------------------------- #
def estimate_exposure(series, year, method, max_gap):
    """根据观测序列推断目标年暴露。

    ``series``：按年排序的 ``(year, p, ui, snapshot_id, sample_n)``。

    返回 ``(status, p, ui, provenance)``：

    * 目标年恰好有观测 → ``observed``；
    *  bracketing 观测 → ``linear_interpolation``（estimated）；
    * 仅单侧有观测且距离 ≤ ``max_gap`` → ``nearest_carry``（estimated）；
    * 否则 ``no_input``——不产出数字。
    """
    if not series:
        return CELL_NO_INPUT, None, None, None
    exact = [pt for pt in series if pt[0] == year]
    if exact:
        _, p, ui, snap, n = exact[0]
        return CELL_OBSERVED, p, ui, {
            "method": "observed",
            "support": [snap],
            "sample_n": n,
        }

    before = [pt for pt in series if pt[0] < year]
    after = [pt for pt in series if pt[0] > year]
    if method == "linear_interpolation" and before and after:
        lo_pt, hi_pt = before[-1], after[0]
        if hi_pt[0] - lo_pt[0] > 2 * max_gap:
            return CELL_NO_INPUT, None, None, None
        frac = (year - lo_pt[0]) / (hi_pt[0] - lo_pt[0])
        p = lo_pt[1] + frac * (hi_pt[1] - lo_pt[1])
        ui = [
            lo_pt[2][0] + frac * (hi_pt[2][0] - lo_pt[2][0]),
            lo_pt[2][1] + frac * (hi_pt[2][1] - lo_pt[2][1]),
        ]
        return CELL_ESTIMATED, p, ui, {
            "method": "linear_interpolation",
            "support": [lo_pt[3], hi_pt[3]],
            "anchor_years": [lo_pt[0], hi_pt[0]],
            "sample_n": _min_sample([lo_pt[4], hi_pt[4]]),
        }

    nearest = None
    if before and after:
        nearest = min(before[-1], after[0], key=lambda pt: abs(pt[0] - year))
    elif before:
        nearest = before[-1]
    elif after:
        nearest = after[0]
    if nearest and abs(nearest[0] - year) <= max_gap:
        return CELL_ESTIMATED, nearest[1], list(nearest[2]), {
            "method": "nearest_carry",
            "support": [nearest[3]],
            "anchor_years": [nearest[0]],
            "sample_n": nearest[4],
        }
    return CELL_NO_INPUT, None, None, None


def _min_sample(values):
    values = [v for v in values if v is not None]
    return min(values) if values else None


# --------------------------------------------------------------------- #
# 计算单元与运行
# --------------------------------------------------------------------- #
def cell_key(country, year, sex, age, disease):
    return (country, year, sex, age, disease)


class Run:
    """一次计算运行：配置 + 每个单元的结果与冻结输入。"""

    def __init__(self, run_id, label, parent_run_id, config, seq):
        self.run_id = run_id
        self.label = label
        self.parent_run_id = parent_run_id
        self.config = copy.deepcopy(config)
        self.seq = seq
        self.cells = {}
        self.diagnostics = []

    def get(self, key):
        return self.cells.get(key)

    def iter_cells(self):
        return self.cells.values()


class Engine:
    def __init__(self):
        self._runs = {}
        self._order = []

    # ------------------------------------------------------------------ #
    def run(self, registry, years, *, run_id, label="", countries=None,
            estimation_method="linear_interpolation", max_gap=3,
            min_age_groups=None, diseases=None, sexes=None):
        """执行一次（增量）计算。"""
        years = sorted(years)
        sexes = sexes or ("MALE", "FEMALE")
        ages = tuple(min_age_groups or AGE_GROUPS)
        config = {
            "years": years,
            "sexes": list(sexes),
            "age_groups": list(ages),
            "countries": sorted(countries) if countries else None,
            "diseases": sorted(diseases) if diseases else None,
            "estimation_method": estimation_method,
            "max_gap": max_gap,
        }
        parent = self._runs[self._order[-1]] if self._order else None
        run = Run(run_id, label, parent.run_id if parent else None, config,
                  len(self._order) + 1)

        indicator_snap = self._require_indicator(registry, run)
        geo = self._load_geo(registry)
        exposure = self._load_exposure(registry, geo, run)
        rr_table = self._load_rr(registry, run)
        deaths_table = self._load_mortality(registry, run)

        target_countries = set(countries or ()) | set(deaths_key[0] for deaths_key in deaths_table)
        target_countries |= {k[0] for k in exposure}
        target_diseases = set(diseases or ())
        target_diseases |= set(rr_table)
        target_diseases |= {key[2] for key in deaths_table}
        if parent:
            # 运行维度沿用上一轮：撤回的输入不能让单元"消失"，只能变 void
            if parent.config["countries"]:
                target_countries |= set(parent.config["countries"])
            if parent.config["diseases"]:
                target_diseases |= set(parent.config["diseases"])
        target_countries = sorted(target_countries)
        target_diseases = sorted(target_diseases)

        reused = recomputed = 0
        for country in target_countries:
            for year in years:
                for disease in target_diseases:
                    for sex in sexes:
                        for age in ages:
                            key = cell_key(country, year, sex, age, disease)
                            result = self._compute_cell(
                                key, registry, indicator_snap, geo, exposure,
                                rr_table, deaths_table, estimation_method, max_gap,
                            )
                            prior = parent.get(key) if parent else None
                            if prior and prior["fingerprint"] == result["fingerprint"]:
                                frozen = copy.deepcopy(prior)
                                frozen["first_run"] = prior.get("first_run", prior["run_id"])
                                frozen["reused_from"] = prior["run_id"]
                                frozen["run_id"] = run_id
                                reused += 1
                            else:
                                frozen = result
                                frozen["run_id"] = run_id
                                frozen["first_run"] = run_id
                                frozen["reused_from"] = None
                                recomputed += 1
                            run.cells[key] = frozen
        run.config["summary"] = {
            "cells": len(run.cells),
            "recomputed": recomputed,
            "reused": reused,
        }
        self._runs[run_id] = run
        self._order.append(run_id)
        return run

    # ------------------------------------------------------------------ #
    def _compute_cell(self, key, registry, indicator_snap, geo, exposure,
                      rr_table, deaths_table, method, max_gap):
        country, year, sex, age, disease = key
        deps = []
        if indicator_snap:
            deps.append(indicator_snap.snapshot_id)

        # 风险参数：先判定精确槽位是否处于未裁定冲突（回退槽位同理）
        rr_slot = self._find_rr_slot(registry, disease, sex, age)
        if rr_slot == "conflict":
            return self._nonnumeric(key, CELL_CONFLICT,
                                    "风险参数存在未裁定竞争声明", deps, method)
        base = deaths_table.get((country, year, disease, sex, age))
        if base is None:
            exp_status = registry.claim_status(
                (INDICATOR_SH, country, year, sex, age)
            )
            if exp_status == STATUS_PENDING_ADJUDICATION:
                return self._nonnumeric(key, CELL_CONFLICT,
                                        "暴露率存在未裁定竞争声明", deps, method)
            return self._nonnumeric(key, CELL_NO_INPUT,
                                    "缺少死亡基数", deps, method)
        deaths, deaths_ui, deaths_snap = base
        deps.append(deaths_snap)

        rr = self._resolve_rr(rr_table, disease, sex, age)
        if rr is None:
            # 记录导致作废的失效风险参数（如刚被撤回的 RR），保留追溯链
            withdrawn_rr = self._last_inactive_rr(registry, disease, sex, age)
            if withdrawn_rr:
                deps.append(withdrawn_rr)
            return self._nonnumeric(
                key, CELL_VOID, "风险参数缺失或已撤回", deps, method,
                extra={"mortality_base": deaths,
                       "invalidated_rr_snapshot": withdrawn_rr})
        rr_value, rr_ui, rr_snap = rr
        deps.append(rr_snap)

        mapping_snap = geo.get(country, (None, None))[1]
        if mapping_snap:
            deps.append(mapping_snap)

        # 目标年暴露槽位若未裁定冲突，即使可插值也不得产出数字
        if registry.claim_status(
            (INDICATOR_SH, country, year, sex, age)
        ) == STATUS_PENDING_ADJUDICATION:
            return self._nonnumeric(key, CELL_CONFLICT,
                                    "暴露率存在未裁定竞争声明", deps, method)

        series = exposure.get((country, sex, age), [])
        status, p, p_ui, prov = estimate_exposure(series, year, method, max_gap)
        if status == CELL_NO_INPUT:
            return self._nonnumeric(key, CELL_NO_INPUT,
                                    "无可用暴露观测或估算支撑", deps, method,
                                    extra={"rr": rr_value, "mortality_base": deaths})
        deps.extend(prov["support"])

        paf = FORMULA_REGISTRY["paf-v1"].fn(p, rr_value)
        paf_lo, paf_hi = FORMULA_REGISTRY["paf-v1"].ui_fn(
            p, p_ui, rr_value, rr_ui
        )
        deaths_lo, deaths_hi = (
            paf_lo * deaths_ui[0],
            paf_hi * deaths_ui[1],
        )
        attr = paf * deaths
        inputs = {
            "exposure_p": p,
            "exposure_ui": p_ui,
            "rr": rr_value,
            "rr_ui": rr_ui,
            "mortality_base": deaths,
            "mortality_ui": deaths_ui,
        }
        fingerprint = canonical_hash(
            {
                "deps": sorted(set(deps)),
                "pipeline": BURDEN_PIPELINE["id"],
                "method": method,
                "max_gap": max_gap,
                "inputs": inputs,
            }
        )
        return {
            "key": list(key),
            "country": country,
            "year": year,
            "sex": sex,
            "age_group": age,
            "disease": disease,
            "status": status,
            "value": round(attr, 6),
            "paf": round(paf, 8),
            "ui": [round(deaths_lo, 6), round(deaths_hi, 6)],
            "estimation": prov,
            "inputs": inputs,
            "dependency_snapshots": sorted(set(deps)),
            "formula_version": BURDEN_PIPELINE["id"],
            "formula_steps": list(BURDEN_PIPELINE["steps"]),
            "fingerprint": fingerprint,
        }

    def _nonnumeric(self, key, status, reason, deps, method, extra=None):
        country, year, sex, age, disease = key
        return {
            "key": list(key),
            "country": country,
            "year": year,
            "sex": sex,
            "age_group": age,
            "disease": disease,
            "status": status,
            "value": None,
            "paf": None,
            "ui": None,
            "estimation": {"method": "none" if status != CELL_NO_INPUT else method},
            "inputs": extra or {},
            "dependency_snapshots": sorted(set(deps)),
            "formula_version": BURDEN_PIPELINE["id"],
            "formula_steps": list(BURDEN_PIPELINE["steps"]),
            "fingerprint": canonical_hash(
                {"deps": sorted(set(deps)), "status": status, "reason": reason,
                 "pipeline": BURDEN_PIPELINE["id"], "extra": extra or {}}
            ),
            "reason": reason,
        }

    # ------------------------------------------------------------------ #
    # 证据装载
    # ------------------------------------------------------------------ #
    def _require_indicator(self, registry, run):
        snaps = registry.active_records(KIND_INDICATOR)
        snaps = [s for s in snaps if s.content.get("indicator_id") == INDICATOR_SH]
        if not snaps:
            run.diagnostics.append("缺少二手烟暴露指标定义")
            return None
        if len(snaps) > 1:
            run.diagnostics.append("存在多个指标定义，取最新版本")
        return snaps[-1]

    def _load_geo(self, registry):
        geo = {}
        for snap in registry.active_records(KIND_GEO_MAPPING):
            c = snap.content
            geo[c["to"]] = (c.get("from"), snap.snapshot_id)
        return geo

    def _load_exposure(self, registry, geo, run):
        """返回 (country, sex, age) -> 按年排序的观测序列。"""
        table = {}
        valid_countries = set(geo)
        for kind in (KIND_SURVEY, KIND_LITERATURE):
            for claim in registry.active_claims(kind):
                slot = claim.slot
                # 槽位：(indicator, country, year, sex, age)
                if len(slot) != 5 or slot[0] != INDICATOR_SH:
                    continue
                _, country, yr, sex, age = slot
                if country not in valid_countries:
                    run.diagnostics.append(
                        f"暴露记录 {claim.snapshot_id} 的地区 {country} 无映射，已排除"
                    )
                    continue
                ui = claim.uncertainty or [claim.value, claim.value]
                table.setdefault((country, sex, age), []).append(
                    (int(yr), float(claim.value), [float(ui[0]), float(ui[1])],
                     claim.snapshot_id, claim.extra.get("sample_n"))
                )
        for series in table.values():
            series.sort(key=lambda pt: pt[0])
        return table

    def _load_rr(self, registry, run):
        """按疾病、性别和年龄整理风险值、区间与证据快照。"""
        table = {}
        for claim in registry.active_claims(KIND_RISK_PARAM):
            slot = claim.slot
            # 槽位：("RR", indicator, disease, sex, age)
            if len(slot) != 5 or slot[0] != "RR" or slot[1] != INDICATOR_SH:
                continue
            _, _, disease, sex, age = slot
            ui = claim.uncertainty or [claim.value, claim.value]
            table.setdefault(disease, {}).setdefault(sex, {})[age] = (
                float(claim.value),
                [float(ui[0]), float(ui[1])],
                claim.snapshot_id,
            )
        return table

    def _find_rr_slot(self, registry, disease, sex, age):
        """按 RR 回退顺序探测槽位：命中有效值/冲突/全部缺失。"""
        for sx, ag in ((sex, age), (sex, "ALL"), ("ALL", "ALL")):
            status = registry.claim_status(("RR", INDICATOR_SH, disease, sx, ag))
            if status == STATUS_PENDING_ADJUDICATION:
                return "conflict"
            if status == STATUS_COMPUTABLE:
                return "resolved"
        return "absent"

    def _last_inactive_rr(self, registry, disease, sex, age):
        """在回退槽位上找最近失效（撤回/替代）的 RR 快照 id，供 void 追溯。"""
        for sx, ag in ((sex, age), (sex, "ALL"), ("ALL", "ALL")):
            inactive = [
                c
                for c in registry.slot_claims(
                    ("RR", INDICATOR_SH, disease, sx, ag),
                    include_inactive=True,
                )
                if not c.active
            ]
            if inactive:
                return sorted(inactive, key=lambda c: c.seq)[-1].snapshot_id
        return None

    def _resolve_rr(self, table, disease, sex, age):
        """RR 槽位回退顺序：精确(性别,年龄) → 性别,ALL → ALL,ALL。"""
        for sx, ag in ((sex, age), (sex, "ALL"), ("ALL", "ALL")):
            hit = table.get(disease, {}).get(sx, {}).get(ag)
            if hit:
                return hit
        return None

    def _load_mortality(self, registry, run):
        table = {}
        for claim in registry.active_claims(KIND_MORTALITY):
            slot = claim.slot
            # 槽位：("DEATHS", country, year, disease, sex, age)
            if len(slot) != 6 or slot[0] != "DEATHS":
                continue
            _, country, yr, disease, sex, age = slot
            ui = claim.uncertainty or [claim.value, claim.value]
            table[(country, int(yr), disease, sex, age)] = (
                float(claim.value),
                [float(ui[0]), float(ui[1])],
                claim.snapshot_id,
            )
        return table

    # ------------------------------------------------------------------ #
    def get_run(self, run_id):
        return self._runs[run_id]

    def latest_run(self):
        return self._runs[self._order[-1]] if self._order else None

    def reproduce_cell(self, cell):
        """用冻结输入与公式版本重算单元，校验与存储值一致（原样复现）。"""
        if cell["status"] not in (CELL_OBSERVED, CELL_ESTIMATED):
            return {"matches": True, "recomputed": None, "stored": None}
        inp = cell["inputs"]
        paf = FORMULA_REGISTRY["paf-v1"].fn(inp["exposure_p"], inp["rr"])
        value = FORMULA_REGISTRY["attributable_deaths-v1"].fn(
            paf, inp["mortality_base"]
        )
        return {
            "matches": round(value, 6) == cell["value"],
            "recomputed": round(value, 6),
            "stored": cell["value"],
            "formula_version": cell["formula_version"],
        }
