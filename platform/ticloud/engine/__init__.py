from .base import AgentEngine, BudgetExceeded, RunContext, RunResult
from .offline import OfflineEngine
from .ti_adapter import TiEngine

# Engine registry: job.engine value -> factory. Ti is the flagship engine;
# offline mirrors Ti's no-API-key demo mode and powers tests/demos.
ENGINES: dict[str, type[AgentEngine]] = {
    "offline": OfflineEngine,
    "ti": TiEngine,
}


def get_engine(name: str) -> AgentEngine:
    try:
        return ENGINES[name]()
    except KeyError:
        raise ValueError(f"unknown engine {name!r}; available: {sorted(ENGINES)}") from None


__all__ = [
    "AgentEngine",
    "BudgetExceeded",
    "RunContext",
    "RunResult",
    "OfflineEngine",
    "TiEngine",
    "ENGINES",
    "get_engine",
]
