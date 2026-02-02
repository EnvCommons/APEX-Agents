# APEX-AGENTS Environment

OpenReward environment implementation of the [APEX-AGENTS benchmark](https://huggingface.co/datasets/mercor/apex-agents) - a professional services evaluation suite for testing AI agents on realistic workplace tasks.

## Overview

APEX-AGENTS is a comprehensive benchmark featuring **480 tasks** across three professional domains:
- **Investment Banking** (160 tasks)
- **Law** (160 tasks)
- **Management Consulting** (160 tasks)

Tasks require multi-turn interaction with realistic workplace environments, including file exploration, document analysis, and creation of professional deliverables (Excel models, Word documents, PowerPoint presentations).

## Features

- **Multi-turn sandboxed environment** with CLI tools (bash, read, write, edit, grep, glob, ls)
- **Rubric-based LLM grading** using gpt-5-mini (1-10 binary criteria per task)
- **Mixed output formats**:
  - 87% console messages (`message_in_console`)
  - 13% file creation/editing (`make_new_doc`, `edit_existing_sheet`, etc.)
- **Realistic task files**: 142 task-specific filesystems with PDFs, spreadsheets, documents
- **Professional scenarios**: Based on 33 realistic "worlds" simulating actual workplace environments

## Installation

### Prerequisites
- Python 3.11+
- OpenReward SDK
- OpenAI API key

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export OPENAI_API_KEY=your_key_here

# Start local server
python server.py
```

### Docker

```bash
# Build image
docker build -t apex-agents:latest .

# Run container
docker run -p 8080:8080 apex-agents:latest
```

## Data Requirements

This environment requires data to be uploaded to OpenReward namespace storage. See [DATA_UPLOAD.md](DATA_UPLOAD.md) for detailed instructions.

**Required uploads**:
- `tasks_and_rubrics.json` (1 MB) - Task metadata and rubric criteria
- `task_files/` (118 MB) - 142 task-specific filesystem snapshots

## Usage

### Basic Agent Testing

```python
import asyncio
import json
import os

from openai import AsyncOpenAI
from openreward import AsyncOpenReward

async def main():
    or_client = AsyncOpenReward()
    oai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

    environment = or_client.environments.get(
        name="EnvCommons/apex-agents"
    )

    tasks = await environment.list_tasks(split="test")
    tools = await environment.list_tools(format="openai")

    # Run first task
    async with environment.session(
        task=tasks[0],
        secrets={"openai_api_key": os.environ["OPENAI_API_KEY"]}
    ) as session:
        prompt = await session.get_prompt()
        # ... agent loop with tool calls

asyncio.run(main())
```

See [test_agent.py](test_agent.py) for a complete example.

### Available Tools

**Custom Tools**:
- `submit_answer(answer: str)` - Submit text response for console message tasks
- `submit_files(file_paths: list[str])` - Submit created/edited files for file-based tasks

**Built-in CLI Tools** (from CLIEnvironment):
- `bash` - Execute shell commands in sandbox
- `read` - Read file contents
- `write` - Write files
- `edit` - Edit existing files
- `grep` - Search file contents
- `glob` - Find files by pattern
- `ls` - List directory contents
- `multi_edit` - Edit multiple files
- `todo_write` - Task tracking

## Task Structure

Each task includes:
- **task_id**: Unique identifier
- **domain**: Investment Banking | Law | Management Consulting
- **prompt**: Task instructions for the agent
- **expected_output**: Output type (message_in_console, make_new_doc, etc.)
- **rubric**: 1-10 binary evaluation criteria (ALL must pass)

### Task File Access

Task-specific files are mounted at:
```
/orwd_data/apex-agents/task_files/{task_id}/filesystem/
```

Agents can explore these files using CLI tools before generating responses.

## Evaluation

Submissions are evaluated using **LLM-based rubric grading** with gpt-5-mini:

1. Each task has 1-10 binary criteria
2. Agent submission is evaluated against each criterion
3. **ALL criteria must pass** for reward=1.0
4. Failed criteria result in reward=0.0

Example rubric evaluation output:
```
Rubric Evaluation (3/4 criteria passed):

✓ Criterion 1: Correctly identified the key issue
   Reasoning: The submission accurately identifies...

✗ Criterion 2: Provided quantitative analysis
   Reasoning: The submission lacks specific numbers...

❌ 1 criteria failed.
```

## Environment Architecture

- **Base Class**: `CLIEnvironment` (provides sandboxed CLI tools)
- **Sandbox**: `generalreasoning/python-ds:3.12-tools`
- **Machine Size**: 2 CPU / 2 GB RAM
- **Data Mount**: `/orwd_data/apex-agents/` (read-only)
- **Workspace**: `/workspace/` (writable for agent outputs)

## Performance Notes

- **Task count**: 480 tasks across all domains
- **Average criteria per task**: 4.06 (range: 1-10)
- **Tasks with input files**: 175 (36.5%)
- **Average files per world**: 166
- **Concurrent criterion evaluation**: All rubric criteria evaluated in parallel for performance

## Development

### Running Tests

```bash
# Syntax check
python -m py_compile *.py

# Local server test
python server.py

# Agent integration test (requires OPENAI_API_KEY)
python test_agent.py
```

### Project Structure

```
apex-agents/
├── apexagents.py           # Main environment class
├── cli_environment.py      # CLIEnvironment base (from FPL)
├── utils.py                # Sandbox helpers (from FPL)
├── server.py               # Server wrapper
├── test_agent.py           # Agent testing script
├── requirements.txt        # Python dependencies
├── Dockerfile              # Container definition
├── DATA_UPLOAD.md          # Data upload instructions
└── README.md               # This file
```

## References

- **Dataset**: https://huggingface.co/datasets/mercor/apex-agents
- **OpenReward Documentation**: https://docs.openreward.org/
- **GitHub Repository**: https://github.com/EnvCommons/apex-agents

## License

This implementation follows the licensing of the original APEX-AGENTS dataset (CC-BY 4.0).

## Citation

If you use this environment in your research, please cite the original APEX-AGENTS benchmark:

```bibtex
@misc{vidgen2026apexagents,
  title        = {APEX--Agents},
  author       = {Vidgen, Bertie and Mann, Austin and Fennelly, Abby and Wright Stanly, John and Rothman, Lucas and Burstein, Marco and Benchek, Julien and Ostrofsky, David and Ravichandran, Anirudh and Sur, Debnil and Venugopal, Neel and Hsia, Alannah and Robinson, Isaac and Huang, Calix and Varones, Olivia and Khan, Daniyal and Haines, Michael and Richards, Zach and Mahapatra, Chirag and Foody, Brendan and Nitski, Osvald},
  year         = {2026},
  howpublished = {arXiv},
  url          = {https://arxiv.org/pdf/2601.14242}
}
```
