# APEX-AGENTS

[![OpenReward Environment](https://img.shields.io/badge/%E2%AD%90%20OpenReward-Environment-f7e6cc)](https://openreward.ai/EnvCommons/apex-agents) [![Hugging Face Dataset](https://img.shields.io/badge/Hugging%20Face-Dataset-orange)](https://huggingface.co/datasets/mercor/apex-agents)

## Description

APEX-AGENTS is an environment for evaluating AI agents on realistic workplace tasks across three professional domains: Investment Banking (160 tasks), Law (160 tasks), and Management Consulting (160 tasks). Tasks require multi-turn interaction with workplace environments including file exploration, document analysis, and creation of professional deliverables.

## Capabilities

- Multi-turn workplace task completion
- Document analysis and file exploration
- Professional deliverable creation (Excel models, Word documents, PowerPoint presentations)
- Sandboxed command execution and file manipulation

## Compute Requirements

Agents are given a sandboxed environment with CLI tools (bash, read, write, edit, grep, glob, ls). Sandbox uses 2 CPU / 2 GB RAM.

## License

[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Tasks

There is one split in this environment:

- **test**: 480 tasks (160 per domain: Investment Banking, Law, Management Consulting)

Tasks include task-specific filesystems with PDFs, spreadsheets, and documents based on 33 realistic workplace scenarios.

## Reward Structure

This is a multi-turn environment with rubric-based evaluation. The agent uses CLI tools to explore files and complete tasks, then submits via `submit_answer` (for console messages) or `submit_files` (for file-based outputs). An LLM grader (gpt-5-mini) evaluates against 1-10 binary rubric criteria. ALL criteria must pass for reward=1.0, otherwise reward=0.0.

## Data

Data consists of JSON metadata (`tasks_and_rubrics.json`) and task-specific filesystem snapshots (`task_files/`) sourced from [HuggingFace mercor/apex-agents](https://huggingface.co/datasets/mercor/apex-agents). Data is stored on the OpenReward platform.

## Tools

| Tool | Description |
|------|-------------|
| `submit_answer` | Submit text response for console message tasks. Ends the episode. |
| `submit_files` | Submit created/edited files for file-based tasks. Ends the episode. |
| `bash` | Execute shell commands in sandbox. |
| `read` | Read file contents. |
| `write` | Write files. |
| `edit` | Edit existing files. |
| `grep` | Search file contents. |
| `glob` | Find files by pattern. |
| `ls` | List directory contents. |

## Time Horizon

Multi-turn. Agents explore files and execute commands before submitting final deliverables.

## Environment Difficulty

APEX-AGENTS evaluates professional workplace task completion across investment banking, law, and consulting domains.

## Other Environment Requirements

OpenAI API key required for LLM-based grading. Pass via `secrets={"openai_api_key": "..."}`.

## Safety

Agents in APEX-AGENTS operate within sandboxed environments with read-only data mounts. The environment does not present direct safety risks.

## Citation

```bibtex
@misc{vidgen2026apexagents,
  title={APEX--Agents},
  author={Vidgen, Bertie and Mann, Austin and Fennelly, Abby and Wright Stanly, John and Rothman, Lucas and Burstein, Marco and Benchek, Julien and Ostrofsky, David and Ravichandran, Anirudh and Sur, Debnil and Venugopal, Neel and Hsia, Alannah and Robinson, Isaac and Huang, Calix and Varones, Olivia and Khan, Daniyal and Haines, Michael and Richards, Zach and Mahapatra, Chirag and Foody, Brendan and Nitski, Osvald},
  year={2026},
  howpublished={arXiv},
  url={https://arxiv.org/pdf/2601.14242}
}
```
