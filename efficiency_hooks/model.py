from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    platform: str
    session_id: str
    agent_id: str
    call_id: str
    phase: str
    cwd: str
    tool: str
    args: dict = field(default_factory=dict)
    status: str = 'unknown'
    error_signature: str = ''


@dataclass(frozen=True)
class Decision:
    action: str = 'allow'
    reason: str = ''
