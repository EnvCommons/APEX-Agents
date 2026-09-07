"""Tests for task-spec loading, submission dispatch and advertised tool names."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apexagents  # noqa: E402
from apexagents import (  # noqa: E402
    ApexAgents,
    SubmitFilesInput,
    TaskSpec,
    resolve_expected_output,
)

# Task ids whose records carry no expected_output and are console-message tasks.
NULL_EXPECTED_OUTPUT_TASK_IDS = [
    "task_854fefdbea5740a3a238c5930c2721e7",
    "task_45f1d761bb464e18993968273a9a9040",
    "task_112defba78604abcb27f4afb573d8d05",
    "task_6b27cc3ab9da428eaa9daa9f5100882b",
    "task_01bf3f1cdf6c432f822f91666047e38c",
]

SECRETS = {"openai_api_key": "test-key", "api_key": "test-key"}


def _record(task_id: str, expected_output, prompt: str = "Do the thing.") -> dict:
    return {
        "task_id": task_id,
        "domain": "Investment Banking",
        "world_id": "world_test",
        "prompt": prompt,
        "expected_output": expected_output,
        "gold_response_type": "text" if expected_output in (None, "message_in_console") else "file",
        "rubric": [{"verifier_id": "v1", "criteria": "It is done."}],
    }


@pytest.fixture
def task_dir(tmp_path, monkeypatch):
    """A data directory holding a small tasks_and_rubrics.json."""
    records = [
        _record("task_file_output", "make_new_doc"),
        _record("task_console", "message_in_console"),
    ] + [_record(task_id, None) for task_id in NULL_EXPECTED_OUTPUT_TASK_IDS]
    (tmp_path / "tasks_and_rubrics.json").write_text(json.dumps(records))
    monkeypatch.setattr(apexagents, "PATH", str(tmp_path))
    return tmp_path


def _build_env(task_id: str, expected_output: str) -> ApexAgents:
    env = ApexAgents(
        {
            "task_id": task_id,
            "domain": "Investment Banking",
            "world_id": "world_test",
            "prompt": "Do the thing.",
            "expected_output": expected_output,
        },
        secrets=SECRETS,
    )

    class _StubSandbox:
        async def download(self, path: str) -> bytes:
            return b"deliverable"

    async def _stub_grade(submission: str) -> dict:
        return {
            "display_text": "graded",
            "metadata": {"submission": submission},
            "reward": 1.0,
        }

    env.sandbox = _StubSandbox()
    env._extract_text_from_files = lambda file_contents: "\n".join(file_contents)
    env._grade_with_rubric = _stub_grade
    return env


# --- submission dispatch -----------------------------------------------------


def test_submit_files_dispatches_on_a_file_output_task(task_dir):
    """submit_files reads expected_output off the task dict, not an attribute."""
    import asyncio

    env = _build_env("task_file_output", "make_new_doc")
    out = asyncio.run(env.submit_files(SubmitFilesInput(file_paths=["/tmp/out.docx"])))

    assert out.reward == 1.0
    assert out.finished is True
    assert env.submitted is True
    assert out.metadata["submission"] == "/tmp/out.docx"


def test_submit_files_rejects_a_console_task_with_a_readable_message(task_dir):
    import asyncio

    env = _build_env("task_console", "message_in_console")
    out = asyncio.run(env.submit_files(SubmitFilesInput(file_paths=["/tmp/out.docx"])))

    assert out.metadata["error"] == "wrong_output_type"
    assert "submit_answer" in out.blocks[0].text


def test_submit_answer_rejects_a_file_output_task_with_a_readable_message(task_dir):
    import asyncio
    from apexagents import SubmitAnswerInput

    env = _build_env("task_file_output", "make_new_doc")
    out = asyncio.run(env.submit_answer(SubmitAnswerInput(answer="hello")))

    assert out.metadata["error"] == "wrong_output_type"
    assert "make_new_doc" in out.blocks[0].text


# --- task spec normalisation -------------------------------------------------


@pytest.mark.parametrize("task_id", NULL_EXPECTED_OUTPUT_TASK_IDS)
def test_null_expected_output_normalises_to_console_message(task_dir, task_id):
    spec = TaskSpec.model_validate(
        {
            "task_id": task_id,
            "domain": "Investment Banking",
            "world_id": "world_test",
            "prompt": "Do the thing.",
            "expected_output": None,
        }
    )
    assert spec.expected_output is None

    env = _build_env(task_id, None)
    assert env.task_data["expected_output"] == "message_in_console"


def test_missing_expected_output_key_normalises(task_dir):
    spec = TaskSpec.model_validate(
        {
            "task_id": "task_console",
            "domain": "Law",
            "world_id": "world_test",
            "prompt": "Do the thing.",
        }
    )
    assert spec.expected_output is None
    assert (
        resolve_expected_output(
            {"expected_output": None, "gold_response_type": "text"}
        )
        == "message_in_console"
    )


def test_list_tasks_emits_no_null_expected_output(task_dir):
    tasks = ApexAgents.list_tasks("test")

    assert len(tasks) == 7
    assert all(t["expected_output"] for t in tasks)
    emitted = {t["task_id"]: t["expected_output"] for t in tasks}
    for task_id in NULL_EXPECTED_OUTPUT_TASK_IDS:
        assert emitted[task_id] == "message_in_console"
    # Every emitted spec must construct a TaskSpec.
    for task in tasks:
        TaskSpec.model_validate(task)


def _deployed_tasks_path() -> Path | None:
    override = os.environ.get("APEX_TASKS_AND_RUBRICS")
    if override:
        return Path(override)
    candidate = Path(apexagents.PATH) / "tasks_and_rubrics.json"
    return candidate if candidate.exists() else None


def test_every_deployed_task_record_validates():
    """All shipped task records build a TaskSpec, none with a falsy output kind."""
    path = _deployed_tasks_path()
    if path is None or not path.exists():
        pytest.skip("tasks_and_rubrics.json not available (set APEX_TASKS_AND_RUBRICS)")

    records = json.loads(path.read_text())
    assert len(records) == 480

    normalised = 0
    for record in records:
        if not record.get("expected_output"):
            normalised += 1
        spec = TaskSpec.model_validate(
            {
                "task_id": record["task_id"],
                "domain": record["domain"],
                "world_id": record["world_id"],
                "prompt": record["prompt"],
                "expected_output": record.get("expected_output"),
            }
        )
        assert resolve_expected_output(record)

    assert normalised == len(NULL_EXPECTED_OUTPUT_TASK_IDS)
    by_id = {r["task_id"]: r for r in records}
    for task_id in NULL_EXPECTED_OUTPUT_TASK_IDS:
        assert resolve_expected_output(by_id[task_id]) == "message_in_console"


# --- advertised tool names ---------------------------------------------------


def _registered_tool_names() -> set[str]:
    return {spec.name for spec in ApexAgents.list_tools().tools}


def test_every_reader_tool_candidate_is_registered():
    """The advertised reader names must all exist in the real tool set."""
    registered = _registered_tool_names()
    advertised = {
        name
        for _label, _extensions, candidates in apexagents.BINARY_FILE_READERS
        for name in candidates
    }
    assert advertised, "no reader tools declared"
    assert advertised <= registered, sorted(advertised - registered)


def test_every_file_type_still_has_a_reader_tool():
    for label, extensions, reader_tools in ApexAgents._reader_tools():
        assert reader_tools, f"no registered reader tool for {label} ({extensions})"


def test_prompt_only_names_tools_that_exist(task_dir):
    import asyncio
    import re

    env = _build_env("task_file_output", "make_new_doc")
    prompt = asyncio.run(env.get_prompt())[0].text

    registered = _registered_tool_names()
    tools_section = prompt.split("IMPORTANT - File Type Tools:")[1]
    named = set()
    for line in tools_section.splitlines():
        if not line.startswith("- For "):
            continue
        named.update(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", line.split(" files: use ")[1]))
    unknown = {n for n in named if n not in registered}
    assert not unknown, sorted(unknown)

    # The reader tools for each binary type are actually mentioned.
    for _label, _extensions, reader_tools in ApexAgents._reader_tools():
        for name in reader_tools:
            assert name in prompt


def test_read_rejection_names_tools_that_exist(task_dir):
    import asyncio

    from cli_environment import ReadParams

    env = _build_env("task_file_output", "make_new_doc")
    registered = _registered_tool_names()

    for extension in (".xlsx", ".xls", ".docx", ".doc", ".pdf", ".pptx", ".ppt"):
        out = asyncio.run(env.read(ReadParams(file_path=f"/orwd_data/deck{extension}")))
        assert out.metadata["error"] == "binary_file"
        suggestion = out.metadata["suggestion"]
        names = suggestion.split(" - use ")[1].split(" or ")
        assert names
        for name in names:
            assert name in registered, name


def test_prompt_names_both_mounted_subtrees(task_dir):
    import asyncio

    env = _build_env("task_file_output", "make_new_doc")
    prompt = asyncio.run(env.get_prompt())[0].text

    assert "/orwd_data/filesystem/" in prompt
    assert "/orwd_data/.apps_data/" in prompt
