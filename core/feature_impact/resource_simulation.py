"""确定性资源生命周期；内部轨迹与黑盒业务结果分开返回。"""
from copy import deepcopy


def validate_operations(operations):
    """只检查公开操作格式；规划和审核阶段不得运行模拟器。"""
    if not isinstance(operations, list) or not 1 <= len(operations) <= 40:
        raise ValueError("SIMULATION_INPUT_INVALID")
    for op in operations:
        if not isinstance(op, dict) or not isinstance(op.get("request_id"), str) or not op["request_id"]:
            raise ValueError("SIMULATION_OPERATION_INVALID")
        action = op.get("action")
        fields = {"action", "request_id"}
        if action == "SUBMIT":
            fields.add("payload")
        elif action == "DELIVER":
            fields.add("mode")
            if op.get("mode") not in {"SHARED", "COPY", "INDEPENDENT"}:
                raise ValueError("SIMULATION_DELIVERY_INVALID")
        elif action not in {"COMPLETE", "CONSUME"}:
            raise ValueError("SIMULATION_ACTION_INVALID")
        if set(op) != fields:
            raise ValueError("SIMULATION_OPERATION_INVALID")


def verify(expected, reports):
    """独立业务判据只比较请求输入与实际上报。"""
    return "PASS" if reports and all(r["payload"] == expected[r["request_id"]] for r in reports) else "FAIL"


def execute(policy, operations):
    validate_operations(operations)
    if policy not in {"OLD", "NEW"}:
        raise ValueError("SIMULATION_INPUT_INVALID")
    slot = dict(slot_id="public", generation=0, payload=None, free=True, done=False)
    jobs, consumers, expected, reports, trace, waiting = {}, {}, {}, [], [], []

    def reclaim():
        if slot["done"] and (policy == "NEW" or not any(c["shared"] and not c["consumed"] for c in consumers.values())):
            slot["free"] = True
            trace.append(dict(event="RELEASE", generation=slot["generation"]))
            if waiting:
                submit(waiting.pop(0))

    def submit(op):
        key = op["request_id"]
        if not slot["free"]:
            waiting.append(op)
            trace.append(dict(event="WAIT_RESOURCE", request_id=key))
            return
        slot.update(generation=slot["generation"] + 1, payload=deepcopy(op["payload"]), free=False, done=False)
        jobs[key] = slot["generation"]
        trace.append(dict(event="ALLOCATE_WRITE", request_id=key, **deepcopy(slot)))

    for op in operations:
        kind, key = op.get("action"), op.get("request_id")
        if not isinstance(key, str) or not key or set(op) - {"action", "request_id", "payload", "mode"}:
            raise ValueError("SIMULATION_OPERATION_INVALID")
        if kind == "SUBMIT":
            if key in expected or "payload" not in op:
                raise ValueError("SIMULATION_REQUEST_INVALID")
            expected[key] = deepcopy(op["payload"])
            submit(op)
        elif kind == "DELIVER":
            mode = op.get("mode")
            if key not in jobs or jobs[key] != slot["generation"] or key in consumers or mode not in {"SHARED", "COPY", "INDEPENDENT"}:
                raise ValueError("SIMULATION_DELIVERY_INVALID")
            consumers[key] = dict(shared=mode == "SHARED", consumed=False, payload=deepcopy(slot["payload"]))
            if mode == "INDEPENDENT":
                consumers[key]["resource"] = dict(slot_id="private-" + key, generation=1, payload=deepcopy(slot["payload"]))
                trace.append(dict(event="ALLOCATE_PRIVATE_WRITE", request_id=key, **deepcopy(consumers[key]["resource"])))
            trace.append(dict(event="DELIVER", request_id=key, mode=mode))
        elif kind == "COMPLETE":
            if key not in jobs or jobs[key] != slot["generation"]:
                raise ValueError("SIMULATION_COMPLETION_INVALID")
            jobs[key] = None
            slot["done"] = True
            trace.append(dict(event="PRODUCER_DONE", request_id=key))
            reclaim()
        elif kind == "CONSUME":
            c = consumers.get(key)
            if c is None or c["consumed"]:
                raise ValueError("SIMULATION_CONSUMPTION_INVALID")
            payload = deepcopy(slot["payload"] if c["shared"] else c["resource"]["payload"] if "resource" in c else c["payload"])
            c["consumed"] = True
            reports.append(dict(request_id=key, payload=payload))
            trace.append(dict(event="CONSUME_REPORT", request_id=key, payload=payload))
            reclaim()
        else:
            raise ValueError("SIMULATION_ACTION_INVALID")
    if not reports or any(not c["consumed"] for c in consumers.values()):
        raise ValueError("SIMULATION_REPORT_INCOMPLETE")
    result = dict(execution_version="resource-lifecycle.v1", expected=expected, reports=reports,
        verdict=verify(expected, reports), pending_requests=[o["request_id"] for o in waiting])
    return result, trace


def controls():
    """公开业务操作对照，不携带隐藏槽位或故障标签。"""
    def sequence(mode="SHARED", reuse=True, early=False):
        ops = [dict(action="SUBMIT", request_id="statistics", payload={"count": 17}),
            dict(action="DELIVER", request_id="statistics", mode=mode)]
        if early:
            ops.append(dict(action="CONSUME", request_id="statistics"))
        ops.append(dict(action="COMPLETE", request_id="statistics"))
        if reuse:
            ops.append(dict(action="SUBMIT", request_id="diagnostic", payload={"count": 91}))
        if not early:
            ops.append(dict(action="CONSUME", request_id="statistics"))
        return ops
    return {"C1": dict(policy="OLD", operations=sequence()), "C2": dict(policy="NEW", operations=sequence(reuse=False)),
        "C3": dict(policy="NEW", operations=sequence()), "C4": dict(policy="NEW", operations=sequence("COPY")),
        "C5": dict(policy="NEW", operations=sequence("INDEPENDENT")), "C6": dict(policy="NEW", operations=sequence(early=True))}
