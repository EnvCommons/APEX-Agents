from __future__ import annotations

import asyncio
import io
import json
from typing import Any, Optional

import openai
from openreward.environments import JSONObject, TextBlock, ToolOutput, tool
from openreward import AsyncOpenReward, SandboxBucketConfig, SandboxSettings
from openreward.toolsets import WordToolset, PDFToolset, ExcelToolset, PowerPointToolset
from pydantic import BaseModel, Field

from cli_environment import CLIEnvironment, ReadParams

# Data path in production (mounted via bucket config)
import os
from pathlib import Path
if os.path.exists("/orwd_data"):
    PATH = "/orwd_data/"
else:
    PATH = Path(__file__).parent

def resolve_expected_output(task: dict[str, Any]) -> Optional[str]:
    """The submission route for a task, tolerating a null `expected_output`.

    5/480 rows in tasks_and_rubrics.json leave `expected_output` null. Every one
    of them has `gold_response_type == "text"`, and across the whole corpus
    "text" holds for exactly the 417 `message_in_console` tasks while all 58
    file-output tasks are "file" — so a null unambiguously means a console
    task.

    This matters beyond just not crashing. The submission gates compare against
    the literal "message_in_console", so a null left in place routes these 5 to
    `submit_files`: an agent that correctly reasons "this wants a console
    message" gets its `submit_answer` rejected and has to fabricate a
    spreadsheet to be graded at all (observed on task_6b27cc3ab9da428e — the
    agent reasoned correctly, was refused, and wrote a throwaway .xlsx). That
    grades the wrong artifact, so the reward for those tasks was meaningless
    even once the session started.

    A null with a non-"text" gold_response_type is a shape we have no rule for;
    leave it alone so TaskSpec's Optional carries it rather than guessing which
    of the six file types was meant.
    """
    expected = task.get("expected_output")
    if expected:
        return expected
    if task.get("gold_response_type") == "text":
        return "message_in_console"
    return expected


class TaskSpec(BaseModel):
    """Task specification for apex-agents tasks."""

    task_id: str
    domain: str
    world_id: str
    prompt: str
    # 5/480 rows in tasks_and_rubrics.json store a real None here. Typed as a
    # required str this raised `500 ValidationError` at session init, so those
    # tasks were unrunnable — the agent never even got a prompt. None already
    # behaves correctly downstream (it is != "message_in_console", so submit_files
    # is the accepted path), so tolerating it is enough; no other change needed.
    expected_output: Optional[str] = None


class SubmitAnswerInput(BaseModel):
    """Input for submit_answer tool (console message tasks)."""

    answer: str = Field(..., description="Text response for console message tasks")


class SubmitFilesInput(BaseModel):
    """Input for submit_files tool (file creation/editing tasks)."""

    file_paths: list[str] = Field(
        ..., description="Paths to created/edited files in sandbox workspace"
    )


# Reader tools advertised for each binary document type, in the order the agent
# should reach for them. The names are filtered against the environment's
# registered tools before they reach the model, so only real tools are named.
BINARY_FILE_READERS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "Excel",
        (".xlsx", ".xls"),
        ("excel_read_tab", "excel_list_tabs_in_spreadsheet"),
    ),
    (
        "Word",
        (".docx", ".doc"),
        ("word_read_document_content", "word_get_document_overview"),
    ),
    (
        "PDF",
        (".pdf",),
        ("pdfs_read_pdf_pages", "pdfs_get_document_overview"),
    ),
    (
        "PowerPoint",
        (".pptx", ".ppt"),
        ("powerpoint_read_all", "powerpoint_read_slides"),
    ),
)


class ApexAgents(CLIEnvironment):
    """
    APEX-AGENTS Environment: Professional services benchmark with 480 tasks
    across Investment Banking, Law, and Management Consulting domains.

    Tasks require multi-turn interaction with file exploration and creation.
    Evaluation uses LLM-based rubric grading with binary criteria.
    """
    toolsets = [WordToolset, PDFToolset, ExcelToolset, PowerPointToolset]

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
        # The submission gates below read task_data["expected_output"] directly,
        # so normalize here too — not just in the emitted task_spec — or the 5
        # null rows keep routing to submit_files. See resolve_expected_output.
        self.task_data["expected_output"] = resolve_expected_output(self.task_data)

        # Initialize OpenAI client for grading
        api_key = secrets.get("openai_api_key")
        if not api_key:
            raise ValueError("OpenAI API key required in secrets")
        self.grader_client = openai.AsyncClient(api_key=api_key)

        # Configure sandbox
        self.sandbox_settings = SandboxSettings(
            environment="GeneralReasoning/APEX-Agents",
            image="generalreasoning/knowledge-worker",
            machine_size="2:2",
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

        # Track whether task-specific files exist and were uploaded
        self.task_files_exist = False

    async def setup(self) -> None:
        """Start sandbox and install python-pptx, python-docx, openpyxl, and PDF libraries for PowerPoint, Word, Excel, and PDF tools"""
        await self.sandbox.start()
      
        # Upload task-specific files if they exist
        task_files_source = Path(PATH) / "task_files" / self.validated.task_id / "filesystem"

        if task_files_source.exists() and task_files_source.is_dir():
            print(f"[SETUP] Uploading task-specific files for {self.validated.task_id}...")

            # Create base directory for task files in writable location
            output, exit_code = await self.sandbox.run("mkdir -p /home/ubuntu/task_files")
            if exit_code != 0:
                print(f"[SETUP WARNING] Failed to create task_files directory: {output}")
                self.task_files_exist = False
                return

            file_count = 0
            for file_path in task_files_source.rglob("*"):
                if file_path.is_file():
                    # Calculate relative path to preserve directory structure
                    relative_path = file_path.relative_to(task_files_source)
                    remote_path = f"/home/ubuntu/task_files/{relative_path}"

                    # Create parent directory (use single quotes to handle spaces safely)
                    parent_dir = str(Path(remote_path).parent)
                    output, exit_code = await self.sandbox.run(f"mkdir -p '{parent_dir}'")

                    if exit_code != 0:
                        print(f"[SETUP WARNING] Failed to create directory {parent_dir}: {output}")
                        continue

                    # Upload to temp location (no spaces), then move to final destination
                    # This works around sandbox.upload() not quoting paths with spaces
                    try:
                        temp_path = f"/tmp/upload_{hash(str(file_path))}.tmp"
                        await self.sandbox.upload(str(file_path), temp_path)

                        # Move to final destination with proper quoting
                        # Escape single quotes in the path for bash
                        escaped_remote = remote_path.replace("'", "'\\''")
                        output, exit_code = await self.sandbox.run(f"mv '{temp_path}' '{escaped_remote}'")

                        if exit_code != 0:
                            print(f"[SETUP WARNING] Failed to move {file_path.name} to final location: {output}")
                            continue

                        file_count += 1
                    except Exception as e:
                        print(f"[SETUP WARNING] Failed to upload {file_path.name}: {str(e)}")
                        continue

            if file_count > 0:
                print(f"[SETUP SUCCESS] Uploaded {file_count} task files to /home/ubuntu/task_files/")
                self.task_files_exist = True
            else:
                print(f"[SETUP WARNING] No files were successfully uploaded")
                self.task_files_exist = False
        else:
            print(f"[SETUP] No task-specific files found for {self.validated.task_id}")
            self.task_files_exist = False

    @classmethod
    def _reader_tools(cls) -> list[tuple[str, tuple[str, ...], list[str]]]:
        """Binary document types paired with the reader tools this env registers."""
        registered = {spec.name for spec in cls.list_tools().tools}
        return [
            (label, extensions, [name for name in candidates if name in registered])
            for label, extensions, candidates in BINARY_FILE_READERS
        ]

    @tool
    async def read(self, params: ReadParams) -> ToolOutput:
        """
        Read file contents. For binary files (Excel, Word, PDF, PowerPoint),
        use the appropriate toolset tools instead.
        """
        file_path = params.file_path

        # Check for binary file extensions
        binary_extensions: dict[str, str] = {}
        for label, extensions, reader_tools in self._reader_tools():
            if not reader_tools:
                continue
            suggestion = f"{label} files - use {' or '.join(reader_tools)}"
            for extension in extensions:
                binary_extensions[extension] = suggestion

        # Get file extension
        ext = '.' + file_path.lower().rsplit('.', 1)[-1] if '.' in file_path else ''

        if ext in binary_extensions:
            suggestion = binary_extensions[ext]
            return ToolOutput(
                blocks=[TextBlock(text=f"Cannot read binary file with 'read' tool.\n\n{suggestion}\n\nAvailable toolset tools can properly parse this file format.")],
                metadata={"error": "binary_file", "suggestion": suggestion},
                reward=0.0,
                finished=False,
            )

        # For text files, use parent class implementation
        return await super().read(params)

    async def get_prompt(self) -> list[TextBlock]:
        """Return task prompt with sandbox context and submission instructions."""
        base_prompt = self.task_data["prompt"]

        # Advertise only the reader tools this environment actually registers
        tool_lines = "\n".join(
            f"- For {'/'.join(extensions)} files: use {', '.join(reader_tools)} (NOT read)"
            for _label, extensions, reader_tools in self._reader_tools()
            if reader_tools
        )

        # Add context about sandbox environment
        sandbox_info = f"""

ENVIRONMENT INFORMATION:
- You are working in a sandboxed Linux environment with CLI tools available
- World files are mounted read-only at /orwd_data/, in two subtrees:
  - /orwd_data/filesystem/ - the documents for this world (Excel, Word, PDF,
    PowerPoint, text)
  - /orwd_data/.apps_data/ - the mail, chat and calendar corpus for this world.
    It is a dot-directory, so a plain `ls /orwd_data/` will not list it; use
    `ls -a /orwd_data/` or address it by path.
- You can use the tools available to help solve the task

IMPORTANT - File Type Tools:
{tool_lines}
- For .txt/.csv/.md files: use read, grep, bash
"""

        # Add task files information if they exist
        if self.task_files_exist:
            sandbox_info += """
Additional task files are available at: /home/ubuntu/task_files/
Use ls to explore the directory structure, then use the appropriate tool for each file type."""

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
                        text=f"This task expects '{self.task_data['expected_output']}', not a console message. Use submit_files instead."
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

        if self.task_data["expected_output"] == "message_in_console":
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
            model="gpt-5-mini",
            messages=[{"role": "user", "content": grader_prompt}],
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
                    "expected_output": resolve_expected_output(t),
                }
            )

        # Simple split strategy: all in "test" split for now
        if split == "test":
            return tasks
        return []

    @classmethod
    def list_splits(cls) -> list[str]:
        return ["test"]
