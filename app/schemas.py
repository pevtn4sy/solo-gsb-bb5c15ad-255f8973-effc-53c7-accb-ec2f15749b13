"""Pydantic 请求 / 响应模型。

三种操作共用判别字段 ``op_type``：
- sample  取样：登记一个根样品（初始余量）
- aliquot 分装：从一个父样品分出一个子样品
- merge   合并：把多个父样品的余量消耗后合并成一个子样品
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator

Volume = Annotated[float, Field(strict=True, gt=0)]
Code = Annotated[str, Field(strict=True, min_length=1, max_length=128)]


# ---------------------------------------------------------------- 请求模型
class SampleOp(BaseModel):
    op_type: Literal["sample"]
    op_no: Code
    sample_code: Code
    volume: Volume
    unit: Code
    note: str | None = Field(default=None, max_length=512)


class AliquotOp(BaseModel):
    op_type: Literal["aliquot"]
    op_no: Code
    parent_code: Code
    child_code: Code
    volume: Volume
    unit: Code
    note: str | None = Field(default=None, max_length=512)


class MergeOp(BaseModel):
    op_type: Literal["merge"]
    op_no: Code
    parent_codes: list[Code] = Field(min_length=2)
    child_code: Code
    # 未指定各父样品消耗量时，默认全部投完（合并剩余余量）。
    volumes: list[float] | None = Field(default=None, min_length=2)
    unit: Code
    note: str | None = Field(default=None, max_length=512)

    @field_validator("parent_codes")
    @classmethod
    def _unique_parents(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("parent_codes 中存在重复样品")
        return v

    @field_validator("volumes")
    @classmethod
    def _positive(cls, v: list[float] | None) -> list[float] | None:
        if v is not None:
            if any(x <= 0 for x in v):
                raise ValueError("volumes 必须全部大于 0")
        return v


Operation = Annotated[
    Union[SampleOp, AliquotOp, MergeOp],
    Field(discriminator="op_type"),
]


class BatchRequest(BaseModel):
    operations: list[Operation] = Field(min_length=1)


class ValidateRequest(BaseModel):
    """余量/冲突预检请求：只校验不落库。"""
    operations: list[Operation] = Field(min_length=1)


# ---------------------------------------------------------------- 响应模型
class SampleOpResult(BaseModel):
    sample_code: str
    initial_volume: float
    remaining: float
    unit: str


class AliquotOpResult(BaseModel):
    parent_code: str
    parent_remaining: float
    child_code: str
    child_remaining: float
    consumed: float
    unit: str


class MergeItem(BaseModel):
    parent_code: str
    parent_remaining: float
    consumed: float


class MergeOpResult(BaseModel):
    parents: list[MergeItem]
    child_code: str
    child_remaining: float
    total_volume: float
    unit: str


OpResult = Annotated[
    Union[SampleOpResult, AliquotOpResult, MergeOpResult],
    Field(discriminator=None),
]


class OperationRecord(BaseModel):
    op_no: str
    op_type: str
    payload: dict
    result: dict
    replayed: bool = False
    created_at: str


class BatchResponse(BaseModel):
    results: list[OperationRecord]


class ValidateItem(BaseModel):
    index: int
    op_no: str
    valid: bool
    result: dict | None = None


class ValidateResponse(BaseModel):
    valid: bool
    items: list[ValidateItem]
    errors: list[dict]


class SampleView(BaseModel):
    code: str
    initial_volume: float
    remaining: float
    unit: str
    created_by_op: str
    created_at: str


class LineageNode(BaseModel):
    code: str
    remaining: float
    initial_volume: float
    unit: str


class LineageEdge(BaseModel):
    parent: str
    child: str
    consumed_volume: float | None
    op_no: str


class LineageResponse(BaseModel):
    direction: Literal["sources", "destinations", "both"]
    center: str
    nodes: list[LineageNode]
    edges: list[LineageEdge]
    roots: list[str]
    leaves: list[str]
    paths: list[list[str]]


class RemainingResponse(BaseModel):
    code: str
    remaining: float
    unit: str
    sufficient: bool
    requested: float | None = None
