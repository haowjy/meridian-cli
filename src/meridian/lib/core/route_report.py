"""Mars selection-report wire contract. Diagnostic history, never a retry queue."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ReportObject(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")


class TargetSourceReport(_ReportObject):
    field: Literal["targets", "managed_root", "none"]
    origin: Literal["project", "local", "unset"]
    path: str | None


class ScopeReport(_ReportObject):
    mode: Literal["only", "unrestricted"]
    enabled_harnesses: list[str]
    excluded_harnesses: list[str]
    target_source: TargetSourceReport


class AssessmentReport(_ReportObject):
    harness: str
    installed: bool
    verdict: Literal["eligible", "unverified", "blocked"]


class ModelAttemptReport(_ReportObject):
    model_token: str
    canonical_model: str
    model_source: str
    assessments: list[AssessmentReport]


class SelectedAssessment(_ReportObject):
    attempt_index: int = Field(ge=0)
    assessment_index: int = Field(ge=0)


class RouteDecisionReport(_ReportObject):
    version: int = Field(ge=2, le=2)
    scope: ScopeReport
    model_attempts: list[ModelAttemptReport]
    selected: SelectedAssessment | None
    outcome: Literal["selected", "exhausted", "explicit_constraint_error"]

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if (self.outcome == "selected") != (self.selected is not None):
            raise ValueError("outcome and selected pointer disagree")
        if self.selected is None:
            return self
        if self.selected.attempt_index >= len(self.model_attempts):
            raise ValueError("selected model attempt is out of bounds")
        attempt = self.model_attempts[self.selected.attempt_index]
        if self.selected.assessment_index >= len(attempt.assessments):
            raise ValueError("selected assessment is out of bounds")
        assessment = attempt.assessments[self.selected.assessment_index]
        if assessment.verdict == "blocked" or not assessment.installed:
            raise ValueError("selected assessment is blocked or not installed")
        if assessment.harness in self.scope.excluded_harnesses or (
            self.scope.mode == "only" and assessment.harness not in self.scope.enabled_harnesses
        ):
            raise ValueError("selected harness is outside report scope")
        return self
