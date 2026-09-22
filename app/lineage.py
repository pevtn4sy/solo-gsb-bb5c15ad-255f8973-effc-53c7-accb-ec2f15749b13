"""谱系追溯：按样品回溯完整来源 / 去向，并提供余量校验。"""
from __future__ import annotations

import sqlite3

from .config import EPSILON
from .services import ConflictError, _err


def _fetch_samples(conn: sqlite3.Connection, codes: list[str]) -> list[dict]:
    if not codes:
        return []
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, initial_volume, remaining, unit "
        f"FROM samples WHERE code IN ({placeholders})",
        codes,
    ).fetchall()
    return [dict(r) for r in rows]


def _reachable(conn: sqlite3.Connection, center: str, upstream: bool) -> set[str]:
    """递归 CTE 求可达节点集合（upstream=True 取祖先，否则取后代）。"""
    if upstream:
        sql = """
        WITH RECURSIVE reach(node) AS (
            SELECT parent_sample FROM lineages WHERE child_sample = :center
            UNION
            SELECT l.parent_sample FROM lineages l
            JOIN reach r ON l.child_sample = r.node
        )
        SELECT node FROM reach
        """
    else:
        sql = """
        WITH RECURSIVE reach(node) AS (
            SELECT child_sample FROM lineages WHERE parent_sample = :center
            UNION
            SELECT l.child_sample FROM lineages l
            JOIN reach r ON l.parent_sample = r.node
        )
        SELECT node FROM reach
        """
    return {r["node"] for r in conn.execute(sql, {"center": center})}


def _edges_within(conn: sqlite3.Connection, nodes: set[str]) -> list[dict]:
    """节点集合内部的全部谱系边。"""
    if not nodes:
        return []
    placeholders = ",".join("?" * len(nodes))
    rows = conn.execute(
        f"SELECT parent_sample, child_sample, consumed_volume, op_no "
        f"FROM lineages WHERE parent_sample IN ({placeholders}) "
        f"AND child_sample IN ({placeholders})",
        [*nodes, *nodes],
    ).fetchall()
    return [{
        "parent": r["parent_sample"],
        "child": r["child_sample"],
        "consumed_volume": r["consumed_volume"],
        "op_no": r["op_no"],
    } for r in rows]


def _all_paths(conn: sqlite3.Connection, center: str,
               upstream: bool) -> list[list[str]]:
    """递归 CTE 枚举到达 / 出发的完整路径（用分隔字符串拼接）。

    环路在数据层已被禁止，DAG 上路径数有限；对极度发散的图 SQL 中以
    length(path) < 10000 兜底，防止异常数据导致膨胀。
    """
    if upstream:
        sql = """
        WITH RECURSIVE walk(node, path) AS (
            SELECT parent_sample, parent_sample || char(31) || :center
            FROM lineages WHERE child_sample = :center
            UNION ALL
            SELECT l.parent_sample, l.parent_sample || char(31) || w.path
            FROM lineages l JOIN walk w ON l.child_sample = w.node
            WHERE length(w.path) < 10000
        )
        SELECT path FROM walk w
        WHERE NOT EXISTS (SELECT 1 FROM lineages l WHERE l.child_sample = w.node)
        """
    else:
        sql = """
        WITH RECURSIVE walk(node, path) AS (
            SELECT child_sample, :center || char(31) || child_sample
            FROM lineages WHERE parent_sample = :center
            UNION ALL
            SELECT l.child_sample, w.path || char(31) || l.child_sample
            FROM lineages l JOIN walk w ON l.parent_sample = w.node
            WHERE length(w.path) < 10000
        )
        SELECT path FROM walk w
        WHERE NOT EXISTS (SELECT 1 FROM lineages l WHERE l.parent_sample = w.node)
        """
    rows = conn.execute(sql, {"center": center}).fetchall()
    return [r["path"].split(chr(31)) for r in rows]


def trace_lineage(conn: sqlite3.Connection, code: str,
                  direction: str = "both") -> dict:
    """查询样品的完整来源（祖先）与去向（后代）谱系。"""
    exists = conn.execute("SELECT 1 FROM samples WHERE code=?", (code,)).fetchone()
    if not exists:
        raise ConflictError([_err(
            "sample_not_found", sample_code=code,
            message=f"样品 {code} 不存在",
        )])

    upstream = _reachable(conn, code, True)
    downstream = _reachable(conn, code, False)

    if direction == "sources":
        nodes = upstream
        edges = _edges_within(conn, upstream | {code})
        roots = [n for n in upstream
                 if not conn.execute(
                     "SELECT 1 FROM lineages WHERE child_sample=? LIMIT 1", (n,)
                 ).fetchone()]
        leaves = [code] if upstream else []
        paths = _all_paths(conn, code, True)
    elif direction == "destinations":
        nodes = downstream
        edges = _edges_within(conn, downstream | {code})
        roots = [code] if downstream else []
        leaves = [n for n in downstream
                  if not conn.execute(
                      "SELECT 1 FROM lineages WHERE parent_sample=? LIMIT 1", (n,)
                  ).fetchone()]
        paths = _all_paths(conn, code, False)
    else:
        nodes = upstream | downstream
        edges = _edges_within(conn, upstream | downstream | {code})
        roots = [n for n in (upstream | {code})
                 if not conn.execute(
                     "SELECT 1 FROM lineages WHERE child_sample=? LIMIT 1", (n,)
                 ).fetchone()]
        leaves = [n for n in (downstream | {code})
                  if not conn.execute(
                      "SELECT 1 FROM lineages WHERE parent_sample=? LIMIT 1", (n,)
                  ).fetchone()]
        paths = _all_paths(conn, code, True) + _all_paths(conn, code, False)

    samples = _fetch_samples(conn, list(nodes | {code}))
    sample_by_code = {s["code"]: s for s in samples}
    node_list = [
        {
            "code": s["code"],
            "remaining": s["remaining"],
            "initial_volume": s["initial_volume"],
            "unit": s["unit"],
        }
        for s in sorted(sample_by_code.values(), key=lambda x: x["code"])
    ]
    # 去重边（递归结果可能重复收集）
    seen, uniq_edges = set(), []
    for e in edges:
        key = (e["parent"], e["child"], e["op_no"])
        if key not in seen:
            seen.add(key)
            uniq_edges.append(e)

    uniq_paths, path_seen = [], set()
    for p in paths:
        key = tuple(p)
        if key not in path_seen:
            path_seen.add(key)
            uniq_paths.append(p)

    return {
        "direction": direction,
        "center": code,
        "nodes": node_list,
        "edges": uniq_edges,
        "roots": sorted(set(roots)),
        "leaves": sorted(set(leaves)),
        "paths": uniq_paths,
    }


def check_remaining(conn: sqlite3.Connection, code: str,
                    requested: float | None = None) -> dict:
    """余量校验：查询当前余量，可选校验是否够分出 requested。"""
    row = conn.execute(
        "SELECT code, remaining, unit FROM samples WHERE code=?", (code,)
    ).fetchone()
    if not row:
        raise ConflictError([_err(
            "sample_not_found", sample_code=code,
            message=f"样品 {code} 不存在",
        )])
    sufficient = True
    if requested is not None and requested > row["remaining"] + EPSILON:
        sufficient = False
    return {
        "code": row["code"],
        "remaining": round(row["remaining"], 9),
        "unit": row["unit"],
        "sufficient": sufficient,
        "requested": requested,
    }
