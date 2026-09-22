"""核心业务引擎：事务校验、余量扣减、环路检测、幂等与批量处理。

本模块只依赖标准库与 SQLite，便于独立单元测试；HTTP 层见 ``app.main``。

批量操作在单个 ``BEGIN IMMEDIATE`` 事务中完成：
1. 先模拟整批操作（含批内新样品 / 余量变化 / 新增边），收集全部冲突；
2. 无冲突才顺序写入。任一批量整体失败会回滚，已提交的操作不受影响。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from .config import EPSILON

# ---------------------------------------------------------------- 错误类型
CONFLICT_REASONS = {
    "op_no_conflict",          # 操作号已存在但载荷不同
    "sample_already_exists",   # 目标样品编号已存在
    "parent_not_found",        # 父样品不存在
    "insufficient_volume",     # 父样品余量不足
    "unit_mismatch",           # 单位不一致
    "cycle_detected",          # 会形成谱系环路
    "duplicate_op_no",         # 同一批内操作号重复
    "duplicate_output",        # 同一批内输出样品编号重复
}


class ConflictError(Exception):
    """409 冲突：携带冲突操作及定位信息。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(json.dumps(errors, ensure_ascii=False))


def _err(reason: str, *, index: int | None = None, op_no: str | None = None,
         sample_code: str | None = None, message: str, **extra) -> dict:
    e = {
        "reason": reason,
        "index": index,
        "op_no": op_no,
        "sample_code": sample_code,
        "message": message,
    }
    e.update(extra)
    return {k: v for k, v in e.items() if v is not None}


def canonical_hash(op) -> str:
    """对操作载荷做规范化哈希，用于幂等比对（忽略 note 以外的键序差异）。"""
    import hashlib

    payload = op.model_dump()
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _round_vol(v: float) -> float:
    return round(float(v), 9)


# ---------------------------------------------------------------- 模拟状态
@dataclass
class SimState:
    """批内模拟状态：叠加 DB 快照与本批的内存变更。"""

    conn: sqlite3.Connection
    sample_remaining: dict[str, float] = field(default_factory=dict)
    sample_unit: dict[str, float] = field(default_factory=dict)
    known_new: set[str] = field(default_factory=set)
    edges: list[tuple[str, str]] = field(default_factory=list)
    descendants_cache: dict[str, set[str]] = field(default_factory=dict)

    def __post_init__(self):
        rows = self.conn.execute(
            "SELECT code, remaining, unit FROM samples"
        ).fetchall()
        for r in rows:
            self.sample_remaining[r["code"]] = r["remaining"]
            self.sample_unit[r["code"]] = r["unit"]
        for r in self.conn.execute(
            "SELECT parent_sample, child_sample FROM lineages"
        ):
            self.edges.append((r["parent_sample"], r["child_sample"]))

    def exists(self, code: str) -> bool:
        return code in self.sample_remaining

    def register_new(self, code: str, volume: float, unit: str) -> None:
        self.sample_remaining[code] = volume
        self.sample_unit[code] = unit
        self.known_new.add(code)
        self.descendants_cache.clear()

    # -- 环路检测：在 (DB边 ∪ 本批边) 构成的图上，若 parent 是 child 的后代则成环
    def descendants(self, code: str) -> set[str]:
        """返回 code 的全部后代（含传递闭包），迭代实现避免深递归。"""
        cached = self.descendants_cache.get(code)
        if cached is not None:
            return cached
        out: dict[str, list[str]] = {}
        for p, c in self.edges:
            out.setdefault(p, []).append(c)
        seen: set[str] = set()
        stack = list(out.get(code, ()))
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(out.get(n, ()))
        self.descendants_cache[code] = seen
        return seen

    def would_cycle(self, parent: str, child: str) -> bool:
        if parent == child:
            return True
        return parent in self.descendants(child)

    def cycle_path(self, parent: str, child: str) -> list[str]:
        """构造定位用闭合环路 parent -> child -> ... -> parent（BFS 最短路）。

        新边为 parent -> child；要成环，图上需已有 child 可达 parent 的路径。
        """
        if parent == child:
            return [parent, child]
        out: dict[str, list[str]] = {}
        for p, c in self.edges:
            out.setdefault(p, []).append(c)
        prev: dict[str, str] = {child: ""}
        queue = [child]
        while queue:
            n = queue.pop(0)
            if n == parent:
                break
            for nxt in out.get(n, ()):
                if nxt not in prev:
                    prev[nxt] = n
                    queue.append(nxt)
        if parent not in prev:
            return [parent, child, parent]
        tail = [parent]
        cur = parent
        while prev[cur]:
            cur = prev[cur]
            tail.append(cur)
        tail.reverse()  # child -> ... -> parent
        return [parent] + tail


# ---------------------------------------------------------------- 幂等读取
def _load_record(conn: sqlite3.Connection, op_no: str) -> dict | None:
    row = conn.execute(
        "SELECT op_no, op_type, payload, result, created_at "
        "FROM operations WHERE op_no=?",
        (op_no,),
    ).fetchone()
    if row is None:
        return None
    return {
        "op_no": row["op_no"],
        "op_type": row["op_type"],
        "payload": json.loads(row["payload"]),
        "result": json.loads(row["result"]),
        "replayed": True,
        "created_at": row["created_at"],
    }


def _record_to_response(rec: dict) -> dict:
    return rec


# ---------------------------------------------------------------- 单操作校验（模拟阶段）
def _validate_one(op, index: int, state: SimState,
                  op_seen: set[str], output_seen: set[str]) -> dict | None:
    """校验一个操作并推进模拟状态；成功返回结果 dict，失败返回 error dict。"""
    no = op.op_no
    if no in op_seen:
        return _err("duplicate_op_no", index=index, op_no=no,
                    message=f"操作号 {no} 在同一批次中重复出现")
    op_seen.add(no)

    t = op.op_type

    if t == "sample":
        code = op.sample_code
        if state.exists(code):
            return _err("sample_already_exists", index=index, op_no=no,
                        sample_code=code,
                        message=f"样品 {code} 已存在，取样操作不能重复登记")
        if code in output_seen:
            return _err("duplicate_output", index=index, op_no=no,
                        sample_code=code,
                        message=f"样品 {code} 在同一批次中被多个操作创建")
        output_seen.add(code)
        state.register_new(code, op.volume, op.unit)
        return {
            "sample_code": code,
            "initial_volume": _round_vol(op.volume),
            "remaining": _round_vol(op.volume),
            "unit": op.unit,
        }

    if t == "aliquot":
        parent, child = op.parent_code, op.child_code
        if not state.exists(parent):
            return _err("parent_not_found", index=index, op_no=no,
                        sample_code=parent,
                        message=f"父样品 {parent} 不存在")
        unit = state.sample_unit[parent]
        if op.unit != unit:
            return _err("unit_mismatch", index=index, op_no=no,
                        sample_code=parent,
                        message=f"父样品单位为 {unit}，请求单位为 {op.unit}",
                        expected_unit=unit, provided_unit=op.unit)
        rem = state.sample_remaining[parent]
        if op.volume > rem + EPSILON:
            return _err("insufficient_volume", index=index, op_no=no,
                        sample_code=parent,
                        message=(f"父样品 {parent} 余量 {_round_vol(rem)} {unit} "
                                 f"不足，需分装 {_round_vol(op.volume)}"),
                        remaining=_round_vol(rem),
                        requested=_round_vol(op.volume))
        if state.would_cycle(parent, child):
            path = state.cycle_path(parent, child)
            return _err("cycle_detected", index=index, op_no=no,
                        sample_code=child,
                        message="该分装会形成谱系环路（样品不能既是后代又是祖先）",
                        cycle_path=path)
        # 输出样品：必须是本批新建或库中不存在，且不能把新样品再输出到已存在样品
        if state.exists(child) and child not in state.known_new:
            return _err("sample_already_exists", index=index, op_no=no,
                        sample_code=child,
                        message=f"子样品 {child} 已存在，分装输出必须为新样品")
        if child in output_seen and child not in state.known_new:
            return _err("duplicate_output", index=index, op_no=no,
                        sample_code=child,
                        message=f"样品 {child} 在同一批次中被多个操作创建")
        output_seen.add(child)

        new_parent_rem = _round_vol(rem - op.volume)
        if child in state.known_new:
            # 批内刚创建的样品继续累积余量
            child_rem = _round_vol(state.sample_remaining[child] + op.volume)
        else:
            child_rem = _round_vol(op.volume)
        state.sample_remaining[parent] = new_parent_rem
        state.sample_remaining[child] = child_rem
        if child not in state.known_new:
            state.sample_unit[child] = op.unit
            state.known_new.add(child)
        state.edges.append((parent, child))
        state.descendants_cache.clear()
        return {
            "parent_code": parent,
            "parent_remaining": new_parent_rem,
            "child_code": child,
            "child_remaining": child_rem,
            "consumed": _round_vol(op.volume),
            "unit": op.unit,
        }

    # merge
    parents = list(op.parent_codes)
    volumes = list(op.volumes) if op.volumes is not None else None
    for p in parents:
        if not state.exists(p):
            return _err("parent_not_found", index=index, op_no=no,
                        sample_code=p, message=f"父样品 {p} 不存在")
    parent_units = {state.sample_unit[p] for p in parents}
    if len(parent_units) > 1:
        return _err("unit_mismatch", index=index, op_no=no,
                    sample_code=parents[0],
                    message=f"参与合并的父样品单位不一致: {sorted(parent_units)}",
                    parent_units=sorted(parent_units))
    parent_unit = next(iter(parent_units))
    if op.unit != parent_unit:
        return _err("unit_mismatch", index=index, op_no=no,
                    sample_code=parents[0],
                    message=f"父样品单位为 {parent_unit}，请求单位为 {op.unit}",
                    expected_unit=parent_unit, provided_unit=op.unit)

    if volumes is not None:
        if len(volumes) != len(parents):
            return _err("invalid_request", index=index, op_no=no,
                        message="volumes 与 parent_codes 长度必须一致")
    else:
        volumes = [state.sample_remaining[p] for p in parents]

    for p, v in zip(parents, volumes):
        rem = state.sample_remaining[p]
        if v > rem + EPSILON:
            return _err("insufficient_volume", index=index, op_no=no,
                        sample_code=p,
                        message=(f"父样品 {p} 余量 {_round_vol(rem)} {parent_unit} "
                                 f"不足，需合并投入 {_round_vol(v)}"),
                        remaining=_round_vol(rem), requested=_round_vol(v))

    child = op.child_code
    # 任一父样品 -> child 成环即拒绝
    for p in parents:
        if state.would_cycle(p, child):
            path = state.cycle_path(p, child)
            return _err("cycle_detected", index=index, op_no=no,
                        sample_code=child,
                        message="该合并会形成谱系环路（样品不能既是后代又是祖先）",
                        cycle_path=path, parent_in_cycle=p)

    if state.exists(child) and child not in state.known_new:
        return _err("sample_already_exists", index=index, op_no=no,
                    sample_code=child,
                    message=f"子样品 {child} 已存在，合并输出必须为新样品")
    if child in output_seen and child not in state.known_new:
        return _err("duplicate_output", index=index, op_no=no,
                    sample_code=child,
                    message=f"样品 {child} 在同一批次中被多个操作创建")
    output_seen.add(child)

    items = []
    total = 0.0
    for p, v in zip(parents, volumes):
        rem = state.sample_remaining[p]
        new_rem = _round_vol(rem - v)
        state.sample_remaining[p] = new_rem
        total += v
        items.append({
            "parent_code": p,
            "parent_remaining": new_rem,
            "consumed": _round_vol(v),
        })
    total = _round_vol(total)
    if child in state.known_new:
        child_rem = _round_vol(state.sample_remaining[child] + total)
    else:
        child_rem = total
    state.sample_remaining[child] = child_rem
    if child not in state.known_new:
        state.sample_unit[child] = op.unit
        state.known_new.add(child)
    for p in parents:
        state.edges.append((p, child))
    state.descendants_cache.clear()
    return {
        "parents": items,
        "child_code": child,
        "child_remaining": child_rem,
        "total_volume": total,
        "unit": op.unit,
    }


# ---------------------------------------------------------------- 写入阶段
def _persist(conn: sqlite3.Connection, op, result: dict) -> dict:
    payload = op.model_dump()
    conn.execute(
        "INSERT INTO operations(op_no, op_type, payload, payload_hash, result) "
        "VALUES (?,?,?,?,?)",
        (op.op_no, op.op_type, json.dumps(payload, ensure_ascii=False),
         canonical_hash(op), json.dumps(result, ensure_ascii=False)),
    )
    t = op.op_type
    if t == "sample":
        conn.execute(
            "INSERT INTO samples(code, initial_volume, remaining, unit, created_by_op)"
            " VALUES (?,?,?,?,?)",
            (result["sample_code"], result["initial_volume"],
             result["remaining"], result["unit"], op.op_no),
        )
    elif t == "aliquot":
        conn.execute(
            "INSERT INTO samples(code, initial_volume, remaining, unit, created_by_op)"
            " VALUES (?,?,?,?,?)",
            (result["child_code"], result["consumed"],
             result["child_remaining"], result["unit"], op.op_no),
        )
        conn.execute(
            "UPDATE samples SET remaining=? WHERE code=?",
            (result["parent_remaining"], op.parent_code),
        )
        conn.execute(
            "INSERT INTO lineages(op_no, parent_sample, child_sample, consumed_volume)"
            " VALUES (?,?,?,?)",
            (op.op_no, op.parent_code, result["child_code"], result["consumed"]),
        )
    else:  # merge
        conn.execute(
            "INSERT INTO samples(code, initial_volume, remaining, unit, created_by_op)"
            " VALUES (?,?,?,?,?)",
            (result["child_code"], result["total_volume"],
             result["child_remaining"], result["unit"], op.op_no),
        )
        for item in result["parents"]:
            conn.execute(
                "UPDATE samples SET remaining=? WHERE code=?",
                (item["parent_remaining"], item["parent_code"]),
            )
            conn.execute(
                "INSERT INTO lineages(op_no, parent_sample, child_sample, consumed_volume)"
                " VALUES (?,?,?,?)",
                (op.op_no, item["parent_code"], result["child_code"],
                 item["consumed"]),
            )

    return {
        "op_no": op.op_no,
        "op_type": op.op_type,
        "payload": payload,
        "result": result,
        "replayed": False,
        "created_at": conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now') AS t"
        ).fetchone()["t"],
    }


# ---------------------------------------------------------------- 对外入口
def apply_batch(conn: sqlite3.Connection, operations: list, *,
                dry_run: bool = False) -> tuple[list[dict], list[dict]]:
    """在一个事务中执行一批操作。

    返回 ``(records, errors)``：
    - 成功（或 dry_run 通过）时 errors 为空，records 为每步结果；
    - 有冲突时 records 为空、errors 给出全部冲突（含 index/op_no/定位），
      事务回滚。
    对已存在的相同操作号直接幂等回放原结果；不同载荷记为 op_no_conflict。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = SimState(conn)
        op_seen: set[str] = set()
        output_seen: set[str] = set()
        planned: list[tuple[object, dict, bool]] = []  # (op, result, is_replay)
        errors: list[dict] = []

        for i, op in enumerate(operations):
            existing = conn.execute(
                "SELECT payload_hash FROM operations WHERE op_no=?",
                (op.op_no,),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != canonical_hash(op):
                    errors.append(_err(
                        "op_no_conflict", index=i, op_no=op.op_no,
                        message=(f"操作号 {op.op_no} 已存在但提交内容与原始记录不一致，"
                                 "幂等重试必须使用相同载荷"),
                    ))
                    continue
                rec = _load_record(conn, op.op_no)
                planned.append((op, rec, True))
                continue

            res = _validate_one(op, i, state, op_seen, output_seen)
            if isinstance(res, dict) and ("reason" in res):
                errors.append(res)
                continue
            planned.append((op, res, False))

        if errors:
            conn.rollback()
            return [], errors

        records: list[dict] = []
        if dry_run:
            for op, res_or_rec, is_replay in planned:
                if is_replay:
                    records.append(res_or_rec)
                else:
                    records.append({
                        "op_no": op.op_no,
                        "op_type": op.op_type,
                        "payload": op.model_dump(),
                        "result": res_or_rec,
                        "replayed": False,
                        "created_at": None,
                    })
            conn.rollback()
            return records, []

        for op, res_or_rec, is_replay in planned:
            if is_replay:
                records.append(_record_to_response(res_or_rec))
            else:
                records.append(_persist(conn, op, res_or_rec))
        conn.commit()
        return records, []
    except Exception:
        conn.rollback()
        raise


def apply_one(conn: sqlite3.Connection, op, *, dry_run: bool = False) -> dict:
    """单个操作入口；冲突时抛 :class:`ConflictError`。"""
    records, errors = apply_batch(conn, [op], dry_run=dry_run)
    if errors:
        raise ConflictError(errors)
    return records[0]
