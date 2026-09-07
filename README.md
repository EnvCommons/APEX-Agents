# APEX-Agents

[![⭐ OpenReward Environment](https://img.shields.io/badge/%E2%AD%90%20OpenReward-Environment-f7e6cc)](https://openreward.ai/GeneralReasoning/APEX-Agents) [![Hugging Face Dataset](https://img.shields.io/badge/Hugging%20Face-Dataset-orange)](https://huggingface.co/datasets/mercor/apex-agents)

## Description

APEX-Agents (AI Productivity Index for Agents) is an environment for evaluating AI agents on realistic workplace tasks across three professional domains: Investment Banking, Law, and Management Consulting. It contains 480 tasks based on 33 realistic workplace scenarios, requiring multi-turn interaction with file exploration, document analysis, and creation of professional deliverables.

## Capabilities

- Multi-turn workplace task completion
- Document analysis and file exploration (PDFs, spreadsheets, Word, PowerPoint)
- Professional deliverable creation
- Sandboxed command execution and file manipulation

## Compute Requirements

Each agent is given an isolated Docker sandbox with 2 CPUs and 2 GB RAM. Task-specific filesystems with PDFs, spreadsheets, and documents are mounted read-only.

## License

[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Tasks

There is one split in this environment:

- **test**: 480 tasks (160 per domain: Investment Banking, Law, Management Consulting)

Tasks include task-specific filesystems based on 33 realistic workplace scenarios ("worlds") populated with relevant files, emails, presentations, and spreadsheets.

## Reward Structure

This is a multi-turn environment with rubric-based evaluation. The agent uses CLI tools to explore files and complete tasks, then submits via `submit_answer` (for console message tasks) or `submit_files` (for file-based outputs). An LLM judge evaluates the submission against 1-10 binary rubric criteria, one call per criterion. ALL criteria must pass for reward=1.0, otherwise reward=0.0.

Each judge call returns a structured verdict — a rationale plus an `is_criteria_true` boolean — so the outcome is carried by that field alone and wording in the rationale never changes it. The rubric criteria and grading instructions are sent as a system message and the submission is fenced off as untrusted data. Judge calls have a per-criterion timeout and are retried; a criterion that still yields no verdict is reported as a grader error rather than as a failed criterion, and does not abort the episode. A submission that could not be fully graded scores 0.0 and is marked with `grading_complete: false` and `grader_error_count` in the metadata, distinguishing it from a submission that was graded and failed. Submission text is capped before grading, with an explicit marker where it was truncated.

Grading metadata reports `passed_count`, `total_count`, per-criterion results, `grading_complete`, `grader_error_count`, `grader_errors`, `submission_truncated` and `judge_model`.

## Data

Data consists of JSON metadata (`tasks_and_rubrics.json`), world filesystems (`world_files/`) containing realistic workplace documents (`filesystem/`) and a mail, chat and calendar corpus (`.apps_data/`) for each of the 33 scenarios, and task-specific files (`task_files/`) for individual tasks. Sourced from [HuggingFace mercor/apex-agents](https://huggingface.co/datasets/mercor/apex-agents). Data is stored on the OpenReward platform.

## Tools

| Tool | Description |
|------|-------------|
| `submit_answer` | Submit text response for console message tasks. Ends the episode. |
| `submit_files` | Submit created/edited files for file-based tasks. Ends the episode. |
| `bash` | Execute shell commands in sandbox. |
| `read` | Read text file contents. |
| `write` | Write files. |
| `edit` | Edit existing files. |
| `grep` | Search file contents. |
| `glob` | Find files by pattern. |
| `ls` | List directory contents. |
| `excel_read_tab` | Read a worksheet from an Excel file. |
| `excel_list_tabs_in_spreadsheet` | List the worksheets in an Excel file. |
| `word_read_document_content` | Read Word document contents. |
| `word_get_document_overview` | Summarise the structure of a Word document. |
| `pdfs_read_pdf_pages` | Read pages of a PDF file. |
| `pdfs_get_document_overview` | Summarise a PDF, including its page count. |
| `powerpoint_read_all` | Read a whole PowerPoint deck. |
| `powerpoint_read_slides` | Read selected slides of a PowerPoint deck. |
| `fmp_market` | Published market and macroeconomic reference data — US Treasury par yield curve rates and CPI. Investment Banking worlds only; coverage boundary in [`market_data_fixtures/COVERAGE.md`](market_data_fixtures/COVERAGE.md). |

## Time Horizon

Multi-turn. Agents explore files and execute commands before submitting final deliverables.

## Environment Difficulty

Tasks are complex multi-step professional workflows that experienced professionals estimate take 1-2 hours to complete. Current leaderboard scores (Pass@1) from [mercor.com/apex](https://www.mercor.com/apex/apex-agents-leaderboard/):

| Model | Pass@1 |
|-------|--------|
| Gemini 3.1 Pro (High) | 33.5% |
| GPT 5.3 Codex (High) | 31.7% |
| Opus 4.6 (High) | 29.8% |
| GPT 5.2 Codex (High) | 27.6% |
| Gemini 3 Flash (High) | 24.0% |
| GPT 5.2 (High) | 23.0% |
| GPT 5.1 Codex (High) | 20.6% |
| GPT 5 Codex (High) | 20.0% |
| Opus 4.5 (High) | 18.4% |
| Gemini 3 Pro (High) | 18.4% |
| GPT 5 (High) | 18.3% |
| Grok 4 | 15.2% |

## Other Environment Requirements

An API key for the rubric judge is required. The judge runs against any OpenAI-compatible endpoint and is selected through `secrets` (or the equivalent upper-case environment variable):

| Secret | Purpose |
| --- | --- |
| `openai_api_key` | OpenAI API key; judge defaults to `gpt-5-mini` |
| `openrouter_api_key` | OpenRouter API key; judge defaults to `openai/gpt-5.6-luna` and base URL `https://openrouter.ai/api/v1` |
| `judge_model` | Overrides the judge model for either provider |
| `judge_base_url` | Overrides the judge endpoint |
| `judge_timeout` | Per-criterion call timeout in seconds (default 180) |
| `judge_max_attempts` | Attempts per criterion before a grader error (default 4) |
| `judge_max_submission_chars` | Submission cap handed to the judge (default 120000) |

An OpenRouter key takes precedence when both are present.

## Safety

Agents in APEX-Agents operate within sandboxed environments with read-only data mounts. The environment does not present direct safety risks.

## Citation

```bibtex
@misc{vidgen2026apexagents,
  title={APEX--Agents},
  author={Vidgen, Bertie and Mann, Austin and Fennelly, Abby and Wright Stanly, John and Rothman, Lucas and Burstein, Marco and Benchek, Julien and Ostrofsky, David and Ravichandran, Anirudh and Sur, Debnil and Venugopal, Neel and Hsia, Alannah and Robinson, Isaac and Huang, Calix and Varones, Olivia and Khan, Daniyal and Haines, Michael and Richards, Zach and Mahapatra, Chirag and Foody, Brendan and Nitski, Osvald},
  year={2026},
  howpublished={arXiv},
  url={https://arxiv.org/abs/2601.14242}
}
```
