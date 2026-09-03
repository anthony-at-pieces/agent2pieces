from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import agent2pieces.scanners.base as scanner_base
from agent2pieces.models import SourceAgent, SourceDisposition
from agent2pieces.scanners.claude import scan_claude_root
from agent2pieces.scanners.codex import scan_codex_root
from agent2pieces.scanners.hermes import _stream_entries, scan_hermes_root

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sources" / "task002"


def copy_fixture(name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / name, target)
    return target


def disposition_pairs(result: object) -> set[tuple[SourceDisposition, str]]:
    return {(item.disposition, item.reason) for item in result.dispositions}  # type: ignore[attr-defined]


def assert_public_candidate_shape(unit: object) -> None:
    data = unit.candidate.model_dump(mode="json")  # type: ignore[attr-defined]
    assert set(data) == {
        "source_agent",
        "source_key",
        "source_path",
        "project_scope",
        "source_updated_at",
        "title",
        "markdown_body",
        "external_links",
        "source_hash",
        "payload_hash",
        "import_id",
    }
    assert len(unit.candidate_input_hash) == 64  # type: ignore[attr-defined]


def test_codex_fixture_has_disjoint_accepted_excluded_and_ignored_counts(
    tmp_path: Path,
) -> None:
    root = copy_fixture("codex", tmp_path)

    result = scan_codex_root(root)

    assert result.counts.model_dump() == {
        "discovered": 3,
        "accepted": 2,
        "excluded": 1,
        "quarantined": 0,
    }
    assert disposition_pairs(result) == {(SourceDisposition.EXCLUDED, "excluded_raw")}
    by_key = {unit.candidate.source_key: unit for unit in result.candidates}
    unit = by_key["valid.md"]
    assert_public_candidate_shape(unit)
    assert unit.candidate.source_agent is SourceAgent.CODEX
    assert unit.candidate.source_key == "valid.md"
    assert unit.candidate.title == "Codex fixture memory"
    assert unit.candidate.project_scope == "fixture-project"
    assert unit.candidate.source_updated_at == "2026-08-31T12:30:00Z"
    assert tuple(unit.candidate.external_links) == ("https://example.test/codex",)
    assert not unit.candidate.markdown_body.startswith("---")
    assert "project: fixture-project" not in unit.candidate.markdown_body
    assert ".hidden.md" in by_key


def test_claude_fixture_excludes_only_exact_index_and_user_type(tmp_path: Path) -> None:
    root = copy_fixture("claude", tmp_path) / "project-alpha" / "memory"
    index = root / "MEMORY.md"
    lowercase = root / "memory.md"
    lowercase.write_text("# Lowercase index is a topic\n", encoding="utf-8")
    lowercase_is_distinct = not lowercase.samefile(index)

    result = scan_claude_root(root)

    assert result.counts.model_dump() == {
        "discovered": 4 if lowercase_is_distinct else 3,
        "accepted": 2 if lowercase_is_distinct else 1,
        "excluded": 2,
        "quarantined": 0,
    }
    assert disposition_pairs(result) == {
        (SourceDisposition.EXCLUDED, "excluded_index"),
        (SourceDisposition.EXCLUDED, "excluded_user"),
    }
    by_key = {unit.candidate.source_key: unit for unit in result.candidates}
    topic = by_key["topic.md"].candidate
    assert topic.title == "Claude fixture memory"
    assert topic.project_scope == "alpha"
    assert tuple(topic.external_links) == ("https://pieces.app/",)
    if lowercase_is_distinct:
        assert by_key["memory.md"].candidate.title == "Lowercase index is a topic"


def test_hermes_fixture_splits_section_delimiters_and_ignores_profiles(tmp_path: Path) -> None:
    root = copy_fixture("hermes", tmp_path)

    result = scan_hermes_root(root)

    assert result.counts.model_dump() == {
        "discovered": 2,
        "accepted": 2,
        "excluded": 0,
        "quarantined": 0,
    }
    assert [unit.candidate.source_key for unit in result.candidates] == [
        "MEMORY.md#section=1",
        "MEMORY.md#section=2",
    ]
    assert [unit.candidate.title for unit in result.candidates] == [
        "Hermes fixture one",
        "Hermes fixture two",
    ]
    assert all(unit.candidate.project_scope == "hermes" for unit in result.candidates)


def test_codex_quarantines_malformed_invalid_empty_and_oversize_units(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    (root / "malformed.md").write_text("---\ntitle: never closed\n", encoding="utf-8")
    (root / "invalid.md").write_bytes(b"# invalid\n\xff")
    (root / "empty.md").write_text("---\nproject: x\n---\n\n", encoding="utf-8")
    (root / "boundary.md").write_bytes(b"x" * 65_536)
    (root / "oversize.md").write_bytes(b"x" * 65_537)
    (root / "ignored.MD").write_text("# ignored", encoding="utf-8")

    result = scan_codex_root(root)

    assert result.counts.model_dump() == {
        "discovered": 5,
        "accepted": 1,
        "excluded": 0,
        "quarantined": 4,
    }
    assert {item.reason for item in result.dispositions} == {
        "malformed",
        "invalid_utf8",
        "empty",
        "oversize",
    }


def test_claude_rejects_duplicate_or_nested_required_frontmatter(tmp_path: Path) -> None:
    root = tmp_path / "projects" / "project-one" / "memory"
    root.mkdir(parents=True)
    (root / "duplicate.md").write_text(
        "---\ntitle: one\ntitle: two\n---\nBody\n",
        encoding="utf-8",
    )
    (root / "nested.md").write_text(
        "---\ntype:\n  child: user\n---\nBody\n",
        encoding="utf-8",
    )
    (root / "unknown-nested.md").write_text(
        "---\nunknown:\n  child: retained-as-unsupported\n---\n# Accepted\nBody\n",
        encoding="utf-8",
    )

    result = scan_claude_root(root)

    assert result.counts.model_dump() == {
        "discovered": 3,
        "accepted": 1,
        "excluded": 0,
        "quarantined": 2,
    }
    assert {item.reason for item in result.dispositions} == {"malformed"}


def test_codex_raw_jsonl_is_classified_without_opening_its_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    raw = root / "session.jsonl"
    raw.write_text('{"secret":"body-must-not-be-read"}\n', encoding="utf-8")

    def reject_reads(path: Path, **_kwargs: object) -> bytes:
        if path == raw:
            raise AssertionError("raw JSONL body was opened")
        return path.read_bytes()

    monkeypatch.setattr("agent2pieces.scanners.base.read_binary", reject_reads)
    result = scan_codex_root(root)

    assert result.counts.model_dump() == {
        "discovered": 1,
        "accepted": 0,
        "excluded": 1,
        "quarantined": 0,
    }
    assert result.dispositions[0].reason == "excluded_raw"


def test_codex_reserved_names_and_skills_are_excluded_for_any_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "broad-codex-root"
    skills = root / "skills" / "private"
    skills.mkdir(parents=True)
    reserved = [root / "MEMORY.md", root / "memory_summary.md", root / "raw_memories.md"]
    for path in reserved:
        path.write_text("# Reserved\nPrivate index content.\n", encoding="utf-8")
    skill = skills / "SKILL.md"
    skill.write_text("# Skill\nPrivate instructions.\n", encoding="utf-8")
    accepted = root / "rollout.md"
    accepted.write_text("# Curated rollout\nPublic candidate.\n", encoding="utf-8")
    real_read = accepted.read_bytes

    def reject_reserved_reads(path: Path, **_kwargs: object) -> bytes:
        if path in {*reserved, skill}:
            raise AssertionError("reserved Codex content was opened")
        return real_read() if path == accepted else path.read_bytes()

    monkeypatch.setattr("agent2pieces.scanners.base.read_binary", reject_reserved_reads)

    result = scan_codex_root(root)

    assert [candidate.candidate.source_key for candidate in result.candidates] == [
        "rollout.md"
    ]
    assert {item.source_key for item in result.dispositions} == {
        "MEMORY.md",
        "memory_summary.md",
        "raw_memories.md",
    }
    assert all(item.reason == "excluded_index" for item in result.dispositions)


def test_claude_unquotes_user_type_and_quarantines_malformed_relevant_keys(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects" / "project-one" / "memory"
    root.mkdir(parents=True)
    (root / "quoted-user.md").write_text(
        '---\ntype: "user"\n---\n# Private profile\nBody\n',
        encoding="utf-8",
    )
    (root / "malformed-type.md").write_text(
        "---\ntype user\n---\n# Must quarantine\nBody\n",
        encoding="utf-8",
    )
    (root / "unterminated-type.md").write_text(
        '---\ntype: "user\n---\n# Must quarantine\nBody\n',
        encoding="utf-8",
    )

    result = scan_claude_root(root)

    assert result.counts.model_dump() == {
        "discovered": 3,
        "accepted": 0,
        "excluded": 1,
        "quarantined": 2,
    }
    assert disposition_pairs(result) == {
        (SourceDisposition.EXCLUDED, "excluded_user"),
        (SourceDisposition.QUARANTINED, "malformed"),
    }


def test_hermes_applies_size_per_nonempty_entry_and_container_encoding(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "MEMORY.md").write_text(
        "# valid\nBody\n\n§\n\n" + ("x" * 65_537) + "\n\n§\n\n# final\nBody",
        encoding="utf-8",
    )
    invalid = root / "nested" / "MEMORY.md"
    invalid.parent.mkdir()
    invalid.write_bytes(b"# invalid\n\xff")
    empty = root / "empty" / "MEMORY.md"
    empty.parent.mkdir()
    empty.write_text(" \n§\n \n", encoding="utf-8")

    result = scan_hermes_root(root)

    assert result.counts.model_dump() == {
        "discovered": 5,
        "accepted": 2,
        "excluded": 0,
        "quarantined": 3,
    }
    assert {item.reason for item in result.dispositions} == {
        "oversize",
        "invalid_utf8",
        "empty",
    }
    assert [unit.candidate.source_key for unit in result.candidates] == [
        "MEMORY.md#section=1",
        "MEMORY.md#section=3",
    ]


def test_safe_file_symlink_is_accepted_while_unsafe_links_are_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    inside_target = root / "inside-target.md"
    inside_target.write_text("# Inside target\nBody", encoding="utf-8")
    outside_target = outside / "outside.md"
    outside_target.write_text("# Outside target\nBody", encoding="utf-8")
    linked_directory = outside / "linked-directory"
    linked_directory.mkdir()
    (linked_directory / "hidden.md").write_text("# Must not be read", encoding="utf-8")
    try:
        (root / "inside-link.md").symlink_to(inside_target)
        (root / "escape.md").symlink_to(outside_target)
        (root / "broken.md").symlink_to(root / "missing.md")
        (root / "linked-dir").symlink_to(linked_directory, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    result = scan_codex_root(root)

    assert result.counts.model_dump() == {
        "discovered": 5,
        "accepted": 2,
        "excluded": 0,
        "quarantined": 3,
    }
    assert {item.reason for item in result.dispositions} == {"symlink_escape"}
    assert {unit.candidate.source_key for unit in result.candidates} == {
        "inside-link.md",
        "inside-target.md",
    }
    assert "hidden.md" not in {unit.candidate.source_key for unit in result.candidates}


def test_in_root_file_symlink_retargeted_before_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.md"
    inside.write_text("# Inside\nSafe body.\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside_body = "# Outside\nBody must never be accepted.\n"
    outside.write_text(outside_body, encoding="utf-8")
    link = root / "memory.md"
    try:
        link.symlink_to(inside)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    real_read = scanner_base.read_binary

    def retarget_then_read(
        path: Path,
        *,
        resolved_root: Path | None = None,
        max_bytes: int | None = 65_536,
    ) -> bytes:
        if path == link:
            path.unlink()
            path.symlink_to(outside)
        return real_read(path, resolved_root=resolved_root, max_bytes=max_bytes)

    monkeypatch.setattr("agent2pieces.scanners.base.read_binary", retarget_then_read)

    result = scan_codex_root(root)

    assert {unit.candidate.source_key for unit in result.candidates} == {"inside.md"}
    assert result.counts.quarantined == 1
    assert outside_body not in repr(result)


def test_junction_like_directory_is_not_traversed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    junction = root / "junction"
    junction.mkdir(parents=True)
    (junction / "hidden.md").write_text("# Must not be read\n", encoding="utf-8")
    monkeypatch.setattr(
        scanner_base,
        "_is_junction",
        lambda path: path == junction,
    )

    result = scan_codex_root(root)

    assert result.counts.accepted == 0
    assert result.counts.discovered == 0
    assert scanner_base.path_problem(junction, root) == "symlink_escape"
    assert "hidden.md" not in repr(result)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics only")
def test_windows_directory_junction_is_not_traversed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "hidden.md").write_text("# Must not be read\n", encoding="utf-8")
    junction = root / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("directory junction creation is unavailable")

    result = scan_codex_root(root)

    assert result.counts.accepted == 0
    assert "hidden.md" not in repr(result)


def test_source_swap_to_symlink_is_rejected_before_target_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "memory.md"
    source.write_text("# Initial\nSafe body.\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside_body = "# Outside\nBody must never be accepted.\n"
    outside.write_text(outside_body, encoding="utf-8")
    real_read = scanner_base.read_binary

    def swap_then_read(
        path: Path,
        *,
        resolved_root: Path | None = None,
        max_bytes: int | None = 65_536,
    ) -> bytes:
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as error:
            pytest.skip(f"symlinks are unavailable: {error}")
        return real_read(path, resolved_root=resolved_root, max_bytes=max_bytes)

    monkeypatch.setattr("agent2pieces.scanners.base.read_binary", swap_then_read)

    result = scan_codex_root(root)

    assert result.counts.accepted == 0
    assert result.counts.quarantined == 1
    assert outside_body not in repr(result)


def test_ancestor_swap_to_outside_symlink_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    source = nested / "memory.md"
    source.write_text("# Initial\nSafe body.\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_body = "# Outside\nBody must never be accepted.\n"
    (outside / "memory.md").write_text(outside_body, encoding="utf-8")
    moved = root / "moved"
    real_read = scanner_base.read_binary
    swapped = False

    def swap_ancestor_then_read(
        path: Path,
        *,
        resolved_root: Path | None = None,
        max_bytes: int | None = 65_536,
    ) -> bytes:
        nonlocal swapped
        if not swapped:
            nested.rename(moved)
            try:
                nested.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                moved.rename(nested)
                pytest.skip(f"directory symlinks are unavailable: {error}")
            swapped = True
        return real_read(path, resolved_root=resolved_root, max_bytes=max_bytes)

    monkeypatch.setattr(
        "agent2pieces.scanners.base.read_binary", swap_ancestor_then_read
    )

    result = scan_codex_root(root)

    assert result.counts.accepted == 0
    assert result.counts.quarantined == 1
    assert outside_body not in repr(result)


def test_hermes_entry_streaming_bounds_reads_and_recovers_after_oversize() -> None:
    class BoundedReader(io.BytesIO):
        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.maximum_request = 0

        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 8_192
            self.maximum_request = max(self.maximum_request, size)
            return super().read(size)

    source = BoundedReader(
        b"# first\nBody\n\xc2\xa7\n"
        + b"x" * (65_536 * 4)
        + b"\n\xc2\xa7\n# final\nBody\n"
    )

    entries = list(_stream_entries(source))

    assert source.maximum_request == 8_192
    assert entries[0][0] is not None
    assert entries[1] == (None, 65_536 * 4 + 2)
    assert entries[2][0] is not None
    assert all(raw is None or len(raw) <= 65_536 for raw, _count in entries)


def test_supported_special_file_is_quarantined(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    root = tmp_path / "codex"
    root.mkdir()
    fifo = root / "pipe.md"
    try:
        os.mkfifo(fifo)
    except OSError as error:
        pytest.skip(f"FIFO creation is unavailable: {error}")

    result = scan_codex_root(root)

    assert result.counts.quarantined == 1
    assert result.dispositions[0].reason == "special_file"


def test_supported_unreadable_file_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    source = root / "unreadable.md"
    source.write_text("# Unreadable", encoding="utf-8")

    def fail_read(path: Path, **_kwargs: object) -> bytes:
        if path == source:
            raise PermissionError("denied")
        return path.read_bytes()

    monkeypatch.setattr("agent2pieces.scanners.base.read_binary", fail_read)
    result = scan_codex_root(root)

    assert result.counts.quarantined == 1
    assert result.dispositions[0].reason == "unreadable"


def test_scans_preserve_source_bytes_mtime_and_mode(tmp_path: Path) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    source = root / "source.md"
    source.write_text("# Read only\nBody\n", encoding="utf-8")
    source.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    before = (source.read_bytes(), source.stat().st_mtime_ns, stat.S_IMODE(source.stat().st_mode))

    result = scan_codex_root(root)

    after = (source.read_bytes(), source.stat().st_mtime_ns, stat.S_IMODE(source.stat().st_mode))
    assert result.counts.accepted == 1
    assert after == before


def test_timestamp_changes_are_observations_but_title_project_and_links_are_successors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    source = root / "memory.md"

    def scan(frontmatter: str, heading: str, link: str) -> object:
        source.write_text(
            f"---\n{frontmatter}\n---\n# {heading}\nBody {link}\n",
            encoding="utf-8",
        )
        return scan_codex_root(root).candidates[0]

    baseline = scan(
        "project: one\nupdated_at: 2026-08-30T12:00:00Z",
        "Title",
        "https://a.test",
    )
    timestamp_only = scan(
        "project: one\nupdated_at: 2026-08-31T12:00:00Z",
        "Title",
        "https://a.test",
    )
    title_changed = scan(
        "project: one\nupdated_at: 2026-08-31T12:00:00Z",
        "Changed title",
        "https://a.test",
    )
    project_changed = scan(
        "project: two\nupdated_at: 2026-08-31T12:00:00Z",
        "Title",
        "https://a.test",
    )
    link_changed = scan(
        "project: one\nupdated_at: 2026-08-31T12:00:00Z",
        "Title",
        "https://b.test",
    )

    assert timestamp_only.candidate.source_hash == baseline.candidate.source_hash
    assert timestamp_only.candidate_input_hash == baseline.candidate_input_hash
    assert timestamp_only.candidate.source_updated_at != baseline.candidate.source_updated_at
    assert title_changed.candidate_input_hash != baseline.candidate_input_hash
    assert project_changed.candidate_input_hash != baseline.candidate_input_hash
    assert link_changed.candidate_input_hash != baseline.candidate_input_hash
    assert project_changed.candidate.source_hash == baseline.candidate.source_hash


def test_frontmatter_title_change_is_a_successor_without_a_source_hash_change(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects" / "project-one" / "memory"
    root.mkdir(parents=True)
    source = root / "topic.md"
    source.write_text("---\ntitle: First\n---\nBody\n", encoding="utf-8")
    first = scan_claude_root(root).candidates[0]
    source.write_text("---\ntitle: Second\n---\nBody\n", encoding="utf-8")
    second = scan_claude_root(root).candidates[0]

    assert second.candidate.source_hash == first.candidate.source_hash
    assert second.candidate_input_hash != first.candidate_input_hash


def test_invalid_updated_at_and_mtime_only_changes_use_audit_timestamp_without_new_hashes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    root.mkdir()
    source = root / "memory.md"
    source.write_text("---\nupdated_at: not-a-time\n---\n# Title\nBody\n", encoding="utf-8")
    os.utime(source, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    first = scan_codex_root(root).candidates[0]
    os.utime(source, ns=(1_700_000_100_000_000_000, 1_700_000_100_000_000_000))
    second = scan_codex_root(root).candidates[0]

    assert first.candidate.source_updated_at != second.candidate.source_updated_at
    assert first.candidate.source_hash == second.candidate.source_hash
    assert first.candidate_input_hash == second.candidate_input_hash
