from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import openai
from openreward.environments import JSONObject, TextBlock, ToolOutput, tool
from openreward import AsyncOpenReward, SandboxBucketConfig, SandboxSettings
from openreward.toolsets import WordToolset, PDFToolset
from pydantic import BaseModel, Field

from cli_environment import CLIEnvironment

# Data path in production (mounted via bucket config)
import os
from pathlib import Path
if os.path.exists("/orwd_data"):
    PATH = "/orwd_data/"
else:
    PATH = Path(__file__).parent

class TaskSpec(BaseModel):
    """Task specification for apex-agents tasks."""

    task_id: str
    domain: str
    world_id: str
    prompt: str
    expected_output: str


class SubmitAnswerInput(BaseModel):
    """Input for submit_answer tool (console message tasks)."""

    answer: str = Field(..., description="Text response for console message tasks")


class SubmitFilesInput(BaseModel):
    """Input for submit_files tool (file creation/editing tasks)."""

    file_paths: list[str] = Field(
        ..., description="Paths to created/edited files in sandbox workspace"
    )


class ApexAgents(CLIEnvironment):
    """
    APEX-AGENTS Environment: Professional services benchmark with 480 tasks
    across Investment Banking, Law, and Management Consulting domains.

    Tasks require multi-turn interaction with file exploration and creation.
    Evaluation uses LLM-based rubric grading with binary criteria.
    """
    toolsets = [WordToolset, PDFToolset]

    def __init__(self, task_spec: JSONObject, secrets: dict[str, str] = {}) -> None:
        super().__init__(task_spec, secrets=secrets)

        # Validate task spec
        self.validated = TaskSpec.model_validate(task_spec)

        # Load task data from /orwd_data
        with open(f"{PATH}/tasks_and_rubrics.json") as f:
            all_tasks: list[dict[str, Any]] = json.load(f)

        self.task_data = next(
            t for t in all_tasks if t["task_id"] == self.validated.task_id
        )

        # Initialize OpenAI client for grading
        api_key = secrets.get("openai_api_key")
        if not api_key:
            raise ValueError("OpenAI API key required in secrets")
        self.grader_client = openai.AsyncClient(api_key=api_key)

        # Configure sandbox
        self.sandbox_settings = SandboxSettings(
            environment="GeneralReasoning/APEX-Agents",
            image="generalreasoning/python-ds:3.12-tools",
            machine_size="0.5:0.5",
            block_network=False,
            bucket_config=SandboxBucketConfig(
                mount_path="/orwd_data",
                read_only=True,
                only_dir=f"world_files/{self.validated.world_id}/",
            ),
        )

        or_client = AsyncOpenReward(api_key=secrets.get("api_key", ""))
        self.sandbox = or_client.sandbox(self.sandbox_settings)

        # Track submission state
        self.submitted = False

    async def setup(self) -> None:
        """Start sandbox and install python-pptx, python-docx, openpyxl, and PDF libraries for PowerPoint, Word, Excel, and PDF tools"""
        await self.sandbox.start()
        # Install python-docx
        output, exit_code = await self.sandbox.run("pip3 install -q python-docx")

        # Install poppler-utils (system dependency for PDF rendering)
        print("[SETUP] Installing poppler-utils for PDF rendering...")
        output, exit_code = await self.sandbox.run("apt-get update && apt-get install -y poppler-utils")

        if exit_code == 0:
            print("[SETUP SUCCESS] poppler-utils installed successfully")
        else:
            print(f"[SETUP WARNING] poppler-utils installation exited with code {exit_code}")
            print(f"Output: {output}")

        # Install PDF manipulation libraries
        print("[SETUP] Installing PDF manipulation libraries...")
        output, exit_code = await self.sandbox.run("pip3 install -q pdfplumber pypdf reportlab pdf2image pillow")

        if exit_code == 0:
            print("[SETUP SUCCESS] PDF libraries installed successfully")
        else:
            print(f"[SETUP WARNING] PDF libraries installation exited with code {exit_code}")
            print(f"Output: {output}")

    async def get_prompt(self) -> list[TextBlock]:
        """Return task prompt with sandbox context and submission instructions."""
        base_prompt = self.task_data["prompt"]

        # Add context about sandbox environment
        sandbox_info = f"""

ENVIRONMENT INFORMATION:
- You are working in a sandboxed Linux environment with CLI tools available
- Task-specific files are mounted at: /orwd_data/
- You can use the tools available to help solve the task
"""

        # Add submission instructions based on expected output
        if self.task_data["expected_output"] == "message_in_console":
            submission_info = "- When ready, use submit_answer with your text response"
        else:
            submission_info = f"""- Expected output: {self.task_data["expected_output"]}
- When ready, use submit_files with paths to your created/edited files"""

        return [TextBlock(text=base_prompt + sandbox_info + submission_info)]

    @tool
    async def submit_answer(self, params: SubmitAnswerInput) -> ToolOutput:
        """
        Submit a text answer for tasks expecting 'message_in_console'.
        This tool evaluates the answer against all rubric criteria using LLM grading.
        """
        if self.submitted:
            return ToolOutput(
                blocks=[TextBlock(text="You have already submitted an answer.")],
                metadata={"error": "already_submitted"},
                reward=0.0,
                finished=True,
            )

        if self.task_data["expected_output"] != "message_in_console":
            return ToolOutput(
                blocks=[
                    TextBlock(
                        text=f"This task expects '{self.task_data.expected_output}', not a console message. Use submit_files instead."
                    )
                ],
                metadata={"error": "wrong_output_type"},
                reward=0.0,
                finished=False,
            )

        # Grade against rubric
        grading_results = await self._grade_with_rubric(params.answer)

        self.submitted = True
        return ToolOutput(
            blocks=[TextBlock(text=grading_results["display_text"])],
            metadata=grading_results["metadata"],
            reward=grading_results["reward"],
            finished=True,
        )

    @tool
    async def submit_files(self, params: SubmitFilesInput) -> ToolOutput:
        """
        Submit created/edited files for tasks expecting file outputs
        (make_new_doc, make_new_sheet, edit_existing_*, etc.)
        """
        if self.submitted:
            return ToolOutput(
                blocks=[TextBlock(text="You have already submitted files.")],
                metadata={"error": "already_submitted"},
                reward=0.0,
                finished=True,
            )

        if self.task_data.expected_output == "message_in_console":
            return ToolOutput(
                blocks=[
                    TextBlock(
                        text="This task expects a console message. Use submit_answer instead."
                    )
                ],
                metadata={"error": "wrong_output_type"},
                reward=0.0,
                finished=False,
            )

        # Download files from sandbox
        file_contents = {}
        for fpath in params.file_paths:
            try:
                content = await self.sandbox.download(fpath)
                file_contents[fpath] = content
            except Exception as e:
                return ToolOutput(
                    blocks=[TextBlock(text=f"Failed to download {fpath}: {str(e)}")],
                    metadata={"error": "download_failed", "file": fpath},
                    reward=0.0,
                    finished=False,
                )

        # Extract text from files and grade against rubric
        extracted_text = self._extract_text_from_files(file_contents)
        grading_results = await self._grade_with_rubric(extracted_text)

        self.submitted = True
        return ToolOutput(
            blocks=[TextBlock(text=grading_results["display_text"])],
            metadata=grading_results["metadata"],
            reward=grading_results["reward"],
            finished=True,
        )

    async def _grade_with_rubric(self, submission: str) -> dict[str, Any]:
        """
        Grade submission against all rubric criteria.
        ALL criteria must pass for full reward.
        """
        rubric = self.task_data["rubric"]

        # Evaluate all criteria concurrently for performance
        evaluation_tasks = [
            self._evaluate_criterion(
                submission=submission,
                criterion=c["criteria"],
                task_prompt=self.task_data["prompt"],
            )
            for c in rubric
        ]
        evaluation_results = await asyncio.gather(*evaluation_tasks)

        # Build results with verifier IDs
        results = []
        for criterion, eval_result in zip(rubric, evaluation_results):
            results.append(
                {
                    "verifier_id": criterion["verifier_id"],
                    "criteria": criterion["criteria"],
                    "passed": eval_result["passed"],
                    "reasoning": eval_result["reasoning"],
                }
            )

        # All criteria must pass
        all_passed = all(r["passed"] for r in results)
        passed_count = sum(r["passed"] for r in results)

        # Display text
        display_lines = [
            f"Rubric Evaluation ({passed_count}/{len(results)} criteria passed):"
        ]
        for i, r in enumerate(results, 1):
            status = "✓" if r["passed"] else "✗"
            display_lines.append(f"\n{status} Criterion {i}: {r['criteria']}")
            display_lines.append(f"   Reasoning: {r['reasoning']}")

        if all_passed:
            display_lines.append("\n\n✅ All criteria passed!")
        else:
            display_lines.append(
                f"\n\n❌ {len(results) - passed_count} criteria failed."
            )

        return {
            "display_text": "\n".join(display_lines),
            "metadata": {
                "task_id": self.task_data["task_id"],
                "passed": all_passed,
                "criteria_results": results,
                "passed_count": passed_count,
                "total_count": len(results),
            },
            "reward": 1.0 if all_passed else 0.0,
        }

    async def _evaluate_criterion(
        self, submission: str, criterion: str, task_prompt: str
    ) -> dict[str, Any]:
        """
        Use gpt-5-mini to evaluate a single criterion.
        CRITICAL: No temperature parameter (per CLAUDE.md grader rules).
        """
        grader_prompt = f"""You are evaluating whether a submission meets a specific criterion.

Task Prompt:
{task_prompt}

Submission:
{submission}

Criterion to evaluate:
{criterion}

Does the submission meet this criterion? Provide brief reasoning (1-2 sentences), then answer either "PASS" or "FAIL"."""

        response = await self.grader_client.chat.completions.create(
            model="gpt-5-mini",  # MUST use gpt-5-mini for graders
            messages=[{"role": "user", "content": grader_prompt}],
            # NO temperature parameter (per CLAUDE.md)
        )

        grading_text = response.choices[0].message.content or ""

        # Parse result
        upper_text = grading_text.upper()
        passed = "PASS" in upper_text and "FAIL" not in upper_text

        return {"passed": passed, "reasoning": grading_text}

    def _extract_text_from_files(self, file_contents: dict[str, bytes]) -> str:
        """
        Extract text from submitted files for grading.
        Handles .docx, .xlsx, .pptx, .txt, .md, .csv
        """
        try:
            from docx import Document
        except ImportError:
            from python_docx import Document

        try:
            import openpyxl
        except ImportError:
            pass

        try:
            from pptx import Presentation
        except ImportError:
            pass

        extracted = []

        for fpath, content in file_contents.items():
            try:
                if fpath.endswith(".docx"):
                    doc = Document(io.BytesIO(content))
                    text = "\n".join([p.text for p in doc.paragraphs])
                    extracted.append(f"=== {fpath} ===\n{text}")

                elif fpath.endswith(".xlsx"):
                    wb = openpyxl.load_workbook(io.BytesIO(content))
                    for sheet in wb.worksheets:
                        rows = [
                            " | ".join([str(cell.value or "") for cell in row])
                            for row in sheet.iter_rows()
                        ]
                        extracted.append(
                            f"=== {fpath} - {sheet.title} ===\n" + "\n".join(rows)
                        )

                elif fpath.endswith(".pptx"):
                    prs = Presentation(io.BytesIO(content))
                    for i, slide in enumerate(prs.slides):
                        slide_text = []
                        for shape in slide.shapes:
                            if hasattr(shape, "text"):
                                slide_text.append(shape.text)
                        extracted.append(
                            f"=== {fpath} - Slide {i+1} ===\n" + "\n".join(slide_text)
                        )

                elif fpath.endswith((".txt", ".md", ".csv")):
                    text = content.decode("utf-8", errors="ignore")
                    extracted.append(f"=== {fpath} ===\n{text}")

            except Exception as e:
                extracted.append(f"=== {fpath} ===\nError extracting text: {str(e)}")

        return "\n\n".join(extracted)

    @classmethod
    def list_tasks(cls, split: str) -> list[JSONObject]:
        """Load tasks from /orwd_data/apex-agents/tasks_and_rubrics.json"""
        with open(f"{PATH}/tasks_and_rubrics.json") as f:
            all_tasks: list[dict[str, Any]] = json.load(f)

        # Create task specs (exclude gold_response and rubric from public data)
        tasks = []
        for t in all_tasks:
            tasks.append(
                {
                    "task_id": t["task_id"],
                    "domain": t["domain"],
                    "world_id": t["world_id"],
                    "prompt": t["prompt"],
                    "expected_output": t["expected_output"],
                }
            )

        # Simple split strategy: all in "test" split for now
        if split == "test":
            return tasks
        return []

    @classmethod
    def list_splits(cls) -> list[str]:
        return ["test"]
