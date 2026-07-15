"""UXF-007: translation preflight summaries without UI dependencies."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from ..domain.project import TranslationProject
from ..domain.translation import TranslationOptions


class PreflightSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    severity: PreflightSeverity
    message: str
    line_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class PreflightReport:
    action: str
    file_name: str
    file_type: str
    target_language: str
    provider: str
    model_name: str
    total_lines: int
    translated_lines: int
    pending_lines: int
    manually_edited_lines: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost: float | None
    glossary_terms: int
    image_count: int
    issues: tuple[PreflightIssue, ...]

    @property
    def can_start(self) -> bool:
        return not any(issue.severity is PreflightSeverity.ERROR for issue in self.issues)


def build_preflight_report(
    project: TranslationProject,
    options: TranslationOptions,
    *,
    action: str = "补译",
    provider: str = "",
    glossary_terms: Iterable[object] = (),
    image_count: int = 0,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    long_line_threshold: int = 2_000,
) -> PreflightReport:
    """Summarize a proposed translation without exposing sensitive source text."""
    pending = project.get_pending_indices()
    source_characters = sum(len(project.original_lines[index]) for index in pending)
    # A conservative, provider-neutral estimate. Provider tokenizers can replace it later.
    input_tokens = (source_characters + 3) // 4
    output_tokens = input_tokens
    estimated_cost = None
    if input_price_per_million is not None and output_price_per_million is not None:
        estimated_cost = (
            input_tokens * input_price_per_million
            + output_tokens * output_price_per_million
        ) / 1_000_000

    issues: list[PreflightIssue] = []
    long_lines = tuple(
        index for index in pending
        if len(project.original_lines[index]) > long_line_threshold
    )
    if long_lines:
        issues.append(PreflightIssue(
            "long_source_lines", PreflightSeverity.WARNING,
            f"{len(long_lines)} 行超过 {long_line_threshold} 字符，可能需要拆分。",
            long_lines,
        ))
    if not pending:
        issues.append(PreflightIssue(
            "nothing_to_translate", PreflightSeverity.INFO,
            "没有待翻译内容。",
        ))
    if not options.model_name.strip():
        issues.append(PreflightIssue(
            "missing_model", PreflightSeverity.ERROR, "尚未选择翻译模型。"
        ))
    if not options.target_language.strip():
        issues.append(PreflightIssue(
            "missing_target_language", PreflightSeverity.ERROR, "尚未选择目标语言。"
        ))

    return PreflightReport(
        action=action,
        file_name=Path(project.source_path).name,
        file_type=project.file_type,
        target_language=options.target_language,
        provider=provider,
        model_name=options.model_name,
        total_lines=len(project.original_lines),
        translated_lines=project.translated_count,
        pending_lines=len(pending),
        manually_edited_lines=len(project.manually_edited_indices),
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        estimated_cost=estimated_cost,
        glossary_terms=sum(1 for _ in glossary_terms),
        image_count=image_count,
        issues=tuple(issues),
    )
