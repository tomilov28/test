import enum


class LifecycleState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class RequestOutcome(str, enum.Enum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class WorkflowEngine(str, enum.Enum):
    OPERATON = "OPERATON"
    FLOWABLE = "FLOWABLE"


class WorkItemState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CommandType(str, enum.Enum):
    START_PROCESS = "START_PROCESS"
    COMPLETE_TASK = "COMPLETE_TASK"
    CANCEL_PROCESS = "CANCEL_PROCESS"


class CommandState(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"
