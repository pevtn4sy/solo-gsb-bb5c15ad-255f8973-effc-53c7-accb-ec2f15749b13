"""端到端接口测试。"""
import pytest


# ----------------------------------------------------------------- 基本流程
def test_health_and_root(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["service"] == "sample-traceability"


def _sample(client, op_no, code, volume, unit="mL"):
    return client.post("/api/v1/operations", json={
        "op_type": "sample", "op_no": op_no,
        "sample_code": code, "volume": volume, "unit": unit,
    })


def test_sample_then_aliquot_chain_and_remaining(client):
    r = _sample(client, "OP-001", "S1", 100)
    assert r.status_code == 201
    assert r.json()["result"]["remaining"] == 100

    r = client.post("/api/v1/operations", json={
        "op_type": "aliquot", "op_no": "OP-002",
        "parent_code": "S1", "child_code": "A1",
        "volume": 30, "unit": "mL",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["result"]["parent_remaining"] == 70
    assert body["result"]["child_remaining"] == 30

    rem = client.get("/api/v1/samples/S1/remaining").json()
    assert rem["remaining"] == 70 and rem["sufficient"] is True

    rem = client.get("/api/v1/samples/S1/remaining?requested=71").json()
    assert rem["sufficient"] is False


def test_insufficient_volume_conflict_with_location(client):
    _sample(client, "OP-1", "S1", 10)
    r = client.post("/api/v1/operations", json={
        "op_type": "aliquot", "op_no": "OP-2",
        "parent_code": "S1", "child_code": "A1",
        "volume": 10.5, "unit": "mL",
    })
    assert r.status_code == 409
    err = r.json()["errors"][0]
    assert err["reason"] == "insufficient_volume"
    assert err["op_no"] == "OP-2"
    assert err["sample_code"] == "S1"
    assert err["remaining"] == 10
    assert err["requested"] > 10


def test_parent_not_found(client):
    r = client.post("/api/v1/operations", json={
        "op_type": "aliquot", "op_no": "OP-9",
        "parent_code": "GHOST", "child_code": "A1",
        "volume": 1, "unit": "mL",
    })
    assert r.status_code == 409
    assert r.json()["errors"][0]["reason"] == "parent_not_found"


def test_unit_mismatch(client):
    _sample(client, "OP-1", "S1", 100, unit="mL")
    r = client.post("/api/v1/operations", json={
        "op_type": "aliquot", "op_no": "OP-2",
        "parent_code": "S1", "child_code": "A1",
        "volume": 10, "unit": "uL",
    })
    assert r.status_code == 409
    assert r.json()["errors"][0]["reason"] == "unit_mismatch"


def test_duplicate_sample_code(client):
    _sample(client, "OP-1", "S1", 100)
    r = _sample(client, "OP-2", "S1", 50)
    assert r.status_code == 409
    assert r.json()["errors"][0]["reason"] == "sample_already_exists"


# ----------------------------------------------------------------- 幂等
def test_idempotent_replay_same_payload(client):
    payload = {"op_type": "sample", "op_no": "OP-IDEM",
               "sample_code": "S1", "volume": 42, "unit": "mL"}
    r1 = client.post("/api/v1/operations", json=payload)
    assert r1.status_code == 201 and r1.json()["replayed"] is False

    r2 = client.post("/api/v1/operations", json=payload)
    assert r2.status_code == 200
    body = r2.json()
    assert body["replayed"] is True
    assert body["result"] == r1.json()["result"]
    # 余量没有被二次增加
    assert client.get("/api/v1/samples/S1").json()["initial_volume"] == 42


def test_idempotent_same_op_no_different_payload_conflict(client):
    _sample(client, "OP-X", "S1", 100)
    r = client.post("/api/v1/operations", json={
        "op_type": "sample", "op_no": "OP-X",
        "sample_code": "S9", "volume": 5, "unit": "mL",
    })
    assert r.status_code == 409
    err = r.json()["errors"][0]
    assert err["reason"] == "op_no_conflict"
    assert err["op_no"] == "OP-X"


def test_get_operation_record(client):
    _sample(client, "OP-G", "S1", 10)
    r = client.get("/api/v1/operations/OP-G")
    assert r.status_code == 200
    assert r.json()["op_type"] == "sample"
    assert client.get("/api/v1/operations/NOPE").status_code == 404


# ----------------------------------------------------------------- 合并
def test_merge_flow(client):
    _sample(client, "OP-1", "S1", 100)
    _sample(client, "OP-2", "S2", 50)
    r = client.post("/api/v1/operations", json={
        "op_type": "merge", "op_no": "OP-M",
        "parent_codes": ["S1", "S2"],
        "volumes": [40, 50],
        "child_code": "M1", "unit": "mL",
    })
    assert r.status_code == 201
    res = r.json()["result"]
    assert res["total_volume"] == 90
    assert res["child_remaining"] == 90
    assert {p["parent_code"]: p["parent_remaining"] for p in res["parents"]} == {
        "S1": 60, "S2": 0
    }
    # 默认 volumes = 全部余量
    r2 = client.post("/api/v1/operations", json={
        "op_type": "merge", "op_no": "OP-M2",
        "parent_codes": ["S1", "M1"],
        "child_code": "M2", "unit": "mL",
    })
    assert r2.status_code == 201
    assert r2.json()["result"]["total_volume"] == 150


def test_merge_insufficient_and_unit_conflict(client):
    _sample(client, "OP-1", "S1", 10)
    _sample(client, "OP-2", "S2", 20, unit="g")
    r = client.post("/api/v1/operations", json={
        "op_type": "merge", "op_no": "OP-M",
        "parent_codes": ["S1", "S2"],
        "child_code": "M1", "unit": "mL",
    })
    assert r.status_code == 409
    assert r.json()["errors"][0]["reason"] == "unit_mismatch"

    r = client.post("/api/v1/operations", json={
        "op_type": "merge", "op_no": "OP-M",
        "parent_codes": ["S1", "S2"],
        "volumes": [11, 1],
        "child_code": "M1", "unit": "mL",
    })
    assert r.status_code == 409
    # S2 单位 g，会先触发单位冲突；这里换成同单位再测余量
    _sample(client, "OP-3", "S3", 5)
    r = client.post("/api/v1/operations", json={
        "op_type": "merge", "op_no": "OP-M",
        "parent_codes": ["S1", "S3"],
        "volumes": [11, 1],
        "child_code": "M1", "unit": "mL",
    })
    assert r.status_code == 409
    err = r.json()["errors"][0]
    assert err["reason"] == "insufficient_volume"
    assert err["sample_code"] == "S1"


# ----------------------------------------------------------------- 环路
def test_cycle_detected_with_path(client):
    _sample(client, "OP-1", "R", 1000)
    # R -> A -> B -> C
    for no, p, c in [("OP-2", "R", "A"), ("OP-3", "A", "B"), ("OP-4", "B", "C")]:
        r = client.post("/api/v1/operations", json={
            "op_type": "aliquot", "op_no": no,
            "parent_code": p, "child_code": c,
            "volume": 10, "unit": "mL",
        })
        assert r.status_code == 201
    # 试图把 C 的后代引回 R：R 已是 C 的祖先，C -> R 成环
    r = client.post("/api/v1/operations", json={
        "op_type": "aliquot", "op_no": "OP-CYC",
        "parent_code": "C", "child_code": "R",
        "volume": 1, "unit": "mL",
    })
    assert r.status_code == 409
    err = r.json()["errors"][0]
    assert err["reason"] == "cycle_detected"
    assert err["cycle_path"][0] == err["cycle_path"][-1]
    assert set(["R", "C"]).issubset(set(err["cycle_path"]))


def test_self_loop_rejected(client):
    _sample(client, "OP-1", "R", 100)
    r = client.post("/api/v1/operations", json={
        "op_type": "aliquot", "op_no": "OP-SLF",
        "parent_code": "R", "child_code": "R",
        "volume": 1, "unit": "mL",
    })
    assert r.status_code == 409
    assert r.json()["errors"][0]["reason"] == "cycle_detected"


# ----------------------------------------------------------------- 谱系
def test_lineage_sources_and_destinations(client):
    _sample(client, "OP-1", "ROOT1", 100)
    _sample(client, "OP-2", "ROOT2", 100)
    client.post("/api/v1/operations", json={
        "op_type": "aliquot", "op_no": "OP-3",
        "parent_code": "ROOT1", "child_code": "MID",
        "volume": 40, "unit": "mL"})
    client.post("/api/v1/operations", json={
        "op_type": "merge", "op_no": "OP-4",
        "parent_codes": ["MID", "ROOT2"],
        "volumes": [40, 30], "child_code": "MIX", "unit": "mL"})
    client.post("/api/v1/operations", json={
        "op_type": "aliquot", "op_no": "OP-5",
        "parent_code": "MIX", "child_code": "FINAL",
        "volume": 20, "unit": "mL"})

    up = client.get("/api/v1/samples/MIX/lineage?direction=sources").json()
    assert sorted(up["roots"]) == ["ROOT1", "ROOT2"]
    assert up["leaves"] == ["MIX"]
    # 完整来源路径
    paths = {tuple(p) for p in up["paths"]}
    assert ("ROOT1", "MID", "MIX") in paths
    assert ("ROOT2", "MIX") in paths
    node_codes = {n["code"] for n in up["nodes"]}
    assert node_codes == {"MIX", "MID", "ROOT1", "ROOT2"}
    assert "FINAL" not in node_codes  # 方向隔离

    down = client.get("/api/v1/samples/MID/lineage?direction=destinations").json()
    assert down["roots"] == ["MID"]
    assert down["leaves"] == ["FINAL"]
    assert ["MID", "MIX", "FINAL"] in down["paths"]

    both = client.get("/api/v1/samples/MIX/lineage").json()
    assert sorted(both["roots"]) == ["ROOT1", "ROOT2"]
    assert both["leaves"] == ["FINAL"]


def test_lineage_unknown_sample_404(client):
    r = client.get("/api/v1/samples/NOPE/lineage")
    assert r.status_code == 404


# ----------------------------------------------------------------- 批量
def test_batch_success_and_rollback(client):
    ops = [
        {"op_type": "sample", "op_no": "B1", "sample_code": "S1",
         "volume": 100, "unit": "mL"},
        {"op_type": "aliquot", "op_no": "B2", "parent_code": "S1",
         "child_code": "A1", "volume": 30, "unit": "mL"},
        {"op_type": "merge", "op_no": "B3", "parent_codes": ["A1", "S1"],
         "volumes": [30, 20], "child_code": "M1", "unit": "mL"},
    ]
    r = client.post("/api/v1/operations/batch", json={"operations": ops})
    assert r.status_code == 201
    assert len(r.json()["results"]) == 3
    assert client.get("/api/v1/samples/M1").json()["remaining"] == 50
    assert client.get("/api/v1/samples/S1").json()["remaining"] == 50

    bad = [
        {"op_type": "sample", "op_no": "B4", "sample_code": "S9",
         "volume": 10, "unit": "mL"},
        {"op_type": "aliquot", "op_no": "B5", "parent_code": "S1",
         "child_code": "A9", "volume": 9999, "unit": "mL"},  # 余量不足
    ]
    r = client.post("/api/v1/operations/batch", json={"operations": bad})
    assert r.status_code == 409
    errors = r.json()["errors"]
    assert len(errors) == 1
    assert errors[0]["index"] == 1
    assert errors[0]["op_no"] == "B5"
    assert errors[0]["reason"] == "insufficient_volume"
    # 整批回滚：S9 不存在
    assert client.get("/api/v1/samples/S9").status_code == 404


def test_batch_collects_multiple_errors_and_positions(client):
    _sample(client, "X0", "S1", 10)
    ops = [
        {"op_type": "aliquot", "op_no": "E1", "parent_code": "GHOST",
         "child_code": "C1", "volume": 1, "unit": "mL"},       # 0 父不存在
        {"op_type": "aliquot", "op_no": "E2", "parent_code": "S1",
         "child_code": "C2", "volume": 999, "unit": "mL"},     # 1 余量不足
        {"op_type": "sample", "op_no": "X0", "sample_code": "C3",
         "volume": 1, "unit": "mL"},                           # 2 操作号冲突
    ]
    r = client.post("/api/v1/operations/batch", json={"operations": ops})
    assert r.status_code == 409
    errors = r.json()["errors"]
    by_idx = {e["index"]: e["reason"] for e in errors}
    assert by_idx == {0: "parent_not_found", 1: "insufficient_volume",
                      2: "op_no_conflict"}


def test_batch_duplicate_op_no_and_output_inside_batch(client):
    ops = [
        {"op_type": "sample", "op_no": "D1", "sample_code": "S1",
         "volume": 10, "unit": "mL"},
        {"op_type": "sample", "op_no": "D1", "sample_code": "S2",
         "volume": 10, "unit": "mL"},
    ]
    r = client.post("/api/v1/operations/batch", json={"operations": ops})
    assert r.status_code == 409
    assert any(e["reason"] == "duplicate_op_no" and e["index"] == 1
               for e in r.json()["errors"])


def test_batch_idempotent_replay(client):
    ops = [{"op_type": "sample", "op_no": "R1", "sample_code": "S1",
            "volume": 10, "unit": "mL"}]
    r1 = client.post("/api/v1/operations/batch", json={"operations": ops})
    assert r1.status_code == 201
    r2 = client.post("/api/v1/operations/batch", json={"operations": ops})
    assert r2.status_code == 200
    assert all(x["replayed"] for x in r2.json()["results"])


def test_batch_internal_cycle_detected(client):
    # 批内先建链 R->A->B，再试图 C(=R) ... 用库内已有的环更直接：批内自环
    ops = [
        {"op_type": "sample", "op_no": "K1", "sample_code": "R",
         "volume": 1000, "unit": "mL"},
        {"op_type": "aliquot", "op_no": "K2", "parent_code": "R",
         "child_code": "A", "volume": 10, "unit": "mL"},
        {"op_type": "aliquot", "op_no": "K3", "parent_code": "A",
         "child_code": "R", "volume": 1, "unit": "mL"},  # 成环
    ]
    r = client.post("/api/v1/operations/batch", json={"operations": ops})
    assert r.status_code == 409
    errs = r.json()["errors"]
    assert any(e["reason"] == "cycle_detected" and e["index"] == 2 for e in errs)
    # 全部回滚
    assert client.get("/api/v1/samples/R").status_code == 404


# ----------------------------------------------------------------- 预检
def test_validate_dry_run_persists_nothing(client):
    _sample(client, "V0", "S1", 10)
    payload = {"operations": [
        {"op_type": "aliquot", "op_no": "V1", "parent_code": "S1",
         "child_code": "A1", "volume": 4, "unit": "mL"},
        {"op_type": "aliquot", "op_no": "V2", "parent_code": "S1",
         "child_code": "A2", "volume": 99, "unit": "mL"},
    ]}
    r = client.post("/api/v1/operations/validate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["items"][0]["valid"] is False  # 有冲突时同批项均不落结果
    assert body["errors"][0]["index"] == 1
    # 未写入
    assert client.get("/api/v1/operations/V1").status_code == 404
    assert client.get("/api/v1/samples/S1").json()["remaining"] == 10

    ok = client.post("/api/v1/operations/validate", json={
        "operations": [payload["operations"][0]]
    }).json()
    assert ok["valid"] is True
    assert ok["items"][0]["result"]["parent_remaining"] == 6
    # 预检后仍未落库
    assert client.get("/api/v1/operations/V1").status_code == 404
    assert client.get("/api/v1/samples/S1").json()["remaining"] == 10


# ----------------------------------------------------------------- 输入校验
def test_invalid_payloads(client):
    # 未知 op_type
    r = client.post("/api/v1/operations", json={
        "op_type": "nope", "op_no": "P1"})
    assert r.status_code == 422
    # 体积必须大于 0
    r = client.post("/api/v1/operations", json={
        "op_type": "sample", "op_no": "P2",
        "sample_code": "S", "volume": 0, "unit": "mL"})
    assert r.status_code == 422
    # merge 至少两个父样品且不重复
    r = client.post("/api/v1/operations", json={
        "op_type": "merge", "op_no": "P3",
        "parent_codes": ["S1", "S1"], "child_code": "C", "unit": "mL"})
    assert r.status_code == 422
    # 空批次
    r = client.post("/api/v1/operations/batch", json={"operations": []})
    assert r.status_code == 422
