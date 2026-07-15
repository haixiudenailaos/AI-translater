"""UXF-008: deterministic post-translation quality inspection."""

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from enum import Enum
import re
from typing import Iterable

from ..domain.project import TranslationProject

_LINE_MARKER_RE = re.compile(r"\[LINE_\d+\]")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


class QualitySeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class QualityIssueType(str, Enum):
    MISSING_TRANSLATION = "missing_translation"
    UNCHANGED_TRANSLATION = "unchanged_translation"
    INTERNAL_MARKER = "internal_marker"
    NUMBER_MISMATCH = "number_mismatch"
    LENGTH_ANOMALY = "length_anomaly"
    REPEATED_TRANSLATION = "repeated_translation"
    GLOSSARY_MISS = "glossary_miss"


@dataclass(frozen=True)
class QualityIssue:
    issue_id: str
    issue_type: QualityIssueType
    severity: QualitySeverity
    line_index: int
    message: str


@dataclass(frozen=True)
class QualityReport:
    total_lines: int
    checked_lines: int
    issues: tuple[QualityIssue, ...]

    @property
    def error_count(self) -> int:
        return sum(issue.severity is QualitySeverity.ERROR for issue in self.issues)

    def to_dict(self) -> dict:
        return {
            "total_lines": self.total_lines,
            "checked_lines": self.checked_lines,
            "error_count": self.error_count,
            "issues": [
                {
                    **asdict(issue),
                    "issue_type": issue.issue_type.value,
                    "severity": issue.severity.value,
                }
                for issue in self.issues
            ],
        }


def inspect_quality(
    project: TranslationProject,
    *,
    glossary_terms: Iterable[tuple[str, str]] = (),
    ignored_issue_ids: Iterable[str] = (),
) -> QualityReport:
    """Check deterministic line-level quality signals without calling an LLM."""
    ignored = set(ignored_issue_ids)
    glossary = tuple((source, target) for source, target in glossary_terms if source and target)
    issues: list[QualityIssue] = []
    targets_seen: dict[str, int] = {}

    def add(issue_type: QualityIssueType, severity: QualitySeverity, index: int, message: str) -> None:
        issue_id = f"{issue_type.value}:{index}"
        if issue_id not in ignored:
            issues.append(QualityIssue(issue_id, issue_type, severity, index, message))

    for index, source in enumerate(project.original_lines):
        if not source or not source.strip():
            continue
        target = project.translated_lines[index] if index < len(project.translated_lines) else ""
        if not target or not target.strip():
            add(QualityIssueType.MISSING_TRANSLATION, QualitySeverity.ERROR, index, "原文非空但译文为空。")
            continue

        normalized_source = "".join(source.split()).casefold()
        normalized_target = "".join(target.split()).casefold()
        if len(normalized_source) >= 4 and SequenceMatcher(None, normalized_source, normalized_target).ratio() >= 0.92:
            add(QualityIssueType.UNCHANGED_TRANSLATION, QualitySeverity.WARNING, index, "译文与原文高度相似，可能未翻译。")
        if _LINE_MARKER_RE.search(target):
            add(QualityIssueType.INTERNAL_MARKER, QualitySeverity.ERROR, index, "译文残留内部行号标记。")
        if sorted(_NUMBER_RE.findall(source)) != sorted(_NUMBER_RE.findall(target)):
            add(QualityIssueType.NUMBER_MISMATCH, QualitySeverity.WARNING, index, "原文和译文中的数字不一致。")
        if len(source) >= 20 and (len(target) < len(source) * 0.15 or len(target) > len(source) * 6):
            add(QualityIssueType.LENGTH_ANOMALY, QualitySeverity.WARNING, index, "译文长度与原文差异异常。")
        previous_index = targets_seen.get(normalized_target)
        if previous_index is not None and len(normalized_target) >= 8:
            add(QualityIssueType.REPEATED_TRANSLATION, QualitySeverity.WARNING, index, f"译文与第 {previous_index + 1} 行重复。")
        else:
            targets_seen[normalized_target] = index
        for source_term, target_term in glossary:
            if source_term in source and target_term not in target:
                add(QualityIssueType.GLOSSARY_MISS, QualitySeverity.WARNING, index, f"术语“{source_term}”未使用指定译法“{target_term}”。")

    return QualityReport(
        total_lines=len(project.original_lines),
        checked_lines=sum(1 for line in project.original_lines if line and line.strip()),
        issues=tuple(issues),
    )
