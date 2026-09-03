from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect


def _open(page: Page, harness: Any) -> None:
    page.goto(harness.base_url)
    expect(page.locator("#connection-status")).to_have_text("Pieces ready")
    expect(page.locator("#candidate-total")).not_to_have_text("0")


def _open_candidate(page: Page, title: str) -> None:
    page.get_by_role("button", name=title, exact=True).first.click()
    expect(page.locator("#candidate-title")).to_have_value(title)


def test_scan_filter_edit_diff_and_hostile_preview_stays_text(
    browser_page: Page,
    browser_harness: Any,
) -> None:
    page = browser_page
    _open(page, browser_harness)
    assert page.evaluate("window.agent2piecesCompromised") is None

    page.locator("#scan-button").click()
    expect(page.locator("#scan-counts")).to_contain_text("completed:", timeout=10_000)
    expect(page.get_by_role("button", name="Browser scan memory", exact=True)).to_be_visible()

    page.locator("#project-filter").select_option(label="Beta (1)")
    expect(page.get_by_role("button", name="Release contact", exact=True)).to_be_visible()
    page.locator("#security-filter").select_option("warning")
    expect(page.locator("#candidate-total")).to_have_text("1")

    page.locator("#project-filter").select_option("")
    page.locator("#security-filter").select_option("")
    _open_candidate(page, "Hostile markup")
    expect(page.locator("#preview-body")).to_contain_text("<script>")
    assert page.evaluate("window.agent2piecesCompromised") is None
    assert page.locator("#preview-body script").count() == 0

    _open_candidate(page, "Changed source")
    expect(page.locator("#prior-body")).to_contain_text("previous approved body")
    expect(page.locator("#current-body")).to_contain_text("current body")
    expect(page.locator("#diff-hunks")).to_contain_text("+ The current body")
    page.locator("#candidate-title").fill("Changed source edited")
    page.locator("#candidate-body").fill("A safe browser edit.")
    page.locator("#save-candidate").click()
    expect(page.locator("#candidate-title")).to_have_value("Changed source edited")
    expect(page.locator("#candidate-state")).to_contain_text("pending")


def test_duplicate_group_uses_scored_default_and_allows_representative_override(
    browser_page: Page,
    browser_harness: Any,
) -> None:
    page = browser_page
    _open(page, browser_harness)
    _open_candidate(page, "Dispatch boundary")

    page.locator("#duplicate-check").click()
    expect(page.locator("#duplicate-verdict")).to_have_text("likely")
    expect(page.locator("#group-representative")).to_have_value(
        browser_harness.candidate_ids["complete"]
    )
    page.locator("#create-group").click()
    expect(page.locator("#live-message")).to_have_text("Duplicate group created.")
    expect(page.locator("#candidate-state")).to_contain_text("pending")
    page.locator("#group-representative").select_option(
        browser_harness.candidate_ids["sparse"]
    )
    page.locator("#set-representative").click()
    expect(page.locator("#live-message")).to_have_text("Group representative changed.")
    persisted = browser_harness.ledger.connection.execute(
        "SELECT representative_candidate_id, representative_overridden "
        "FROM review_groups"
    ).fetchone()
    assert persisted["representative_candidate_id"] == browser_harness.candidate_ids[
        "sparse"
    ]
    assert persisted["representative_overridden"] == 1


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [("Approve group", "approved"), ("Exclude group", "excluded")],
)
def test_duplicate_group_decisions_are_visible_and_render_the_resulting_status(
    browser_page: Page,
    browser_harness: Any,
    action: str,
    expected_status: str,
) -> None:
    page = browser_page
    _open(page, browser_harness)
    _open_candidate(page, "Dispatch boundary")
    page.locator("#duplicate-check").click()
    expect(page.locator("#duplicate-verdict")).to_have_text("likely")
    page.locator("#create-group").click()
    expect(page.locator("#live-message")).to_have_text("Duplicate group created.")

    group_action = page.get_by_role("button", name=action, exact=True)
    expect(group_action).to_be_visible()
    group_action.click()

    expect(page.locator("#duplicate-group")).to_contain_text(expected_status)
    persisted = browser_harness.ledger.connection.execute(
        "SELECT status FROM review_groups"
    ).fetchone()
    assert persisted["status"] == expected_status


def test_apply_requires_fresh_confirmation_and_cancel_never_posts(
    browser_page: Page,
    browser_harness: Any,
) -> None:
    page = browser_page
    import_actions: list[str] = []
    page.on(
        "request",
        lambda request: import_actions.append(str(request.post_data_json["action"]))
        if request.url.endswith("/api/import-jobs")
        else None,
    )
    _open(page, browser_harness)
    assert import_actions == []

    page.get_by_label("Select Approved import for Apply").check()
    page.locator("#apply-button").click()
    expect(page.locator("#apply-endpoint")).to_have_text(
        "http://pieces.test/model_context_protocol/2025-03-26/mcp"
    )
    expect(page.locator("#apply-count")).to_have_text("1")
    expect(page.locator("#apply-context-hash")).to_have_text(
        re.compile(r"^[0-9a-f]{64}$")
    )
    approved = browser_harness.ledger.get_candidate(
        browser_harness.candidate_ids["approved"]
    )
    expect(page.locator("#apply-confirmation")).to_contain_text(approved.payload_hash)
    expect(page.locator("#apply-confirmation")).to_contain_text("project: none")
    expect(page.locator("#apply-confirmation")).to_contain_text("files: none")
    page.locator("#apply-cancel").click()
    assert import_actions == ["preview"]

    _open_candidate(page, "Approved import")
    page.locator("#candidate-body").fill("An unsaved edit invalidates confirmation.")
    expect(page.locator("#apply-confirm")).to_be_disabled()
    page.locator("#candidate-body").fill("Write this approved candidate once.")
    page.locator("#apply-button").click()
    page.locator("#apply-confirm").click()
    expect(page.locator("#import-progress")).to_have_text("completed", timeout=10_000)
    expect(page.locator("#import-items")).to_contain_text("imported")
    assert import_actions == ["preview", "preview", "start"]


def test_ambiguous_write_pauses_and_recheck_resumes_same_job(
    browser_page: Page,
    browser_harness: Any,
) -> None:
    from agent2pieces.mcp_client import MarkerSearchResult

    page = browser_page
    browser_harness.pieces.write_results.append("post_failure")
    browser_harness.pieces.marker_results.extend(
        [
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(outcome="absent", coverage="complete"),
            MarkerSearchResult(
                outcome="one_parent",
                coverage="complete",
                parent_memory_ids=("recovered-memory",),
            ),
        ]
    )
    _open(page, browser_harness)
    page.get_by_label("Select Approved import for Apply").check()
    page.locator("#apply-button").click()
    page.locator("#apply-confirm").click()
    expect(page.locator("#import-progress")).to_contain_text("paused", timeout=10_000)
    expect(page.locator("#import-items")).to_contain_text("ambiguous")
    expect(page.locator("#resume-recheck")).to_be_enabled()
    page.locator("#resume-recheck").click()
    expect(page.locator("#import-progress")).to_have_text("completed", timeout=10_000)
    expect(page.locator("#import-items")).to_contain_text("imported")
