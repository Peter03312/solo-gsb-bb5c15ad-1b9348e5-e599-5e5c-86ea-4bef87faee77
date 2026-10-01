"""付款额度预留指令：创建（预留）、取消（释放）、确认（原子登记付款）。

业务规则：

- 创建预留：财务凭"权利人编号 + 唯一指令号（instruction_ref）+ 正整数分额"
  预留付款额度。只有"当前余额扣除全部待确认预留后仍足额"（available >= amount）
  才创建指令（status=pending），返回指令状态与可用额；同号同内容重试返回原指令
  （status=duplicate，HTTP 200），同号异内容（权利人或金额不同）拒绝
  （409 reservation_ref_conflict）。
- 取消：待确认指令可取消，取消即释放额度（之后不再计入待确认预留）；
  取消可幂等重试；指令不存在 404 reservation_not_found，
  已终结（confirmed）指令不能取消（409 reservation_not_pending）。
- 确认：提交外部付款流水号，原子登记一笔现有付款并结束预留（pending->confirmed），
  返回付款记录及当前余额；同流水号重试幂等（确认本指令登记的付款），
  异流水号或被其他付款占用的流水号拒绝（409 payment_ref_conflict）。
- 负额追补可能冲减余额：确认时须留足"更早创建的待确认指令及本指令金额"，
  即 balance >= 本指令金额 + 更早 pending 总额，否则拒绝且指令保持 pending 不变
  （409 reservation_exceeds_balance）。比本指令更晚创建的预留不在留足范围内
  （它们必须能容忍更早指令优先；不满足时其创建/确认本就会被拒或被取消）。

并发协议（单事务内固定加锁次序，无死锁）：
  指令号事务咨询锁 -> 外部付款流水号事务咨询锁（仅确认）-> 权利人 FOR UPDATE。
- 指令号锁序列化同一指令的并发创建/取消/确认，幂等与状态判定确定；
- 确认与直接付款登记共用"外部付款流水号锁"，"流水号已被其他付款占用"的
  判定与付款插入原子；
- 权利人行锁与付款登记、银行退回、出单互斥，余额/预留求和所见即所入账。
"""
from .errors import APIError
from .payments_service import (
    _combined_key,
    _ref_lock_key,
    get_available,
    get_balance,
    get_reserved_pending,
    lock_payment_ref,
)

# 预留指令号咨询锁命名空间
_RESERVATION_REF_NS = 0x52535631  # "RSV1"


def _reservation_lock_key(instruction_ref: str) -> int:
    return _ref_lock_key(instruction_ref)


def _lock_instruction(conn, instruction_ref: str) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (
            _combined_key(
                _RESERVATION_REF_NS,
                _reservation_lock_key(instruction_ref),
            ),
        ),
    )


def _load_reservation(conn, instruction_ref: str):
    return conn.execute(
        """
        SELECT v.id, v.holder_code, v.instruction_ref, v.amount, v.status,
               v.payment_id, v.created_at, v.updated_at,
               p.external_ref AS payment_ref
        FROM payment_reservations v
        LEFT JOIN payments p ON p.id = v.payment_id
        WHERE v.instruction_ref = %s
        """,
        (instruction_ref,),
    ).fetchone()


def _reservation_payload(conn, row, status: str) -> dict:
    """组装指令响应；balance/available 取操作（或重试）时的当前值。"""
    balance = get_balance(conn, row["holder_code"])
    reserved = get_reserved_pending(conn, row["holder_code"])
    return {
        "reservation_id": row["id"],
        "holder_code": row["holder_code"],
        "instruction_ref": row["instruction_ref"],
        "amount": int(row["amount"]),
        # created=新创建；cancelled=本次取消；duplicate=同号同内容/重复操作重试
        "status": status,
        # 指令当前状态：pending | confirmed | cancelled
        "reservation_status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "balance": balance,
        # 待确认预留总额（含本指令：pending 时；取消后不含）与扣除后的可用额
        "reserved_pending": reserved,
        "available": balance - reserved,
        # 已确认指令附带付款流水号；其余为 None
        "payment_ref": row["payment_ref"],
    }


def create_reservation(conn, holder_code: str, instruction_ref: str, amount: int) -> dict:
    """创建付款额度预留（余额扣除全部待确认预留后足额才创建）。"""
    with conn.transaction():
        _lock_instruction(conn, instruction_ref)

        existing = _load_reservation(conn, instruction_ref)
        if existing is not None:
            if (
                existing["holder_code"] != holder_code
                or int(existing["amount"]) != amount
            ):
                raise _instruction_conflict(instruction_ref, existing)
            # 同号同内容重试：无论指令当前处于何种状态，都返回原指令，
            # 不重复创建、不改状态。
            return _reservation_payload(conn, existing, status="duplicate")

        # 锁定权利人行：与付款/退回/出单互斥；权利人不存在则 404。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:
            raise _holder_not_found(holder_code)

        balance = get_balance(conn, holder_code)
        reserved = get_reserved_pending(conn, holder_code)
        available = balance - reserved
        # 只有当前余额扣除全部待确认预留后仍足额才创建（available 可能为负）
        if amount > available:
            raise _exceeds_reservation(
                holder_code, instruction_ref, amount, balance, reserved, available
            )

        row = conn.execute(
            """
            INSERT INTO payment_reservations(holder_code, instruction_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (holder_code, instruction_ref, amount),
        ).fetchone()
        created = _load_reservation(conn, instruction_ref)
        assert int(created["id"]) == row["id"]
        return _reservation_payload(conn, created, status="created")


def cancel_reservation(conn, instruction_ref: str) -> dict:
    """取消待确认指令并释放额度；取消幂等，已确认指令不可取消。"""
    with conn.transaction():
        _lock_instruction(conn, instruction_ref)

        existing = _load_reservation(conn, instruction_ref)
        if existing is None:
            raise _instruction_not_found(instruction_ref)

        if existing["status"] == "cancelled":
            # 重复取消：幂等返回原指令，额度此前已释放
            return _reservation_payload(conn, existing, status="duplicate")
        if existing["status"] == "confirmed":
            raise _not_pending(instruction_ref, "confirmed")

        # pending：取消。持锁后状态确定，UPDATE 只发生在 pending 行上；
        # 锁定权利人行，使"释放后可用额"与后续创建/付款的可见性串行一致。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (existing["holder_code"],),
        ).fetchone()
        if holder is None:  # 外键建档，理论不可达
            raise _instruction_not_found(instruction_ref)

        row = conn.execute(
            """
            UPDATE payment_reservations
            SET status = 'cancelled', updated_at = now()
            WHERE id = %s AND status = 'pending'
            RETURNING id, updated_at
            """,
            (existing["id"],),
        ).fetchone()
        if row is None:  # 极端竞态兜底：状态已被并发改动
            raise _not_pending(instruction_ref, "confirmed")

        cancelled = _load_reservation(conn, instruction_ref)
        return _reservation_payload(conn, cancelled, status="cancelled")


def confirm_reservation(conn, instruction_ref: str, external_ref: str) -> dict:
    """确认待确认指令：原子登记外部付款并结束预留。

    - 同流水号重试（本指令此前已用该流水号确认）：幂等返回付款记录；
    - 流水号已被**其他付款**占用，或本指令已用**另一流水号**确认：
      409 payment_ref_conflict；
    - 指令已取消：409 reservation_not_pending；
    - 当前余额不足"更早待确认指令 + 本指令"：409 reservation_exceeds_balance，
      指令保持 pending 不变（不登记任何付款）。
    """
    with conn.transaction():
        _lock_instruction(conn, instruction_ref)

        existing = _load_reservation(conn, instruction_ref)
        if existing is None:
            raise _instruction_not_found(instruction_ref)
        reservation_id = existing["id"]
        holder_code = existing["holder_code"]
        amount = int(existing["amount"])

        if existing["status"] == "confirmed":
            # 已终结：只允许同流水号幂等返回原付款；异流水号拒绝
            if existing["payment_ref"] != external_ref:
                raise _reservation_ref_conflict(instruction_ref, existing, external_ref)
            payment = conn.execute(
                """
                SELECT id, holder_code, external_ref, amount, created_at
                FROM payments WHERE id = %s
                """,
                (existing["payment_id"],),
            ).fetchone()
            return _confirm_payload(existing, payment, duplicate=True, conn=conn)
        if existing["status"] == "cancelled":
            raise _not_pending(instruction_ref, "cancelled")

        # pending：先取外部付款流水号锁（与直接付款登记共用），
        # 再判定流水号是否已被其他付款占用。
        lock_payment_ref(conn, external_ref)
        other = conn.execute(
            """
            SELECT v.id AS reservation_id, p.id AS payment_id,
                   p.holder_code, p.external_ref, p.amount, p.created_at
            FROM payments p
            LEFT JOIN payment_reservations v ON v.payment_id = p.id
            WHERE p.external_ref = %s
            """,
            (external_ref,),
        ).fetchone()
        if other is not None:
            # 流水号已被付款占用：
            # - 由本指令确认登记不可能发生（本指令仍 pending），故必为他用：
            #   其他直接付款或其他指令的确认 => 拒绝，指令不变。
            raise _payment_ref_occupied(external_ref, other)

        # 锁定权利人行：与付款/退回/出单互斥。
        holder = conn.execute(
            "SELECT code FROM rightsholders WHERE code = %s FOR UPDATE",
            (holder_code,),
        ).fetchone()
        if holder is None:  # 外键建档，理论不可达
            raise _instruction_not_found(instruction_ref)

        # 负额追补可能冲减余额：确认时必须留足"更早创建的待确认指令 + 本指令"。
        balance = get_balance(conn, holder_code)
        earlier_pending = get_reserved_pending(
            conn, holder_code, before_id=reservation_id
        )
        required = amount + earlier_pending
        if balance < required:
            raise _insufficient_for_earlier(
                instruction_ref, holder_code, amount, balance, earlier_pending
            )

        # 原子登记现有付款并结束预留：付款行与状态迁移在同一事务内提交，
        # 外部观察不到"已付款但未终结"或"已终结但无付款"的中间态。
        payment_row = conn.execute(
            """
            INSERT INTO payments(holder_code, external_ref, amount)
            VALUES (%s, %s, %s)
            RETURNING id, created_at
            """,
            (holder_code, external_ref, amount),
        ).fetchone()
        updated = conn.execute(
            """
            UPDATE payment_reservations
            SET status = 'confirmed', payment_id = %s, updated_at = now()
            WHERE id = %s AND status = 'pending'
            RETURNING id, updated_at
            """,
            (payment_row["id"], reservation_id),
        ).fetchone()
        if updated is None:  # 兜底：持锁后状态不应被改动
            raise _not_pending(instruction_ref, "confirmed")

        payment = {
            "id": payment_row["id"],
            "holder_code": holder_code,
            "external_ref": external_ref,
            "amount": amount,
            "created_at": payment_row["created_at"],
        }
        final = _load_reservation(conn, instruction_ref)
        return _confirm_payload(final, payment, duplicate=False, conn=conn)


def _confirm_payload(reservation, payment, duplicate: bool, conn) -> dict:
    """确认响应：付款记录与当前余额（确认把预留转为付款，balance 扣减）。"""
    holder_code = payment["holder_code"]
    balance = get_balance(conn, holder_code)
    reserved = get_reserved_pending(conn, holder_code)
    return {
        "reservation_id": reservation["id"],
        "holder_code": holder_code,
        "instruction_ref": reservation["instruction_ref"],
        "amount": int(payment["amount"]),
        # created=本次确认并登记付款；duplicate=同流水号幂等重试
        "status": "duplicate" if duplicate else "created",
        "reservation_status": reservation["status"],
        "payment_id": payment["id"],
        "payment_ref": payment["external_ref"],
        "registered_at": payment["created_at"],
        "confirmed_at": reservation["updated_at"],
        # 当前余额（已扣减本笔付款）与当时待确认预留/可用额
        "balance_after": balance,
        "reserved_pending": reserved,
        "available": balance - reserved,
    }


def get_reservation(conn, instruction_ref: str):
    """查询单条指令状态（权利人对账的单指令视图）。"""
    with conn.transaction():
        _lock_instruction(conn, instruction_ref)
        row = _load_reservation(conn, instruction_ref)
        if row is None:
            return None
        # 查询无写操作：操作状态直接回显指令当前状态（pending/confirmed/cancelled）
        return _reservation_payload(conn, row, status=row["status"])


# ---- 错误 ----

def _holder_not_found(holder_code: str):
    return APIError(
        404,
        "holder_not_found",
        f"rightsholder {holder_code} not found",
    )


def _instruction_not_found(instruction_ref: str):
    return APIError(
        404,
        "reservation_not_found",
        f"payment reservation {instruction_ref} not found",
        detail={"instruction_ref": instruction_ref},
    )


def _instruction_conflict(instruction_ref: str, existing):
    return APIError(
        409,
        "reservation_ref_conflict",
        f"instruction ref {instruction_ref} already created with different content",
        detail={
            "instruction_ref": instruction_ref,
            "existing_holder_code": existing["holder_code"],
            "existing_amount": int(existing["amount"]),
            "existing_status": existing["status"],
        },
    )


def _reservation_ref_conflict(instruction_ref: str, existing, requested_ref: str):
    return APIError(
        409,
        "reservation_ref_conflict",
        f"reservation {instruction_ref} already confirmed with a different "
        f"payment ref {existing['payment_ref']}; requested {requested_ref}",
        detail={
            "instruction_ref": instruction_ref,
            "existing_payment_ref": existing["payment_ref"],
            "requested_payment_ref": requested_ref,
        },
    )


def _not_pending(instruction_ref: str, state: str):
    return APIError(
        409,
        "reservation_not_pending",
        f"payment reservation {instruction_ref} is {state} and can no longer be "
        f"cancelled or confirmed again",
        detail={"instruction_ref": instruction_ref, "state": state},
    )


def _exceeds_reservation(
    holder_code, instruction_ref, amount, balance, reserved, available
):
    return APIError(
        409,
        "reservation_exceeds_balance",
        f"reservation amount {amount} exceeds available amount {available} "
        f"(balance {balance} minus pending reservations {reserved}) "
        f"of rightsholder {holder_code}; reservation rejected",
        detail={
            "holder_code": holder_code,
            "instruction_ref": instruction_ref,
            "requested_amount": amount,
            "balance": balance,
            "reserved_pending": reserved,
            "available": available,
        },
    )


def _insufficient_for_earlier(
    instruction_ref, holder_code, amount, balance, earlier_pending
):
    return APIError(
        409,
        "reservation_exceeds_balance",
        f"confirming reservation {instruction_ref} requires balance to cover "
        f"amount {amount} plus earlier pending reservations {earlier_pending} "
        f"(required {amount + earlier_pending}, balance {balance}); "
        f"reservation stays pending",
        detail={
            "holder_code": holder_code,
            "instruction_ref": instruction_ref,
            "requested_amount": amount,
            "balance": balance,
            "earlier_pending_reservations": earlier_pending,
            "required": amount + earlier_pending,
        },
    )


def _payment_ref_occupied(external_ref: str, other):
    return APIError(
        409,
        "payment_ref_conflict",
        f"payment ref {external_ref} is already used by another payment; "
        f"cannot use it to confirm the reservation",
        detail={
            "external_ref": external_ref,
            "existing_payment_id": other["payment_id"],
            "existing_holder_code": other["holder_code"],
            "existing_amount": int(other["amount"]),
            "occupied_by_reservation_id": other["reservation_id"],
        },
    )
