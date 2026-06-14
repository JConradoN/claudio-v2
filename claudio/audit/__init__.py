from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudio.config import Config

from claudio.audit.agent_mesh import AgentMeshAudit
from claudio.audit.runlog import RunLog


class AuditLog:
    """Fachada que escreve em ambos: runlog arquivo + agent_mesh audit_log."""

    def __init__(self, config: "Config") -> None:
        self._runlog = RunLog(config)
        self._mesh = AgentMeshAudit(config)

    def log(self, event: str, data: dict, level: str = "info") -> None:
        self._runlog.log(event, data, level)
        self._mesh.log(event, data, level)

    def close(self) -> None:
        self._runlog.close()
