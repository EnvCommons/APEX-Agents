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

This is a multi-turn environment with rubric-based evaluation. The agent uses CLI tools to explore files and complete tasks, then submits via `submit_answer` (for console message tasks) or `submit_files` (for file-based outputs). An LLM grader (gpt-5-mini) evaluates against 1-10 binary rubric criteria. ALL criteria must pass for reward=1.0, otherwise reward=0.0.

## Data

Data consists of JSON metadata (`tasks_and_rubrics.json`) and task-specific filesystem snapshots (`task_files/`) sourced from [HuggingFace mercor/apex-agents](https://huggingface.co/datasets/mercor/apex-agents). Data is stored on the OpenReward platform.

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
| `excel_read` | Read Excel file contents. |
| `excel_list_sheets` | List sheets in an Excel file. |
| `word_read` | Read Word document contents. |
| `pdf_read` | Read PDF file contents. |
| `powerpoint_read` | Read PowerPoint file contents. |
| `powerpoint_list_slides` | List slides in a PowerPoint file. |

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

OpenAI API key required for LLM-based grading. Pass via `secrets={"openai_api_key": "..."}`.

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
