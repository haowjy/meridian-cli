from typing import Any, Self

class BaseEvent:
    type: str

    def __init__(self, **data: Any) -> None: ...
    def model_dump_json(
        self,
        *,
        by_alias: bool = ...,
        exclude_none: bool = ...,
    ) -> str: ...
    @classmethod
    def model_validate(cls, obj: object) -> Self: ...

CustomEvent = BaseEvent
ReasoningMessageContentEvent = BaseEvent
ReasoningMessageEndEvent = BaseEvent
ReasoningMessageStartEvent = BaseEvent
RunErrorEvent = BaseEvent
RunFinishedEvent = BaseEvent
RunStartedEvent = BaseEvent
StepFinishedEvent = BaseEvent
TextMessageContentEvent = BaseEvent
TextMessageEndEvent = BaseEvent
TextMessageStartEvent = BaseEvent
ToolCallArgsEvent = BaseEvent
ToolCallEndEvent = BaseEvent
ToolCallResultEvent = BaseEvent
ToolCallStartEvent = BaseEvent
