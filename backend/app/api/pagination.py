from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel

T = TypeVar("T")

MAX_LIMIT = 200


class Pagination(BaseModel):
    limit: int
    offset: int


def pagination_params(limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0)) -> Pagination:
    return Pagination(limit=limit, offset=offset)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
