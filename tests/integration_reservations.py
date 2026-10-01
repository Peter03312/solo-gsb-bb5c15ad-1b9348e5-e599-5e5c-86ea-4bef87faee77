"""付款额度预留指令：端到端集成测试（pgserver 便携 PostgreSQL，无需 Docker）。

覆盖：
- 创建预留（可用额口径：余额 - 全部待确认预留；足额才创建）；
- 同号同内容重试幂等、同号异内容 409；权利人不存在 404；金额非正整数 422；
- 取消释放额度与幂等；不存在 404；已确认不可取消 409；
- 确认原子登记付款并结束预留；同流水号幂等；异流水号/被其他付款占用 409；
- 负额追补冲减余额：确认时须留足更早创建的待确认指令及本指令，
  否则拒绝且指令保持 pending 不变（更早指令优先、更晚指令不影响确认）；
- 原付款登记接口新付款避开待确认预留（余额足够但可用额不足时拒绝），
  输入/响应/错误码兼容；退回继续加回余额；
- 权利人对账可见待确认预留、可用额与指令状态；历史指令与付款不可改写（触发器）；
- 并发：同指令并发创建仅一条；并发确认/直接付款对同流水号串行不重复入账。

运行: PYTHONPATH=. python3 tests/integration_reservations.py
"""
import os
import pathlib
import tempfile
import threading

import pgserver

_pgdata = pathlib.Path(tempfile.mkdtemp(prefix="royalty_rsv_")) / "pgdata"
server = pgserver.get_server(_pgdata, cleanup_mode="delete")
try:
    os.environ["DATABASE_URL"] = server.get_uri()
    # 配置必须在设置环境变量后导入
    from fastapi.testclient import TestClient
    import psycopg
    from app import db
    from app.main import app

    failures = []

    def check(name, cond, extra=""):
        print(("PASS" if cond else "FAIL"), name, extra)
        if not cond:
            failures.append(name)

    def post(c, path, body=None):
        return c.post(path, json=body if body is not None else {})

    with TestClient(app) as c:
        check("health", c.get("/health").json()["status"] == "ok")

        # ---- 建档：作品 + 两名权利人；出具 100 分单据：A=60，B=40 ----
        c.post("/works", json={"code": "W", "title": "作品"})
        c.post("/rightsholders", json={"code": "A", "name": "甲"})
        c.post("/rightsholders", json={"code": "B", "name": "乙"})
        c.post("/agreements", json={
            "work_code": "W",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "A", "percent": "60"},
                       {"holder_code": "B", "percent": "40"}]})
        c.post("/plays/import", json={
            "import_id": "P1", "work_code": "W",
            "occurred_at": "2026-02-10T20:00:00+08:00", "amount": 100})
        period = {"start": "2026-02-01T00:00:00+08:00",
                  "end": "2026-03-01T00:00:00+08:00"}
        c.post("/statements/issue", json=period)

        # 无预留：reserved=0，available=balance
        led = c.get("/rightsholders/A/ledger").json()
        check("A baseline ledger", led["balance"] == 60
              and led["reserved_total"] == 0 and led["available"] == 60
              and led["reservations"] == [], str(led))

        # ---- 创建预留：40 分，可用额 60-40=20 ----
        r = post(c, "/payment-reservations",
                 {"holder_code": "A", "instruction_ref": "R1", "amount": 40})
        body = r.json()
        check("create R1 201", r.status_code == 201
              and body["status"] == "created"
              and body["reservation_status"] == "pending"
              and body["balance"] == 60
              and body["reserved_pending"] == 40
              and body["available"] == 20
              and body["reservation_id"] >= 1, r.text)

        # 预留不改变余额，只缩小可用额
        led = c.get("/rightsholders/A/ledger").json()
        check("ledger reserves pending", led["balance"] == 60
              and led["reserved_total"] == 40 and led["available"] == 20
              and len(led["reservations"]) == 1
              and led["reservations"][0]["status"] == "pending"
              and led["reservations"][0]["payment_ref"] is None, str(led))

        # 同号同内容重试 => 200 duplicate，返回原指令，不重复预留
        r = post(c, "/payment-reservations",
                 {"holder_code": "A", "instruction_ref": "R1", "amount": 40})
        b2 = r.json()
        check("R1 idempotent", r.status_code == 200 and b2["status"] == "duplicate"
              and b2["reservation_id"] == body["reservation_id"]
              and b2["reservation_status"] == "pending"
              and b2["available"] == 20, r.text)
        check("R1 single row",
              len(c.get("/rightsholders/A/ledger").json()["reservations"]) == 1)

        # 同号异内容（金额不同 / 权利人不同）=> 409 reservation_ref_conflict
        r = post(c, "/payment-reservations",
                 {"holder_code": "A", "instruction_ref": "R1", "amount": 10})
        check("R1 conflict amount", r.status_code == 409
              and r.json()["error"] == "reservation_ref_conflict"
              and r.json()["detail"]["existing_amount"] == 40
              and r.json()["detail"]["existing_status"] == "pending", r.text)
        r = post(c, "/payment-reservations",
                 {"holder_code": "B", "instruction_ref": "R1", "amount": 40})
        check("R1 conflict holder", r.status_code == 409
              and r.json()["error"] == "reservation_ref_conflict"
              and r.json()["detail"]["existing_holder_code"] == "A", r.text)

        # 权利人不存在 404；金额非正整数/非整数/布尔 422
        r = post(c, "/payment-reservations",
                 {"holder_code": "NOBODY", "instruction_ref": "RX", "amount": 1})
        check("reservation holder missing 404", r.status_code == 404
              and r.json()["error"] == "holder_not_found", r.text)
        for bad in (0, -1, 1.5, True):
            r = post(c, "/payment-reservations",
                     {"holder_code": "A", "instruction_ref": f"RB-{bad}", "amount": bad})
            check(f"reservation bad amount {bad!r} 422", r.status_code == 422, r.text)

        # 可用额只剩 20：预留 21 被拒（余额 60 虽 >= 21，但待确认预留 40 后不足）
        r = post(c, "/payment-reservations",
                 {"holder_code": "A", "instruction_ref": "R2", "amount": 21})
        check("R2 exceeds available", r.status_code == 409
              and r.json()["error"] == "reservation_exceeds_balance"
              and r.json()["detail"]["balance"] == 60
              and r.json()["detail"]["reserved_pending"] == 40
              and r.json()["detail"]["available"] == 20, r.text)
        # 拒绝不留任何预留
        check("R2 rejected leaves no row",
              c.get("/payment-reservations/R2").status_code == 404
              and c.get("/rightsholders/A/ledger").json()["reserved_total"] == 40)

        # 恰好等于可用额 20 可以创建
        r = post(c, "/payment-reservations",
                 {"holder_code": "A", "instruction_ref": "R3", "amount": 20})
        check("R3 fills available 201", r.status_code == 201
              and r.json()["reserved_pending"] == 60
              and r.json()["available"] == 0, r.text)
        # 可用额 0，再预留 1 分被拒
        r = post(c, "/payment-reservations",
                 {"holder_code": "A", "instruction_ref": "R4", "amount": 1})
        check("R4 zero available rejected", r.status_code == 409
              and r.json()["error"] == "reservation_exceeds_balance", r.text)

        # ---- 原付款登记接口：新付款须避开待确认预留 ----
        # 余额 60 仍为正，但 60 已被 R1(40)+R3(20) 全额预留：
        # 直接付款 1 分也必须拒绝（错误码/余额字段保持兼容）
        r = post(c, "/payments",
                 {"holder_code": "A", "external_ref": "PA-DIR", "amount": 1})
        check("direct pay avoids reservations", r.status_code == 409
              and r.json()["error"] == "payment_exceeds_balance"
              and r.json()["detail"]["balance"] == 60
              and r.json()["detail"]["reserved_pending"] == 60
              and r.json()["detail"]["available"] == 0, r.text)
        check("direct pay rejected no row",
              c.get("/rightsholders/A/ledger").json()["paid_total"] == 0)

        # ---- 取消 R3：释放 20，可用额恢复 20 ----
        r = post(c, "/payment-reservations/R3/cancel")
        body = r.json()
        check("cancel R3", r.status_code == 200
              and body["status"] == "cancelled"
              and body["reservation_status"] == "cancelled"
              and body["reserved_pending"] == 40
              and body["available"] == 20, r.text)
        # 重复取消幂等
        r = post(c, "/payment-reservations/R3/cancel")
        check("cancel R3 idempotent", r.status_code == 200
              and r.json()["status"] == "duplicate"
              and r.json()["reservation_status"] == "cancelled"
              and r.json()["available"] == 20, r.text)

        # 取消不存在的指令 404
        r = post(c, "/payment-reservations/NOPE/cancel")
        check("cancel missing 404", r.status_code == 404
              and r.json()["error"] == "reservation_not_found", r.text)

        # 释放后直接付款 20 可以（避开剩余预留 R1=40）
        r = post(c, "/payments",
                 {"holder_code": "A", "external_ref": "PA-DIR", "amount": 20})
        check("direct pay 20 after release", r.status_code == 201
              and r.json()["status"] == "created"
              and r.json()["balance_after"] == 40, r.text)
        # 余额现在 40，恰被 R1 预留 40：再付 1 拒绝
        r = post(c, "/payments",
                 {"holder_code": "A", "external_ref": "PA-DIR2", "amount": 1})
        check("direct pay again blocked", r.status_code == 409
              and r.json()["detail"]["balance"] == 40
              and r.json()["detail"]["available"] == 0, r.text)

        # 取消已确认指令将被拒（后面确认 R1 后再测）

        # ---- 确认 R1：提交外部付款流水号，原子登记付款并结束预留 ----
        r = post(c, "/payment-reservations/R1/confirm",
                 {"external_ref": "PAY-R1"})
        body = r.json()
        check("confirm R1", r.status_code == 201
              and body["status"] == "created"
              and body["reservation_status"] == "confirmed"
              and body["payment_ref"] == "PAY-R1"
              and body["amount"] == 40
              and body["payment_id"] >= 1
              and body["balance_after"] == 0
              and body["reserved_pending"] == 0
              and body["available"] == 0, r.text)
        payment_id_r1 = body["payment_id"]

        # 付款已登记、预留已终结；对账单：余额 0，无待确认预留
        led = c.get("/rightsholders/A/ledger").json()
        check("ledger after confirm", led["billed_total"] == 60
              and led["paid_total"] == 60 and led["balance"] == 0
              and led["reserved_total"] == 0 and led["available"] == 0
              and {p["external_ref"] for p in led["payments"]}
              == {"PA-DIR", "PAY-R1"}
              and len(led["reservations"]) == 2, str(led))
        statuses = {v["instruction_ref"]: v["status"]
                    for v in led["reservations"]}
        check("ledger reservation states",
              statuses == {"R1": "confirmed", "R3": "cancelled"}
              or all(v["status"] in ("pending", "confirmed", "cancelled")
                     for v in led["reservations"]), str(statuses))
        r1_row = next(v for v in led["reservations"]
                      if v["instruction_ref"] == "R1")
        check("confirmed row carries payment_ref",
              r1_row["status"] == "confirmed" and r1_row["payment_ref"] == "PAY-R1",
              str(r1_row))

        # 同流水号重试确认 => 200 duplicate，返回原付款，不重复入账
        r = post(c, "/payment-reservations/R1/confirm",
                 {"external_ref": "PAY-R1"})
        b2 = r.json()
        check("confirm R1 idempotent", r.status_code == 200
              and b2["status"] == "duplicate"
              and b2["payment_id"] == payment_id_r1
              and b2["balance_after"] == 0, r.text)
        check("confirm no double payment",
              c.get("/rightsholders/A/ledger").json()["paid_total"] == 60)

        # 已确认指令提交另一流水号 => 409 reservation_ref_conflict
        r = post(c, "/payment-reservations/R1/confirm",
                 {"external_ref": "PAY-OTHER"})
        check("confirm R1 different ref", r.status_code == 409
              and r.json()["error"] == "reservation_ref_conflict"
              and r.json()["detail"]["existing_payment_ref"] == "PAY-R1"
              and r.json()["detail"]["requested_payment_ref"] == "PAY-OTHER",
              r.text)
        # 已确认指令不可取消 => 409 reservation_not_pending
        r = post(c, "/payment-reservations/R1/cancel")
        check("cancel confirmed rejected", r.status_code == 409
              and r.json()["error"] == "reservation_not_pending"
              and r.json()["detail"]["state"] == "confirmed", r.text)

        # 确认不存在的指令 404
        r = post(c, "/payment-reservations/NOPE/confirm",
                 {"external_ref": "X"})
        check("confirm missing 404", r.status_code == 404
              and r.json()["error"] == "reservation_not_found", r.text)

        # 流水号已被其他付款（直接付款）占用 => 409 payment_ref_conflict
        # B 余额 40，预留 30 后确认时使用已占用流水号 PA-DIR
        r = post(c, "/payment-reservations",
                 {"holder_code": "B", "instruction_ref": "RB1", "amount": 30})
        check("B reserve 30", r.status_code == 201 and r.json()["available"] == 10,
              r.text)
        r = post(c, "/payment-reservations/RB1/confirm",
                 {"external_ref": "PA-DIR"})
        check("confirm ref occupied by other payment", r.status_code == 409
              and r.json()["error"] == "payment_ref_conflict"
              and r.json()["detail"]["existing_holder_code"] == "A", r.text)
        # 被拒后指令保持 pending、可用额不变、未登记付款
        rb1 = c.get("/payment-reservations/RB1").json()
        check("RB1 stays pending after occupied ref",
              rb1["reservation_status"] == "pending"
              and rb1["available"] == 10, str(rb1))
        check("no payment for RB1 yet",
              c.get("/rightsholders/B/ledger").json()["paid_total"] == 0)

        # 同一流水号也不能被另一条预留指令确认占用（先确认 RB1，再让 RB2 抢同号）
        r = post(c, "/payment-reservations/RB1/confirm",
                 {"external_ref": "PAY-RB1"})
        check("confirm RB1", r.status_code == 201
              and r.json()["balance_after"] == 10, r.text)
        r = post(c, "/payment-reservations",
                 {"holder_code": "B", "instruction_ref": "RB2", "amount": 10})
        check("B reserve 10", r.status_code == 201 and r.json()["available"] == 0,
              r.text)
        r = post(c, "/payment-reservations/RB2/confirm",
                 {"external_ref": "PAY-RB1"})
        check("confirm ref occupied by other reservation", r.status_code == 409
              and r.json()["error"] == "payment_ref_conflict"
              and r.json()["detail"]["occupied_by_reservation_id"] >= 1, r.text)
        check("RB2 stays pending",
              c.get("/payment-reservations/RB2").json()["reservation_status"]
              == "pending")

        # ---- 负额追补冲减余额：确认时须留足更早创建的待确认指令及本指令 ----
        # 用独立 100% 权利人 D 做确定性场景，避免分摊取整干扰。
        c.post("/rightsholders", json={"code": "D", "name": "丁"})
        c.post("/works", json={"code": "WD", "title": "丁作品"})
        c.post("/agreements", json={
            "work_code": "WD",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "D", "percent": "100"}]})
        d_period = {"start": "2026-06-01T00:00:00+08:00",
                    "end": "2026-07-01T00:00:00+08:00"}
        # 出具 100 分单据（100% 归 D），登记一笔 20 付款 => 余额 80
        c.post("/plays/import", json={
            "import_id": "PD1", "work_code": "WD",
            "occurred_at": "2026-06-10T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json=d_period)
        check(post(c, "/payments",
                   {"holder_code": "D", "external_ref": "PAY-D0",
                    "amount": 20}).status_code == 201, "D baseline pay")
        check(c.get("/rightsholders/D/ledger").json()["balance"] == 80,
              "D baseline balance 80")

        # 余额 80：创建更早预留 EARLY=40、更晚预留 LATE=30（共 70，可用额剩 10）
        r = post(c, "/payment-reservations",
                 {"holder_code": "D", "instruction_ref": "EARLY", "amount": 40})
        check("EARLY created", r.status_code == 201
              and r.json()["reserved_pending"] == 40
              and r.json()["available"] == 40, r.text)
        r = post(c, "/payment-reservations",
                 {"holder_code": "D", "instruction_ref": "LATE", "amount": 30})
        check("LATE created", r.status_code == 201
              and r.json()["reserved_pending"] == 70
              and r.json()["available"] == 10, r.text)

        # 负额追补冲减 30：PD1 100 -> 70 => 单据 -30，余额 80 -> 50，
        # 待确认预留仍 70（可用额 -20）。
        c.post("/plays/import", json={
            "import_id": "PD1R1", "work_code": "WD",
            "occurred_at": "2026-06-10T20:00:00+08:00", "amount": 70,
            "corrects_id": "PD1"})
        c.post("/statements/issue", json=d_period)
        led = c.get("/rightsholders/D/ledger").json()
        check("negative supplement shrinks D balance",
              led["balance"] == 50 and led["reserved_total"] == 70
              and led["available"] == -20, str(led))

        # 确认更早的 EARLY(40)：须留足"更早 pending(0) + 自身 40"；50 >= 40，成功。
        r = post(c, "/payment-reservations/EARLY/confirm",
                 {"external_ref": "PAY-EARLY"})
        check("early confirmed when balance covers itself", r.status_code == 201
              and r.json()["reservation_status"] == "confirmed"
              and r.json()["balance_after"] == 10, r.text)
        # EARLY 转为付款后：余额 10，待确认只剩 LATE 30
        led = c.get("/rightsholders/D/ledger").json()
        check("D after early confirm", led["paid_total"] == 60
              and led["balance"] == 10 and led["reserved_total"] == 30
              and led["available"] == -20, str(led))

        # 确认更晚的 LATE(30)：EARLY 已确认（不计更早 pending），只须自身 30；
        # 余额 10 < 30 => 拒绝，LATE 保持 pending 不变、不登记付款。
        r = post(c, "/payment-reservations/LATE/confirm",
                 {"external_ref": "PAY-LATE"})
        check("late confirm rejected by shrunk balance", r.status_code == 409
              and r.json()["error"] == "reservation_exceeds_balance"
              and r.json()["detail"]["required"] == 30
              and r.json()["detail"]["earlier_pending_reservations"] == 0
              and r.json()["detail"]["balance"] == 10, r.text)
        check("LATE stays pending, no payment",
              c.get("/payment-reservations/LATE").json()["reservation_status"]
              == "pending"
              and c.get("/rightsholders/D/ledger").json()["paid_total"] == 60)

        # 再冲减使余额 -20：LATE 仍 pending；确认仍被拒且指令不变
        c.post("/plays/import", json={
            "import_id": "PD1R2", "work_code": "WD",
            "occurred_at": "2026-06-10T20:00:00+08:00", "amount": 40,
            "corrects_id": "PD1R1"})
        c.post("/statements/issue", json=d_period)
        led = c.get("/rightsholders/D/ledger").json()
        check("D negative balance possible", led["balance"] == -20, str(led))
        r = post(c, "/payment-reservations/LATE/confirm",
                 {"external_ref": "PAY-LATE"})
        check("late confirm still rejected at negative balance", r.status_code == 409
              and r.json()["detail"]["balance"] == -20, r.text)
        check("LATE still pending",
              c.get("/payment-reservations/LATE").json()["reservation_status"]
              == "pending")

        # 后续正额恢复：PD1 40 -> 100（追补 +60），余额 -20 -> 40，LATE(30) 可确认
        c.post("/plays/import", json={
            "import_id": "PD1R3", "work_code": "WD",
            "occurred_at": "2026-06-10T20:00:00+08:00", "amount": 100,
            "corrects_id": "PD1R2"})
        c.post("/statements/issue", json=d_period)
        led = c.get("/rightsholders/D/ledger").json()
        check("D recovered to 40", led["balance"] == 40, str(led))
        r = post(c, "/payment-reservations/LATE/confirm",
                 {"external_ref": "PAY-LATE"})
        check("late confirmed after recovery", r.status_code == 201
              and r.json()["reservation_status"] == "confirmed"
              and r.json()["balance_after"] == 10, r.text)

        # ---- 更早优先的确定性：两笔 pending，负额追补后余额只够更早的 ----
        # 重置一个干净 100% 权利人 E：余额 100，建 EARLY2=50、LATE2=40，
        # 再用负追补把余额压到 50。
        c.post("/rightsholders", json={"code": "E", "name": "戊"})
        c.post("/works", json={"code": "WE", "title": "戊作品"})
        c.post("/agreements", json={
            "work_code": "WE",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "E", "percent": "100"}]})
        e_period = {"start": "2026-08-01T00:00:00+08:00",
                    "end": "2026-09-01T00:00:00+08:00"}
        c.post("/plays/import", json={
            "import_id": "PE1", "work_code": "WE",
            "occurred_at": "2026-08-10T20:00:00+08:00", "amount": 100})
        c.post("/statements/issue", json=e_period)
        check("E balance 100",
              c.get("/rightsholders/E/ledger").json()["balance"] == 100)
        check(post(c, "/payment-reservations",
                   {"holder_code": "E", "instruction_ref": "EARLY2",
                    "amount": 50}).status_code == 201, "EARLY2 created")
        check(post(c, "/payment-reservations",
                   {"holder_code": "E", "instruction_ref": "LATE2",
                    "amount": 40}).status_code == 201, "LATE2 created")
        # 负额追补冲减 50：PE1 100 -> 50，余额 100 -> 50
        c.post("/plays/import", json={
            "import_id": "PE1R1", "work_code": "WE",
            "occurred_at": "2026-08-10T20:00:00+08:00", "amount": 50,
            "corrects_id": "PE1"})
        c.post("/statements/issue", json=e_period)
        check("E balance 50 after negative supplement",
              c.get("/rightsholders/E/ledger").json()["balance"] == 50)
        # 确认更晚的 LATE2(40)：更早 pending EARLY2=50 必须留足，
        # required = 50 + 40 = 90 > 50 => 拒绝，LATE2 保持 pending。
        r = post(c, "/payment-reservations/LATE2/confirm",
                 {"external_ref": "PAY-LATE2"})
        check("later confirm must reserve for earlier", r.status_code == 409
              and r.json()["error"] == "reservation_exceeds_balance"
              and r.json()["detail"]["earlier_pending_reservations"] == 50
              and r.json()["detail"]["required"] == 90
              and r.json()["detail"]["balance"] == 50, r.text)
        check("LATE2 pending unchanged",
              c.get("/payment-reservations/LATE2").json()
              ["reservation_status"] == "pending")
        # 更早的 EARLY2 只须覆盖自身 50（更早 pending=0）=> 余额 50 恰好确认
        r = post(c, "/payment-reservations/EARLY2/confirm",
                 {"external_ref": "PAY-EARLY2"})
        check("EARLY2 confirmed with priority", r.status_code == 201
              and r.json()["balance_after"] == 0, r.text)
        # EARLY2 确认后 LATE2 仍 pending（确认更早不改更晚指令），随后取消释放
        check("LATE2 still pending after early confirmed",
              c.get("/payment-reservations/LATE2").json()
              ["reservation_status"] == "pending")
        r = post(c, "/payment-reservations/LATE2/cancel")
        check("LATE2 cancel releases", r.status_code == 200
              and r.json()["reservation_status"] == "cancelled"
              and r.json()["reserved_pending"] == 0, r.text)

        # ---- 退回继续加回余额（且不改变待确认预留），加回后可供确认 ----
        # 对 B 的 PAY-RB1（30）登记全额退回 30：余额 10 -> 40，
        # 不触碰任何预留。
        r = post(c, "/refunds",
                 {"payment_ref": "PAY-RB1", "refund_ref": "RF-RB1", "amount": 30})
        check("refund adds balance back", r.status_code == 201
              and r.json()["status"] == "created"
              and r.json()["balance_after"] == 40, r.text)
        led = c.get("/rightsholders/B/ledger").json()
        check("B ledger refunded", led["paid_total"] == 30
              and led["refunded_total"] == 30 and led["balance"] == 40, str(led))

        # ---- 历史指令与付款不可改写（DB 触发器强制） ----
        with db.pool.connection() as db_conn:
            rid = db_conn.execute(
                "SELECT id FROM payment_reservations WHERE instruction_ref = %s",
                ("R1",)).fetchone()["id"]
            blocked = {}
            for op, sql, args in [
                ("upd_amount",
                 "UPDATE payment_reservations SET amount = 1 WHERE id = %s", (rid,)),
                ("upd_ref",
                 "UPDATE payment_reservations SET instruction_ref = 'X' WHERE id = %s",
                 (rid,)),
                ("del",
                 "DELETE FROM payment_reservations WHERE id = %s", (rid,)),
                ("reopen",
                 "UPDATE payment_reservations SET status = 'pending' WHERE id = %s",
                 (rid,)),
            ]:
                try:
                    db_conn.execute(sql, args)
                    blocked[op] = False
                except psycopg.Error:
                    blocked[op] = True
            # pending 指令改金额也必须被挡
            pid_pending = db_conn.execute(
                "SELECT id FROM payment_reservations WHERE instruction_ref = %s",
                ("R3",)).fetchone()["id"]
            try:
                db_conn.execute(
                    "UPDATE payment_reservations SET amount = 123 WHERE id = %s",
                    (pid_pending,))
                blocked["upd_cancelled_amount"] = False
            except psycopg.Error:
                blocked["upd_cancelled_amount"] = True
            # 付款仍不可改/删（原触发器）
            try:
                db_conn.execute("UPDATE payments SET amount = 1 WHERE id = %s",
                                (payment_id_r1,))
                blocked["upd_payment"] = False
            except psycopg.Error:
                blocked["upd_payment"] = True
        check("reservations immutable", all(blocked.values()), str(blocked))

        # ---- 单条指令查询 ----
        r = c.get("/payment-reservations/R1")
        check("get reservation", r.status_code == 200
              and r.json()["reservation_status"] == "confirmed"
              and r.json()["payment_ref"] == "PAY-R1"
              and r.json()["status"] == "confirmed", r.text)
        r = c.get("/payment-reservations/NOPE")
        check("get reservation 404", r.status_code == 404
              and r.json()["error"] == "reservation_not_found", r.text)

        # ---- 并发：同一指令号并发创建，恰好一条 pending，其余幂等 ----
        # C：新权利人，给足余额
        c.post("/rightsholders", json={"code": "C", "name": "丙"})
        c.post("/works", json={"code": "W2", "title": "丙作品"})
        c.post("/agreements", json={
            "work_code": "W2",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00",
            "shares": [{"holder_code": "C", "percent": "100"}]})
        c.post("/plays/import", json={
            "import_id": "PC1", "work_code": "W2",
            "occurred_at": "2026-03-10T20:00:00+08:00", "amount": 1000})
        c.post("/statements/issue", json={
            "start": "2026-03-01T00:00:00+08:00",
            "end": "2026-04-01T00:00:00+08:00"})
        check("C balance 1000",
              c.get("/rightsholders/C/ledger").json()["balance"] == 1000)

        create_results = []

        def fire_create():
            rr = post(c, "/payment-reservations",
                      {"holder_code": "C", "instruction_ref": "RC-X", "amount": 50})
            create_results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_create) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ids = {b["reservation_id"] for _, b in create_results}
        n_created = sum(1 for s, b in create_results if b["status"] == "created")
        n_dup = sum(1 for s, b in create_results if b["status"] == "duplicate")
        check("concurrent create serialized",
              len(ids) == 1 and n_created == 1 and n_dup == 7
              and all(s in (200, 201) for s, _ in create_results),
              f"{[(s, b['status']) for s, b in create_results]}")
        check("C reserved 50 once",
              c.get("/rightsholders/C/ledger").json()["reserved_total"] == 50)

        # 并发确认同一指令（同流水号）：恰好登记一笔付款，其余幂等
        confirm_results = []

        def fire_confirm():
            rr = post(c, "/payment-reservations/RC-X/confirm",
                      {"external_ref": "PAY-RC-X"})
            confirm_results.append((rr.status_code, rr.json()))

        ts = [threading.Thread(target=fire_confirm) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        pay_ids = {b["payment_id"] for _, b in confirm_results}
        n_c = sum(1 for s, b in confirm_results if b["status"] == "created")
        n_d = sum(1 for s, b in confirm_results if b["status"] == "duplicate")
        check("concurrent confirm serialized",
              len(pay_ids) == 1 and n_c == 1 and n_d == 7
              and {s for s, _ in confirm_results} <= {200, 201},
              f"{[(s, b['status']) for s, b in confirm_results]}")
        led = c.get("/rightsholders/C/ledger").json()
        check("C one payment, reservation closed",
              led["paid_total"] == 50 and led["reserved_total"] == 0
              and led["balance"] == 950, str(led))

        # 并发：直接付款与预留确认抢同一外部流水号，恰一笔成功，绝不重复入账
        # 再给 C 预留 RC-Y=50，两线程分别用同流水号 PAY-RACE：
        # 一个走确认，一个走直接付款（C 余额 950、无 pending，直接付款额度足够）。
        check(post(c, "/payment-reservations",
                   {"holder_code": "C", "instruction_ref": "RC-Y",
                    "amount": 50}).status_code == 201, "RC-Y created")
        race = {"created": [], "conflict": [], "other": []}

        def fire_confirm_y():
            rr = post(c, "/payment-reservations/RC-Y/confirm",
                      {"external_ref": "PAY-RACE"})
            if rr.status_code == 201:
                race["created"].append(("confirm", rr.json()["payment_id"]))
            elif rr.status_code == 409:
                race["conflict"].append(("confirm", rr.json()["error"]))
            else:
                race["other"].append(("confirm", rr.status_code, rr.text))

        def fire_direct_y():
            rr = post(c, "/payments",
                      {"holder_code": "C", "external_ref": "PAY-RACE", "amount": 50})
            if rr.status_code == 201:
                race["created"].append(("direct", rr.json()["payment_id"]))
            elif rr.status_code == 409:
                race["conflict"].append(("direct", rr.json()["error"]))
            else:
                race["other"].append(("direct", rr.status_code, rr.text))

        t1 = threading.Thread(target=fire_confirm_y)
        t2 = threading.Thread(target=fire_direct_y)
        t1.start(); t2.start(); t1.join(); t2.join()
        check("confirm vs direct same ref one wins",
              not race["other"] and len(race["created"]) == 1
              and len(race["conflict"]) == 1, str(race))
        led = c.get("/rightsholders/C/ledger").json()
        # 无论谁赢，PAY-RACE 只有一笔 50；总付款 100，余额 900
        check("no double payment after ref race",
              led["paid_total"] == 100 and led["balance"] == 900
              and len([p for p in led["payments"]
                       if p["external_ref"] == "PAY-RACE"]) == 1, str(led))
        # 输的若是 confirm，则 RC-Y 仍 pending（可再用别的流水号确认或取消）
        rc_y = c.get("/payment-reservations/RC-Y").json()
        check("RC-Y consistent after race",
              rc_y["reservation_status"] in ("pending", "confirmed"),
              str(rc_y))
        if rc_y["reservation_status"] == "pending":
            # pending 说明直接付款赢了；可用额应被 RC-Y 占用 50
            check("RC-Y pending still reserves",
                  rc_y["reserved_pending"] == 50
                  and rc_y["available"] == led["balance"] - 50, str(rc_y))
            # 取消后释放；直接付款赢的流水号现在被占用，再确认同号仍 409
            r = post(c, "/payment-reservations/RC-Y/confirm",
                     {"external_ref": "PAY-RACE"})
            check("RC-Y cannot steal occupied ref", r.status_code == 409
                  and r.json()["error"] == "payment_ref_conflict", r.text)
            check(post(c, "/payment-reservations/RC-Y/cancel").status_code == 200,
                  "RC-Y cancelled")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        raise SystemExit(1)
    print("ALL RESERVATION INTEGRATION CHECKS PASSED")
finally:
    server._cleanup()
    import shutil
    shutil.rmtree(_pgdata.parent, ignore_errors=True)
