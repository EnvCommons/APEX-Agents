"""Tests for rubric aggregation in ApexAgents._grade_with_rubric."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apexagents import ApexAgents  # noqa: E402
from judge import GraderError, JudgeConfig  # noqa: E402

DATA = Path(__file__).parent / "data"
FAIL_WORDING_CRITERIA = json.loads(
    (DATA / "fail_wording_criteria.json").read_text(encoding="utf-8")
)


def make_env(rubric, verdicts):
    """An ApexAgents whose judge is replaced by a scripted verdict map.

    `verdicts` maps criterion text to either a bool or an exception to raise.
    """
    env = ApexAgents.__new__(ApexAgents)
    env.task_data = {
        "task_id": "task_test",
        "prompt": "Task prompt.",
        "rubric": rubric,
    }
    env.judge_config = JudgeConfig(model="stub-judge", api_key="stub")
    env.grader_client = None
    env.evaluated = []

    async def _evaluate_criterion(submission, criterion, task_prompt):
        env.evaluated.append(criterion)
        await asyncio.sleep(0)
        outcome = verdicts[criterion]
        if isinstance(outcome, BaseException):
            raise outcome
        return {
            "passed": outcome,
            "reasoning": "Analysis of failed transactions; criterion handled.",
            "grader_error": None,
        }

    env._evaluate_criterion = _evaluate_criterion
    return env


def rubric_from(criteria):
    return [
        {"verifier_id": f"ver_{i}", "criteria": c} for i, c in enumerate(criteria)
    ]


def test_all_criteria_passing_scores_one():
    criteria = [c["criteria"] for c in FAIL_WORDING_CRITERIA[:4]]
    env = make_env(rubric_from(criteria), {c: True for c in criteria})
    result = asyncio.run(env._grade_with_rubric("submission"))

    assert result["reward"] == 1.0
    assert result["metadata"]["passed"] is True
    assert result["metadata"]["passed_count"] == 4
    assert result["metadata"]["total_count"] == 4
    assert result["metadata"]["grading_complete"] is True
    assert result["metadata"]["grader_error_count"] == 0


def test_one_failing_criterion_zeroes_the_reward():
    criteria = [c["criteria"] for c in FAIL_WORDING_CRITERIA[:3]]
    verdicts = {criteria[0]: True, criteria[1]: False, criteria[2]: True}
    env = make_env(rubric_from(criteria), verdicts)
    result = asyncio.run(env._grade_with_rubric("submission"))

    assert result["reward"] == 0.0
    assert result["metadata"]["passed_count"] == 2
    assert result["metadata"]["grading_complete"] is True


def test_one_raising_criterion_does_not_kill_the_rest():
    criteria = [c["criteria"] for c in FAIL_WORDING_CRITERIA[:4]]
    verdicts = {
        criteria[0]: True,
        criteria[1]: GraderError("judge failed after 4 attempts: TimeoutError"),
        criteria[2]: True,
        criteria[3]: True,
    }
    env = make_env(rubric_from(criteria), verdicts)
    result = asyncio.run(env._grade_with_rubric("submission"))

    # Every criterion was still dispatched and reported.
    assert len(env.evaluated) == 4
    results = result["metadata"]["criteria_results"]
    assert len(results) == 4
    assert [r["passed"] for r in results] == [True, False, True, True]
    assert result["metadata"]["passed_count"] == 3

    # The un-gradeable criterion is flagged, not silently counted as a failure.
    assert result["metadata"]["grading_complete"] is False
    assert result["metadata"]["grader_error_count"] == 1
    assert result["metadata"]["grader_errors"][0]["verifier_id"] == "ver_1"
    assert "TimeoutError" in result["metadata"]["grader_errors"][0]["error"]
    assert result["reward"] == 0.0
    assert "could not be graded" in result["display_text"]


def test_a_grader_error_blocks_a_full_score_even_if_everything_else_passed():
    criteria = [c["criteria"] for c in FAIL_WORDING_CRITERIA[:2]]
    verdicts = {criteria[0]: True, criteria[1]: RuntimeError("boom")}
    env = make_env(rubric_from(criteria), verdicts)
    result = asyncio.run(env._grade_with_rubric("submission"))

    assert result["reward"] == 0.0
    assert result["metadata"]["passed"] is False
    assert result["metadata"]["grading_complete"] is False


def test_every_criterion_raising_still_returns_a_result():
    criteria = [c["criteria"] for c in FAIL_WORDING_CRITERIA[:3]]
    env = make_env(
        rubric_from(criteria), {c: GraderError("no verdict") for c in criteria}
    )
    result = asyncio.run(env._grade_with_rubric("submission"))

    assert result["reward"] == 0.0
    assert result["metadata"]["grader_error_count"] == 3
    assert result["metadata"]["passed_count"] == 0


def test_oversized_submission_is_truncated_before_the_fan_out():
    criteria = [c["criteria"] for c in FAIL_WORDING_CRITERIA[:2]]
    env = make_env(rubric_from(criteria), {c: True for c in criteria})
    env.judge_config = JudgeConfig(
        model="stub-judge", api_key="stub", max_submission_chars=500
    )

    seen = []
    original = env._evaluate_criterion

    async def spy(submission, criterion, task_prompt):
        seen.append(submission)
        return await original(submission, criterion, task_prompt)

    env._evaluate_criterion = spy
    result = asyncio.run(env._grade_with_rubric("X" * 100_000))

    assert result["metadata"]["submission_truncated"] is True
    assert all(len(s) < 1_000 for s in seen)
    assert "truncated" in result["display_text"]


def test_judge_model_is_reported_in_metadata():
    criteria = [FAIL_WORDING_CRITERIA[0]["criteria"]]
    env = make_env(rubric_from(criteria), {criteria[0]: True})
    env.judge_config = JudgeConfig(model="openai/gpt-5.6-luna", api_key="stub")
    result = asyncio.run(env._grade_with_rubric("submission"))
    assert result["metadata"]["judge_model"] == "openai/gpt-5.6-luna"
