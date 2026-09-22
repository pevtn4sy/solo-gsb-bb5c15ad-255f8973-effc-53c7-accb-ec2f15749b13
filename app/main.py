"""FastAPI 入口：科研样品分装追溯 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .database import get_conn, init_db
from .lineage import check_remaining, trace_lineage
from .schemas import (
    BatchRequest,
    BatchResponse,
    LineageResponse,
    Operation,
    OperationRecord,
    RemainingResponse,
    SampleView,
    ValidateItem,
    ValidateRequest,
    ValidateResponse,
)
from .services import ConflictError, apply_batch, apply_one, _load_record


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="科研样品分装追溯 API",
    version="1.0.0",
    description=(
        "取样 / 分装 / 合并操作的事务化追溯服务：父样品余量校验、"
        "环路禁止、谱系 DAG 维护、操作号幂等与批量处理。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ConflictError)
async def conflict_handler(request: Request, exc: ConflictError):
    # 仅“样品不存在”归为 404，其余业务冲突统一 409，并带冲突操作定位
    only_not_found = bool(exc.errors) and all(
        e.get("reason") == "sample_not_found" for e in exc.errors
    )
    status_code = 404 if only_not_found else 409
    return JSONResponse(status_code=status_code,
                        content={"detail": "操作冲突", "errors": exc.errors})


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "sample-traceability",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["meta"])
def health(conn=Depends(get_conn)):
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# ---------------------------------------------------------------- 操作写入
@app.post("/api/v1/operations", response_model=OperationRecord,
          tags=["operations"],
          status_code=201,
          summary="提交单个取样/分装/合并操作（按 op_no 幂等）")
def submit_operation(op: Operation, conn=Depends(get_conn)):
    """重复提交相同 ``op_no`` 与相同载荷时，幂等返回首次结果（replayed=true）。

    相同 ``op_no`` 但载荷不同返回 409 ``op_no_conflict``。
    余量不足、样品不存在、单位不一致、会成环等同样返回 409 并带定位。
    """
    try:
        record = apply_one(conn, op)
    except ConflictError:
        raise
    if record.get("replayed"):
        # 幂等回放以 200 返回；首次写入 201
        return JSONResponse(status_code=200, content=record)
    return record


@app.post("/api/v1/operations/batch", response_model=BatchResponse,
          tags=["operations"],
          status_code=201,
          summary="批量提交（整批一个事务，全部成功或全部回滚）")
def submit_batch(req: BatchRequest, conn=Depends(get_conn)):
    records, errors = apply_batch(conn, req.operations)
    if errors:
        # 整批失败：返回每一个冲突操作及其在批次中的位置 index
        return JSONResponse(status_code=409,
                            content={"detail": "批量操作存在冲突，整批已回滚",
                                     "errors": errors})
    replayed = all(r.get("replayed") for r in records)
    return JSONResponse(status_code=200 if replayed and records else 201,
                        content={"results": records})


@app.post("/api/v1/operations/validate", response_model=ValidateResponse,
          tags=["operations"],
          summary="余量/冲突预检（dry-run，不写入任何数据）")
def validate_operations(req: ValidateRequest, conn=Depends(get_conn)):
    """逐操作校验，返回每项 valid 与所有冲突定位；已存在的相同 op_no 视为有效回放。"""
    records, errors = apply_batch(conn, req.operations, dry_run=True)
    items = []
    if not errors:
        for i, rec in enumerate(records):
            items.append(ValidateItem(
                index=i, op_no=rec["op_no"], valid=True, result=rec["result"]
            ))
    else:
        err_by_index = {e.get("index"): e for e in errors if e.get("index") is not None}
        for i, op in enumerate(req.operations):
            if i in err_by_index:
                items.append(ValidateItem(index=i, op_no=op.op_no, valid=False))
            else:
                # 与冲突项同批的操作不单独给出模拟结果
                items.append(ValidateItem(index=i, op_no=op.op_no, valid=False))
    return ValidateResponse(valid=not errors, items=items, errors=errors)


@app.get("/api/v1/operations/{op_no}", response_model=OperationRecord,
         tags=["operations"], summary="按操作号查询原始记录与结果（幂等结果）")
def get_operation(op_no: str, conn=Depends(get_conn)):
    rec = _load_record(conn, op_no)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"操作号 {op_no} 不存在")
    return rec


# ---------------------------------------------------------------- 样品与追溯
@app.get("/api/v1/samples/{code}", response_model=SampleView,
         tags=["samples"], summary="查询样品当前余量等信息")
def get_sample(code: str, conn=Depends(get_conn)):
    row = conn.execute(
        "SELECT code, initial_volume, remaining, unit, created_by_op, created_at "
        "FROM samples WHERE code=?", (code,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"样品 {code} 不存在")
    return dict(row)


@app.get("/api/v1/samples/{code}/remaining", response_model=RemainingResponse,
         tags=["samples"], summary="余量校验（可选 requested 做充足性判断）")
def get_remaining(
    code: str,
    requested: float | None = Query(default=None, gt=0,
                                    description="拟取出的体积，校验是否够用"),
    conn=Depends(get_conn),
):
    return check_remaining(conn, code, requested)


@app.get("/api/v1/samples/{code}/lineage", response_model=LineageResponse,
         tags=["samples"],
         summary="按样品回溯完整来源/去向谱系（roots/leaves/完整路径）")
def get_lineage(
    code: str,
    direction: Literal["sources", "destinations", "both"] = Query(
        default="both", description="sources=向上回溯来源, destinations=向下追出去向"
    ),
    conn=Depends(get_conn),
):
    return trace_lineage(conn, code, direction)
