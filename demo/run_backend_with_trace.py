# -*- coding: utf-8 -*-
"""WP3 diagnostic backend: starts the REAL server.py app but logs any
exception escaping AgentRouter's single-agent adapter (test/diagnostic scope
only; used once to root-cause the Case B step failure)."""

import os
import sys
import traceback

sys.path.insert(0, r"D:\PythonProject\Local_Agent")

import server  # noqa: E402
from core.runtime import agent_adapter_factory  # noqa: E402

_original_execute = agent_adapter_factory.AgentRouterSingleAgentAdapter.execute


def _traced_execute(self, request, run_context):
    try:
        return _original_execute(self, request, run_context)
    except BaseException:
        print("[wp3-trace] EXCEPTION escaping adapter:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        raise


def _install_router_trace():
    """Wrap AgentRouter.complete_single_agent to surface the inner failure."""
    import core.agent_router as ar

    original = ar.AgentRouter.complete_single_agent

    def traced(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except BaseException:
            print("[wp3-router-trace] EXCEPTION:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            raise

    ar.AgentRouter.complete_single_agent = traced


agent_adapter_factory.AgentRouterSingleAgentAdapter.execute = _traced_execute
_install_router_trace()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        server.app, host=server.settings.api_host, port=server.settings.api_port
    )
