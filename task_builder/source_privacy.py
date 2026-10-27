from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import SourceBundle, ValidationIssue, ValidationReport

_PUBLIC_FILES = ("instruction.md", "task.toml", "README.md")
_REPOSITORY_URL = re.compile(
    r"(?i)(?:https?://|git@)(?:www\.)?(?:github|gitlab|bitbucket)\.[^/\s]+/[^\s<>'\"`]+"
)
_DOI = re.compile(r"(?i)\b10\.\d{4,9}/[-._;/:a-z0-9]+")
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<value>.+?)\s*#*\s*$", re.MULTILINE)
# 引用行中的期刊名（如 "Nature Methods 20, 1323–1335 (2023)" 或 "Genome Biol. 21, 57 (2020)"）。
_JOURNAL_CITATION = re.compile(
    r"(?m)(?:^|[.;]\s+)(?P<value>[A-Z][A-Za-z.&' -]{3,80}?)\.?\s+\d{1,4}\s*[,:]\s*[A-Za-z]?\d+[^()\n]{0,20}\((?:19|20)\d{2}\)"
)

# 单个纯字母普通词（如 Results、Print）不构成来源泄露证据：这类 token 更可能
# 来自 PDF 字体字典等元数据噪音，而非真实标题、作者或期刊名。DOI、仓库地址、
# 多词标题和含标点的期刊名不受此规则影响，仍会被完整保留。
_SINGLE_WORD = re.compile(r"(?i)^[a-z]+$")


def _is_common_word(value: str) -> bool:
    return bool(_SINGLE_WORD.match(value))


@dataclass(frozen=True)
class SourceMarker:
    kind: str
    value: str


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\\", "/").strip()).casefold()


def _clean_marker(value: str) -> str:
    value = value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\,", ",")
    value = value.strip().strip("\"'<>[](){}.,;:")
    return re.sub(r"\s+", " ", value)


def _repository_base(value: str) -> str:
    value = _clean_marker(value)
    value = re.split(r"[#?]", value, maxsplit=1)[0]
    match = re.match(r"(?i)(https?://[^/]+/[^/\s]+/[^/\s]+)", value)
    if match:
        base = match.group(1).split("@", 1)[0]
        return base.removesuffix(".git")
    return value


def _add_marker(markers: set[SourceMarker], kind: str, value: str) -> None:
    cleaned = _clean_marker(value)
    if len(cleaned) < 4:
        return
    # 纯单个普通单词不作为泄露依据（Results、Print、Variational 等元数据噪音）。
    if _is_common_word(cleaned):
        return
    markers.add(SourceMarker(kind, cleaned))


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if b"\x00" in raw[:4096]:
        return ""
    return raw.decode("utf-8", errors="ignore")


def _metadata_files(source: SourceBundle) -> list[Path]:
    candidates = [
        source.code / ".git" / "config",
        source.code / "README.md",
        source.code / "README.rst",
        source.code / "README.txt",
        source.code / "CITATION.cff",
        source.code / "CITATION.md",
        source.code / "CITATION.txt",
        source.code / "pyproject.toml",
        source.code / "setup.py",
        source.code / "DESCRIPTION",
    ]
    return [path for path in candidates if path.is_file()]


def _link_urls(value: object) -> list[str]:
    """递归取出 links.json 中的所有 URL 字符串，不依赖具体字段名。"""
    if isinstance(value, str):
        return [value] if re.match(r"(?i)(?:https?://|git@|doi:|10\.\d{4,9}/)", value.strip()) else []
    if isinstance(value, dict):
        return [url for item in value.values() for url in _link_urls(item)]
    if isinstance(value, list):
        return [url for item in value for url in _link_urls(item)]
    return []


def _paper_title(text: str) -> str | None:
    match = _MARKDOWN_HEADING.search(text)
    if match:
        return match.group("value")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:300] or None


def source_markers(source: SourceBundle) -> tuple[SourceMarker, ...]:
    markers: set[SourceMarker] = set()
    for path in (source.source_dir, source.context, source.data, source.code):
        resolved = path.resolve()
        _add_marker(markers, "input path", str(resolved))
        _add_marker(markers, "input path", resolved.as_posix())

    paper_text = _read_text(source.context / "text.md")
    title = _paper_title(paper_text)
    if title:
        _add_marker(markers, "paper metadata", title)
    # 只在正文开头区域找论文自身 DOI 与期刊，避免把参考文献里的其他论文都当成来源。
    head = paper_text[:20000]
    for match in _DOI.finditer(head):
        _add_marker(markers, "DOI", match.group(0))
        _add_marker(markers, "DOI", "https://doi.org/" + match.group(0))
    for match in _JOURNAL_CITATION.finditer(head):
        _add_marker(markers, "journal", match.group("value"))

    try:
        links = json.loads(_read_text(source.context / "links.json") or "null")
    except ValueError:
        links = None
    for url in _link_urls(links):
        if _REPOSITORY_URL.match(url):
            _add_marker(markers, "original code repository", _repository_base(url))
        for match in _DOI.finditer(url):
            _add_marker(markers, "DOI", match.group(0))
            _add_marker(markers, "DOI", "https://doi.org/" + match.group(0))

    for path in _metadata_files(source):
        text = _read_text(path)
        for match in _REPOSITORY_URL.finditer(text):
            _add_marker(markers, "original code repository", _repository_base(match.group(0)))
        for match in _DOI.finditer(text):
            _add_marker(markers, "DOI", match.group(0))
            _add_marker(markers, "DOI", "https://doi.org/" + match.group(0))

    return tuple(sorted(markers, key=lambda marker: (marker.kind, marker.value.casefold())))


def public_description_paths(draft: Path) -> tuple[Path, ...]:
    return tuple(path for path in (draft / name for name in _PUBLIC_FILES) if path.is_file())


def validate_public_description(draft: Path, source: SourceBundle) -> ValidationReport:
    public_files = public_description_paths(draft)
    markers = source_markers(source)
    issues: list[ValidationIssue] = []
    for path in public_files:
        text = _normalise(_read_text(path))
        if not text:
            continue
        for marker in markers:
            marker_text = _normalise(marker.value)
            if len(marker_text) < 4:
                continue
            if marker_text in text:
                issues.append(
                    ValidationIssue(
                        "source-privacy",
                        "用户可见任务描述泄露了输入来源信息",
                        f"{path.name} 包含 {marker.kind}: {marker.value}",
                    )
                )
    return ValidationReport(
        "source-privacy",
        not issues,
        tuple(issues),
        {"checked_files": [str(path) for path in public_files]},
    )
