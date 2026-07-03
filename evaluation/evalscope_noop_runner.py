"""EvalScope external no-op runner for scaffold/verifier smoke tests."""

from __future__ import annotations

from evalscope.agent.external.runners import AgentRunner, AgentRunResult, BridgeEndpoint, ExternalAgentTask, RunnerTimeoutError
from evalscope.api.agent import AgentEnvironment
from evalscope.api.registry import register_runner


@register_runner("noop")
class NoopRunner(AgentRunner):
    """Run a harmless command inside the sample environment and produce no patch."""

    framework: str = "noop"

    def __init__(self, **_: object) -> None:
        pass

    async def setup(self, env: AgentEnvironment) -> None:
        return None

    async def run(
        self,
        task: ExternalAgentTask,
        env: AgentEnvironment,
        bridge: BridgeEndpoint,
    ) -> AgentRunResult:
        result = await env.exec(
            ["bash", "-lc", "pwd > /tmp/evalscope-noop-runner.txt"],
            timeout=min(float(task.timeout or 60), 60.0),
        )
        if result.timed_out:
            raise RunnerTimeoutError("noop runner timed out")
        if result.returncode != 0:
            tail = ((result.stderr or "") + (result.stdout or "")).strip()[-1000:]
            raise RuntimeError(f"noop runner failed with code {result.returncode}: {tail}")
        return AgentRunResult(
            output="noop runner completed without modifying the repository",
            metrics={
                "wall_time": result.duration,
                "returncode": result.returncode,
            },
        )
