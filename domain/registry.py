"""证据登记层：文献、调查、指标定义、地区映射、风险参数与死亡基数。

核心约束：

* **只追加**：原始数据更正永不覆盖，只产生新的版本快照，旧快照保留；
* **重复合并**：同一来源（``kind + source_id``）且内容哈希一致的重复导入
  合并为同一快照，不产生新版本；
* **竞争保留**：不同来源对同一测量槽给出不一致取值时，双方都保留为
  竞争声明（``待裁定``），裁定前不可用于计算；
* **撤回不删除**：撤回只把最新快照置为 ``已失效``，旧报告仍可追溯它。

登记层不做计算，只负责证据身份、版本、状态与声明级裁定。
"""

import copy
import hashlib
import json
from dataclasses import dataclass, field

from .terms import (
    KIND_LITERATURE,
    KIND_MORTALITY,
    KIND_RISK_PARAM,
    KIND_SURVEY,
    STATUS_COMPUTABLE,
    STATUS_PENDING_ADJUDICATION,
    STATUS_SUPERSEDED,
    STATUS_WITHDRAWN,
)

# 携带"测量值"、参与槽位竞争的证据种类
MEASUREMENT_KINDS = frozenset(
    {KIND_SURVEY, KIND_LITERATURE, KIND_RISK_PARAM, KIND_MORTALITY}
)

CLAIM_COMPETING = "competing"
CLAIM_SELECTED = "selected"
CLAIM_REJECTED = "rejected"


def canonical_hash(obj):
    """对任意 JSON 兼容内容计算稳定哈希（字典键排序）。"""
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_slot(slot):
    """把测量槽位归一化为可比较的元组（接受元组/列表/字典）。"""
    if slot is None:
        return None
    if isinstance(slot, dict):
        return ("__dict__",) + tuple(
            (str(k), _slot_scalar(slot[k])) for k in sorted(slot)
        )
    return tuple(_slot_scalar(x) for x in slot)


def _slot_scalar(value):
    if isinstance(value, (dict, list)):
        return canonical_hash(value)
    return value


@dataclass
class Snapshot:
    """一条证据记录的某个不可变版本。"""

    record_id: str
    kind: str
    source_id: str
    version: int
    content: dict
    content_hash: str
    provenance: dict
    status: str = STATUS_COMPUTABLE
    supersedes: str = None

    @property
    def snapshot_id(self):
        return f"{self.record_id}@v{self.version}"

    def to_dict(self):
        return {
            "snapshot_id": self.snapshot_id,
            "record_id": self.record_id,
            "kind": self.kind,
            "source_id": self.source_id,
            "version": self.version,
            "content": copy.deepcopy(self.content),
            "content_hash": self.content_hash,
            "provenance": copy.deepcopy(self.provenance),
            "status": self.status,
            "supersedes": self.supersedes,
        }


@dataclass
class Claim:
    """一条测量快照在某个测量槽位上的声明。"""

    slot: tuple
    value: object
    uncertainty: object
    snapshot_id: str
    record_id: str
    kind: str
    seq: int
    status: str = CLAIM_COMPETING
    active: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class Event:
    """登记层事件日志（导入/合并/更正/裁定/撤回），全部只追加。"""

    seq: int
    action: str
    detail: dict = field(default_factory=dict)


class ConflictError(ValueError):
    """槽位不存在可裁定冲突，或裁定目标不合法。"""


class Registry:
    """内存型证据登记处。"""

    def __init__(self):
        self._records = {}          # record_id -> [Snapshot...]
        self._snapshots = {}        # snapshot_id -> Snapshot
        self._claims = {}           # 归一化槽位 -> [Claim...]
        self._adjudications = {}    # 槽位 -> 裁定信息
        self._events = []
        self._counter = 0

    # ------------------------------------------------------------------ #
    # 导入
    # ------------------------------------------------------------------ #
    def import_record(self, kind, source_id, content, provenance=None):
        """导入一条证据，返回 ``(snapshot, changed)``。

        * 内容哈希与最新版本相同 → 合并，``changed=False``，不产生新版本；
        * 同一来源内容变化 → 追加更正快照，旧版本置 ``已替代``；
        * 新来源 → 建立首版快照。
        """
        if not source_id:
            raise ValueError("source_id 不能为空")
        content = copy.deepcopy(content)
        provenance = copy.deepcopy(provenance) or {}
        record_id = f"{kind}:{source_id}"
        digest = canonical_hash(
            {"kind": kind, "source_id": source_id, "content": content}
        )
        history = self._records.get(record_id)
        if history and history[-1].content_hash == digest:
            snap = history[-1]
            self._log("merge", {"snapshot_id": snap.snapshot_id})
            return snap, False

        version = len(history) + 1 if history else 1
        snap = Snapshot(
            record_id=record_id,
            kind=kind,
            source_id=source_id,
            version=version,
            content=content,
            content_hash=digest,
            provenance={**provenance, "import_seq": self._tick()},
            supersedes=history[-1].snapshot_id if history else None,
        )
        self._records.setdefault(record_id, []).append(snap)
        self._snapshots[snap.snapshot_id] = snap
        if history:
            old = history[-2]
            old.status = STATUS_SUPERSEDED
            self._deactivate_claims(old.snapshot_id)
        if kind in MEASUREMENT_KINDS:
            self._index_claims(snap)
        self._log(
            "correction" if version > 1 else "import",
            {"snapshot_id": snap.snapshot_id, "kind": kind, "version": version},
        )
        return snap, True

    def _index_claims(self, snap):
        payload = snap.content
        items = payload.get("slots")
        if items is None and payload.get("slot") is not None:
            items = [
                {
                    "slot": payload["slot"],
                    "value": payload.get("value"),
                    "uncertainty": payload.get("uncertainty"),
                    "extra": payload.get("extra")
                    or {"sample_n": (payload.get("sampling") or {}).get("sample_n")},
                }
            ]
        if not items:
            return
        for item in items:
            self._claims.setdefault(normalize_slot(item["slot"]), []).append(
                Claim(
                    slot=normalize_slot(item["slot"]),
                    value=item.get("value"),
                    uncertainty=copy.deepcopy(item.get("uncertainty")),
                    snapshot_id=snap.snapshot_id,
                    record_id=snap.record_id,
                    kind=snap.kind,
                    seq=self._counter,
                    extra=copy.deepcopy(item.get("extra") or {}),
                )
            )
        for item in items:
            self._refresh_slot(normalize_slot(item["slot"]))

    def _deactivate_claims(self, snapshot_id):
        touched = set()
        for slot, claims in self._claims.items():
            for claim in claims:
                if claim.snapshot_id == snapshot_id and claim.active:
                    claim.active = False
                    touched.add(slot)
        for slot in touched:
            self._refresh_slot(slot)

    def _refresh_slot(self, slot):
        active = [c for c in self._claims.get(slot, []) if c.active]
        for c in active:
            c.status = CLAIM_COMPETING
        if not active:
            self._adjudications.pop(slot, None)
            return
        distinct = {canonical_hash((c.value, c.uncertainty)) for c in active}
        winner_id = self._adjudications.get(slot, {}).get("winner")
        if len(distinct) == 1 or winner_id not in {c.snapshot_id for c in active}:
            # 无冲突，或原获胜方已失效：取消陈旧裁定
            if winner_id is not None and winner_id not in {
                c.snapshot_id for c in active
            }:
                self._adjudications.pop(slot, None)
            if len(distinct) == 1:
                # 取值一致：最早导入的声明作为代表（多来源一致，非竞争）
                chosen = min(active, key=lambda c: (c.seq, c.snapshot_id))
                chosen.status = CLAIM_SELECTED
            return
        # 存在冲突且有有效裁定
        for c in active:
            c.status = (
                CLAIM_SELECTED if c.snapshot_id == winner_id else CLAIM_REJECTED
            )

    # ------------------------------------------------------------------ #
    # 裁定
    # ------------------------------------------------------------------ #
    def adjudicate(self, slot, winner_snapshot_id, reason, by):
        """在竞争声明中裁定获胜快照；落败声明保留为 REJECTED（可追溯）。"""
        slot = normalize_slot(slot)
        active = [c for c in self._claims.get(slot, []) if c.active]
        candidates = {c.snapshot_id for c in active}
        if winner_snapshot_id not in candidates:
            raise ConflictError(
                f"裁定目标 {winner_snapshot_id} 不在槽位 {slot} 的有效声明中"
            )
        if len({canonical_hash((c.value, c.uncertainty)) for c in active}) <= 1:
            raise ConflictError(f"槽位 {slot} 不存在取值冲突，无需裁定")
        for c in active:
            c.status = (
                CLAIM_SELECTED
                if c.snapshot_id == winner_snapshot_id
                else CLAIM_REJECTED
            )
        self._adjudications[slot] = {
            "winner": winner_snapshot_id,
            "reason": reason,
            "by": by,
            "seq": self._tick(),
        }
        self._log(
            "adjudicate",
            {"slot": list(slot), "winner": winner_snapshot_id, "reason": reason},
        )

    def pending_conflicts(self):
        """返回所有取值冲突且尚未裁定的槽位。"""
        out = []
        for slot, claims in self._claims.items():
            active = [c for c in claims if c.active]
            distinct = {canonical_hash((c.value, c.uncertainty)) for c in active}
            if len(distinct) > 1 and slot not in self._adjudications:
                out.append(
                    {
                        "slot": list(slot),
                        "claims": [
                            {
                                "snapshot_id": c.snapshot_id,
                                "value": c.value,
                                "kind": c.kind,
                            }
                            for c in active
                        ],
                    }
                )
        return out

    # ------------------------------------------------------------------ #
    # 撤回（不删除）
    # ------------------------------------------------------------------ #
    def withdraw(self, record_id=None, *, kind=None, source_id=None, reason="", by=""):
        """撤回一条证据记录的当前版本；快照保留，状态置 ``已失效``。"""
        record_id = record_id or f"{kind}:{source_id}"
        history = self._records.get(record_id)
        if not history:
            raise KeyError(f"未知记录 {record_id}")
        snap = history[-1]
        if snap.status == STATUS_WITHDRAWN:
            return snap
        snap.status = STATUS_WITHDRAWN
        self._deactivate_claims(snap.snapshot_id)
        self._log("withdraw", {"snapshot_id": snap.snapshot_id, "reason": reason,
                               "by": by})
        return snap

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, snapshot_id):
        return self._snapshots[snapshot_id]

    def history(self, record_id=None, *, kind=None, source_id=None):
        record_id = record_id or f"{kind}:{source_id}"
        return list(self._records.get(record_id, []))

    def active_records(self, kind):
        """某种类当前有效（最新版未撤回）的快照。"""
        return [
            hist[-1]
            for hist in self._records.values()
            if hist[-1].kind == kind and hist[-1].status != STATUS_WITHDRAWN
        ]

    def resolve_claim(self, slot):
        """返回槽位当前获胜声明。

        * 冲突未裁定 → ``None``；
        * 无任何有效声明 → ``"absent"``（供引擎区分缺失与冲突）。
        """
        slot = normalize_slot(slot)
        claims = [c for c in self._claims.get(slot, []) if c.active]
        if not claims:
            return "absent"
        distinct = {canonical_hash((c.value, c.uncertainty)) for c in claims}
        if len(distinct) > 1 and slot not in self._adjudications:
            return None
        selected = [c for c in claims if c.status == CLAIM_SELECTED]
        pool = selected or claims
        return sorted(pool, key=lambda c: (c.seq, c.snapshot_id))[0]

    def active_claims(self, kind):
        """枚举某种类当前有效声明（含被裁定拒绝的不含）。

        裁定未决槽位的声明不在其中——竞争声明通过 :meth:`pending_conflicts`
        暴露，避免未裁定数据被当作观测值使用。
        """
        out = []
        for slot, claims in self._claims.items():
            distinct = {
                canonical_hash((c.value, c.uncertainty))
                for c in claims
                if c.active
            }
            unresolved = len(distinct) > 1 and slot not in self._adjudications
            for c in claims:
                if c.active and c.status == CLAIM_SELECTED and not unresolved:
                    out.append(c)
        return [c for c in out if c.kind == kind]

    def slot_claims(self, slot, include_inactive=False):
        """返回槽位声明；include_inactive 时含已替代/撤回的历史声明。"""
        slot = normalize_slot(slot)
        claims = list(self._claims.get(slot, []))
        if include_inactive:
            return claims
        return [c for c in claims if c.active]

    def claim_status(self, slot):
        slot = normalize_slot(slot)
        claims = [c for c in self._claims.get(slot, []) if c.active]
        if not claims:
            return "absent"
        distinct = {canonical_hash((c.value, c.uncertainty)) for c in claims}
        if len(distinct) > 1 and slot not in self._adjudications:
            return STATUS_PENDING_ADJUDICATION
        return STATUS_COMPUTABLE

    def events(self):
        return [
            {"seq": e.seq, "action": e.action, "detail": copy.deepcopy(e.detail)}
            for e in self._events
        ]

    def all_snapshots(self):
        return list(self._snapshots.values())

    # ------------------------------------------------------------------ #
    def _tick(self):
        self._counter += 1
        return self._counter

    def _log(self, action, detail):
        self._events.append(
            Event(seq=len(self._events) + 1, action=action, detail=detail)
        )
