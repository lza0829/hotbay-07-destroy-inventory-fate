from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.services import retention as svc

router = APIRouter(tags=["保留"])


class RunIn(BaseModel):
    policy_id: int


@router.get("/保留策略")
def policies():
    return svc.list_policies()


@router.get("/销毁流水")
def destroys():
    return svc.list_destroys()


@router.post("/保留执行")
def run(body: RunIn):
    # 全局唯一口径「留幽灵销毁」，不再接受 mode 分叉
    return svc.run_retention(body.policy_id)
