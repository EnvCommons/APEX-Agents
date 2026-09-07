from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Optional

import openai
import openpyxl
from docx import Document
from docx.oxml.ns import qn
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pypdf import PdfReader
from openreward.environments import JSONObject, TextBlock, ToolOutput, tool
from openreward import AsyncOpenReward, SandboxBucketConfig, SandboxSettings
from openreward.toolsets import WordToolset, PDFToolset, ExcelToolset, PowerPointToolset
from pydantic import BaseModel, Field

from cli_environment import CLIEnvironment, ReadParams
from judge import JudgeConfig, judge_criterion, prepare_submission
from market_data import MarketDataToolset

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

# Ceiling on a single headless LibreOffice recalculation of a submitted
# workbook, so a pathological file cannot hold a submission open.
_RECALC_TIMEOUT_SECONDS = 120

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
    toolsets = [WordToolset, PDFToolset, ExcelToolset, PowerPointToolset, MarketDataToolset]

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

        # Judge client for rubric grading; provider comes from secrets/env
        self.judge_config = JudgeConfig.from_secrets(secrets)
        self.grader_client = self.judge_config.client()

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
        """Start the sandbox and upload the task's files into /home/ubuntu/task_files"""
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

        A criterion the judge cannot grade is reported as a grader error
        rather than as a failed criterion, and never aborts the episode.
        """
        rubric = self.task_data["rubric"]

        prepared, truncated = prepare_submission(
            submission, self.judge_config.max_submission_chars
        )

        # Evaluate all criteria concurrently for performance
        evaluation_tasks = [
            self._evaluate_criterion(
                submission=prepared,
                criterion=c["criteria"],
                task_prompt=self.task_data["prompt"],
            )
            for c in rubric
        ]
        evaluation_results = await asyncio.gather(
            *evaluation_tasks, return_exceptions=True
        )

        # Build results with verifier IDs
        results = []
        for criterion, eval_result in zip(rubric, evaluation_results):
            if isinstance(eval_result, BaseException):
                eval_result = {
                    "passed": False,
                    "reasoning": f"Grader error: {eval_result}",
                    "grader_error": str(eval_result),
                }
            results.append(
                {
                    "verifier_id": criterion["verifier_id"],
                    "criteria": criterion["criteria"],
                    "passed": eval_result["passed"],
                    "reasoning": eval_result["reasoning"],
                    "grader_error": eval_result.get("grader_error"),
                }
            )

        grader_errors = [r for r in results if r["grader_error"]]
        grading_complete = not grader_errors

        # All criteria must pass, and all criteria must have been graded
        passed_count = sum(r["passed"] for r in results)
        all_passed = grading_complete and all(r["passed"] for r in results)

        # Display text
        display_lines = [
            f"Rubric Evaluation ({passed_count}/{len(results)} criteria passed):"
        ]
        for i, r in enumerate(results, 1):
            status = "!" if r["grader_error"] else ("✓" if r["passed"] else "✗")
            display_lines.append(f"\n{status} Criterion {i}: {r['criteria']}")
            display_lines.append(f"   Reasoning: {r['reasoning']}")

        if truncated:
            display_lines.append(
                "\n\nNote: the submission was truncated to fit the grading "
                "context limit; grading used the visible content."
            )

        if not grading_complete:
            display_lines.append(
                f"\n\n⚠️ {len(grader_errors)} criteria could not be graded "
                "(grader error). This submission was not fully evaluated and "
                "scores 0.0."
            )
        elif all_passed:
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
                # Grading is incomplete when the judge could not return a
                # verdict for some criterion. The reward is 0.0 either way,
                # so these flags distinguish it from a graded failure.
                "grading_complete": grading_complete,
                "grader_error_count": len(grader_errors),
                "grader_errors": [
                    {"verifier_id": r["verifier_id"], "error": r["grader_error"]}
                    for r in grader_errors
                ],
                "submission_truncated": truncated,
                "judge_model": self.judge_config.model,
            },
            "reward": 1.0 if all_passed else 0.0,
        }

    async def _evaluate_criterion(
        self, submission: str, criterion: str, task_prompt: str
    ) -> dict[str, Any]:
        """
        Ask the judge whether a single criterion is met.

        The verdict comes from the structured `is_criteria_true` field, so
        wording in the rationale cannot change the outcome. Raises
        `GraderError` when no verdict can be obtained.
        """
        verdict = await judge_criterion(
            self.grader_client,
            self.judge_config,
            task_prompt=task_prompt,
            submission=submission,
            criterion=criterion,
        )
        return {
            "passed": verdict.is_criteria_true,
            "reasoning": verdict.rationale,
            "grader_error": None,
        }

    def _extract_text_from_files(self, file_contents: dict[str, bytes]) -> str:
        """
        Extract text from submitted files for grading.

        Handles .docx, .xlsx/.xlsm, .pptx, .pdf and plain-text formats. Every
        file yields a block, including one that cannot be parsed: an
        unsupported or broken file emits a visible marker so an empty
        extraction is never graded as an empty submission. Per-file outcomes
        are also recorded on ``extraction_diagnostics``.

        Pure with respect to the arguments apart from that one attribute, so it
        is safe to run off the event loop in a worker thread.
        """
        extracted: list[str] = []
        diagnostics: list[dict[str, Any]] = []

        for fpath, content in file_contents.items():
            # The submitted path decides the parser, matched case-insensitively
            # so an upper-case extension is not treated as unknown.
            ext = os.path.splitext(fpath)[1].lower()
            try:
                if ext == ".docx":
                    text = _docx_to_text(content)
                    extracted.append(f"=== {fpath} ===\n{text}")
                    diagnostics.append({"file": fpath, "status": "ok", "format": ext})

                elif ext in (".xlsx", ".xlsm"):
                    text, recalc = _xlsx_to_text(content, ext)
                    extracted.append(f"=== {fpath} ===\n{text}")
                    diagnostics.append(
                        {
                            "file": fpath,
                            "status": "ok",
                            "format": ext,
                            "formula_recalc": recalc,
                        }
                    )

                elif ext == ".pptx":
                    text = _pptx_to_text(content)
                    extracted.append(f"=== {fpath} ===\n{text}")
                    diagnostics.append({"file": fpath, "status": "ok", "format": ext})

                elif ext == ".pdf":
                    text = _pdf_to_text(content)
                    extracted.append(f"=== {fpath} ===\n{text}")
                    diagnostics.append({"file": fpath, "status": "ok", "format": ext})

                elif ext in (".txt", ".md", ".csv", ".json", ".tsv", ".xml", ".html"):
                    text = content.decode("utf-8", errors="replace")
                    extracted.append(f"=== {fpath} ===\n{text}")
                    diagnostics.append({"file": fpath, "status": "ok", "format": ext})

                else:
                    label = ext or "(no extension)"
                    extracted.append(
                        f"=== {fpath} ===\n"
                        f"[EXTRACTION FAILED: unsupported file type {label}. "
                        f"No text could be read from this file, so none of its "
                        f"contents are part of the graded submission.]"
                    )
                    diagnostics.append(
                        {"file": fpath, "status": "unsupported", "format": label}
                    )

            except Exception as e:
                extracted.append(
                    f"=== {fpath} ===\n"
                    f"[EXTRACTION FAILED: {type(e).__name__}: {e}. "
                    f"No text could be read from this file, so none of its "
                    f"contents are part of the graded submission.]"
                )
                diagnostics.append(
                    {
                        "file": fpath,
                        "status": "error",
                        "format": ext,
                        "error": f"{type(e).__name__}: {e}",
                    }
                )

        self.extraction_diagnostics = diagnostics
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


# --- File extraction helpers -------------------------------------------------
#
# These render a submitted binary document into the plain text a rubric judge
# reads. They are module-level and stateless so they can run in a worker
# thread. Fidelity matters more than brevity here: a number that does not
# survive extraction cannot be credited.


def _docx_to_text(content: bytes) -> str:
    """Render a .docx as text, including everything outside the body flow.

    ``Document.paragraphs`` covers only top-level body paragraphs, so tables,
    headers, footers, text boxes and footnotes are collected separately.
    """
    doc = Document(io.BytesIO(content))
    parts: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)

    for table in doc.tables:
        parts.extend(_docx_table_lines(table))

    # Text boxes live in a drawing canvas rather than the body flow, so their
    # runs are read straight off the XML.
    textbox_lines = _docx_textbox_lines(doc)
    if textbox_lines:
        parts.append("=== Text Boxes ===")
        parts.extend(textbox_lines)

    for idx, section in enumerate(doc.sections, start=1):
        for label, container in (
            ("Header", section.header),
            ("First Page Header", section.first_page_header),
            ("Even Page Header", section.even_page_header),
            ("Footer", section.footer),
            ("First Page Footer", section.first_page_footer),
            ("Even Page Footer", section.even_page_footer),
        ):
            lines = _docx_container_lines(container)
            if lines:
                parts.append(f"=== Section {idx} {label} ===")
                parts.extend(lines)

    for label, part_name in (
        ("Footnotes", "word/footnotes.xml"),
        ("Endnotes", "word/endnotes.xml"),
    ):
        lines = _docx_note_lines(content, part_name)
        if lines:
            parts.append(f"=== {label} ===")
            parts.extend(lines)

    return "\n".join(parts)


def _docx_table_lines(table: Any) -> list[str]:
    """Rows of a table as tab-separated lines, following nested tables."""
    lines: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            cell_text = cell.text.strip()
            cells.append(cell_text)
            for nested in cell.tables:
                lines.extend(_docx_table_lines(nested))
        if any(cells):
            lines.append("\t".join(cells))
    return lines


def _docx_container_lines(container: Any) -> list[str]:
    """Paragraph and table text of a header or footer."""
    lines: list[str] = []
    try:
        if container.is_linked_to_previous:
            return lines
        for para in container.paragraphs:
            text = para.text.strip()
            if text:
                lines.append(text)
        for table in container.tables:
            lines.extend(_docx_table_lines(table))
    except (AttributeError, ValueError):
        return lines
    return lines


def _docx_textbox_lines(doc: Any) -> list[str]:
    """Text of every text box anchored in the document body."""
    lines: list[str] = []
    for txbx in doc.element.body.iter(qn("w:txbxContent")):
        for para in txbx.iter(qn("w:p")):
            text = "".join(node.text or "" for node in para.iter(qn("w:t"))).strip()
            if text:
                lines.append(text)
    return lines


def _docx_note_lines(content: bytes, part_name: str) -> list[str]:
    """Footnote or endnote text, read from the package part directly.

    python-docx exposes no API for these parts, and the separator notes Word
    always writes are skipped so only authored notes appear.
    """
    lines: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            if part_name not in zf.namelist():
                return lines
            root = ET.fromstring(zf.read(part_name))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return lines

    note_tags = (qn("w:footnote"), qn("w:endnote"))
    for note in root:
        if note.tag not in note_tags:
            continue
        if note.get(qn("w:type")) in ("separator", "continuationSeparator"):
            continue
        for para in note.iter(qn("w:p")):
            text = "".join(node.text or "" for node in para.iter(qn("w:t"))).strip()
            if text:
                lines.append(text)
    return lines


def _xlsx_to_text(content: bytes, suffix: str = ".xlsx") -> tuple[str, str]:
    """Render a workbook as cell values plus the formulas behind them.

    Returns the text and the recalculation outcome, one of ``cached`` (every
    formula already carried a saved result), ``libreoffice`` (results were
    computed here) or ``unavailable`` (formula cells have no value to show).

    openpyxl has no formula engine, and it drops the value cache of any
    workbook it saves, so a workbook the agent edited carries formulas with no
    result. Those cells read as ``None`` and would reach the judge blank;
    LibreOffice recalculates them first.
    """
    formula_text = _xlsx_formula_map(content)
    values_source: bytes = content
    recalc_status = "cached"

    if formula_text and _xlsx_has_uncached_formulas(content, formula_text):
        recalculated = _recalculate_xlsx(content, suffix)
        if recalculated is None:
            recalc_status = "unavailable"
        else:
            values_source = recalculated
            recalc_status = "libreoffice"

    wb = openpyxl.load_workbook(io.BytesIO(values_source), data_only=True)
    try:
        blocks: list[str] = []
        if recalc_status == "unavailable":
            blocks.append(
                "=== Extraction Note ===\n"
                "[formula results could not be computed: blank cells below may "
                "be uncomputed formulas rather than missing values]"
            )

        for sheet in wb.worksheets:
            rows: list[str] = []
            formula_lines: list[str] = []
            for row in sheet.iter_rows():
                values: list[str] = []
                for cell in row:
                    # An explicit None test, because 0 and False are answers.
                    values.append("" if cell.value is None else str(cell.value))
                    formula = formula_text.get((sheet.title, cell.coordinate))
                    if formula:
                        formula_lines.append(f"{cell.coordinate}: {formula}")
                if any(values):
                    rows.append(" | ".join(values))

            sheet_text = "\n".join(rows)
            if formula_lines:
                sheet_text += "\n\n=== Formulas ===\n" + "\n".join(formula_lines)
            blocks.append(f"=== Sheet: {sheet.title} ===\n{sheet_text}")

        return "\n\n".join(blocks), recalc_status
    finally:
        wb.close()


def _xlsx_formula_map(content: bytes) -> dict[tuple[str, str], str]:
    """Authored formula text of every formula cell, keyed by sheet and cell."""
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=False, read_only=True)
    try:
        formulas: dict[tuple[str, str], str] = {}
        for sheet_name in wb.sheetnames:
            for row in wb[sheet_name].iter_rows():
                for cell in row:
                    coord = getattr(cell, "coordinate", None)
                    if not coord:
                        continue
                    value = cell.value
                    # Array formulas arrive as an object carrying .text.
                    text = value if isinstance(value, str) else getattr(value, "text", None)
                    if isinstance(text, str) and text.startswith("="):
                        formulas[(sheet_name, coord)] = text
        return formulas
    finally:
        wb.close()


def _xlsx_has_uncached_formulas(
    content: bytes, formula_text: dict[tuple[str, str], str]
) -> bool:
    """True when a formula cell has no saved result to read."""
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    try:
        for sheet_name in wb.sheetnames:
            for row in wb[sheet_name].iter_rows():
                for cell in row:
                    if cell.value is not None:
                        continue
                    coord = getattr(cell, "coordinate", None)
                    if coord and (sheet_name, coord) in formula_text:
                        return True
        return False
    finally:
        wb.close()


def _recalculate_xlsx(content: bytes, suffix: str = ".xlsx") -> bytes | None:
    """Compute a workbook's formula results with headless LibreOffice.

    Returns the recalculated workbook, or None when LibreOffice is missing or
    the conversion fails. Everything is scoped to a private temporary
    directory, including LibreOffice's user profile, so concurrent calls do
    not contend.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None

    with tempfile.TemporaryDirectory(prefix="xlsx_recalc_") as tmpdir:
        tmp = Path(tmpdir)
        source = tmp / f"workbook{suffix}"
        source.write_bytes(content)
        outdir = tmp / "out"
        outdir.mkdir()
        profile = tmp / "profile"

        try:
            result = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--calc",
                    f"-env:UserInstallation=file://{profile}",
                    "--convert-to",
                    "xlsx",
                    "--outdir",
                    str(outdir),
                    str(source),
                ],
                capture_output=True,
                timeout=_RECALC_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        if result.returncode != 0:
            return None

        converted = outdir / f"{source.stem}.xlsx"
        if not converted.exists():
            return None
        return converted.read_bytes()


def _pptx_to_text(content: bytes) -> str:
    """Render a deck as text: every shape, plus tables and speaker notes."""
    prs = Presentation(io.BytesIO(content))
    blocks: list[str] = []

    for index, slide in enumerate(prs.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            lines.extend(_pptx_shape_lines(shape))

        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                lines.append(f"=== Speaker Notes ===\n{notes}")

        blocks.append(f"=== Slide {index} ===\n" + "\n".join(lines))

    return "\n\n".join(blocks)


def _pptx_shape_lines(shape: Any) -> list[str]:
    """Text of a shape, descending into groups and tables.

    A group shape, a table and a chart all lack a ``.text`` attribute, so each
    is handled by its own accessor rather than skipped.
    """
    lines: list[str] = []

    # A shape whose type python-pptx cannot resolve still gets its plain text
    # read below, so an unknown type never costs the whole slide.
    try:
        shape_type = shape.shape_type
    except (ValueError, KeyError, AttributeError):
        shape_type = None

    if shape_type == MSO_SHAPE_TYPE.GROUP:
        for child in shape.shapes:
            lines.extend(_pptx_shape_lines(child))
        return lines

    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                lines.append("\t".join(cells))
        return lines

    if getattr(shape, "has_chart", False):
        chart = shape.chart
        chart_lines = []
        try:
            if chart.has_title and chart.chart_title.has_text_frame:
                chart_lines.append(chart.chart_title.text_frame.text.strip())
            for series in chart.plots[0].series:
                values = " | ".join(
                    "" if v is None else str(v) for v in series.values
                )
                chart_lines.append(f"{series.name}: {values}")
        except (ValueError, AttributeError, IndexError):
            pass
        if chart_lines:
            lines.append("=== Chart ===\n" + "\n".join(chart_lines))
        return lines

    text = getattr(shape, "text", "")
    if text and text.strip():
        lines.append(text.strip())
    return lines


def _pdf_to_text(content: bytes) -> str:
    """Render a PDF's text layer page by page."""
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        reader.decrypt("")

    pages: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception as e:
            text = f"[page text could not be extracted: {type(e).__name__}: {e}]"
        pages.append(f"=== Page {index} ===\n{text}")

    return "\n\n".join(pages)
