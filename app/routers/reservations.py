from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from .. import reservations_service as svc
from ..db import get_conn
from ..errors import APIError
from ..schemas import (
    ReservationActionOut,
    ReservationConfirmIn,
    ReservationConfirmOut,
    ReservationCreateIn,
)

router = APIRouter(tags=["payment-reservations"])


@router.post("/payment-reservations")
def create_reservation(body: ReservationCreateIn, conn=Depends(get_conn)):
    """凭权利人编号、唯一指令号与正整数分额预留付款额度。

    - 只有当前余额扣除**全部待确认预留**后仍足额（available >= amount）才创建，
      返回指令状态（pending）与当前余额、待确认预留总额、可用额；
    - 同指令号同内容重试幂等返回原指令（status=duplicate，HTTP 200）；
      同指令号异内容（权利人或金额不同）409 reservation_ref_conflict；
    - 权利人不存在 404 holder_not_found；金额非正整数 422；
      可用额不足 409 reservation_exceeds_balance，拒绝且不留任何预留。
    """
    result = svc.create_reservation(
        conn, body.holder_code, body.instruction_ref, body.amount
    )
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=ReservationActionOut(**result).model_dump(mode="json"),
    )


@router.post("/payment-reservations/{instruction_ref}/cancel")
def cancel_reservation(instruction_ref: str, conn=Depends(get_conn)):
    """取消待确认指令并释放额度。

    - 待确认指令取消即释放额度（不再计入待确认预留），200 返回指令与最新可用额；
      重复取消幂等（status=duplicate，HTTP 200），不重复变更；
    - 指令不存在 404 reservation_not_found；指令已确认（无法取消）
      409 reservation_not_pending。
    """
    result = svc.cancel_reservation(conn, instruction_ref)
    # 取消为状态迁移，统一 200（首次 status=cancelled，重试 status=duplicate）
    return JSONResponse(
        status_code=200,
        content=ReservationActionOut(**result).model_dump(mode="json"),
    )


@router.post("/payment-reservations/{instruction_ref}/confirm")
def confirm_reservation(
    instruction_ref: str, body: ReservationConfirmIn, conn=Depends(get_conn)
):
    """确认待确认指令：提交外部付款流水号，原子登记现有付款并结束预留。

    - 在同一事务内登记一笔付款（沿用付款登记字段）并把指令置为 confirmed，
      返回付款记录及登记后余额；
    - 同一流水号重试幂等返回原付款（status=duplicate，HTTP 200）；
      异流水号，或该流水号已被其他付款占用，409 payment_ref_conflict；
    - 负额追补可能冲减余额：确认时余额须留足**更早创建的待确认指令及本指令**
      金额，否则 409 reservation_exceeds_balance 且指令保持 pending 不变；
    - 指令不存在 404 reservation_not_found；已取消 409 reservation_not_pending；
      已确认但提交另一流水号 409 reservation_ref_conflict。
    """
    result = svc.confirm_reservation(conn, instruction_ref, body.external_ref)
    status = 201 if result["status"] == "created" else 200
    return JSONResponse(
        status_code=status,
        content=ReservationConfirmOut(**result).model_dump(mode="json"),
    )


@router.get(
    "/payment-reservations/{instruction_ref}",
    response_model=ReservationActionOut,
)
def get_reservation(instruction_ref: str, conn=Depends(get_conn)):
    """查询单条预留指令的状态与当时可用额（不存在 404 reservation_not_found）。"""
    result = svc.get_reservation(conn, instruction_ref)
    if result is None:
        raise APIError(
            404,
            "reservation_not_found",
            f"payment reservation {instruction_ref} not found",
        )
    return result
