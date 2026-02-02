import os
from typing import Dict, List, Any, Optional
from pydantic import BaseModel

from openreward import OpenReward, SandboxBucketConfig, SandboxSettings
from openreward.environments import (Environment, JSONObject, TextBlock,
                                     ToolOutput, tool, Server)

from utils import (download_text, upload_text)

# Pydantic models for tool inputs
class BashParams(BaseModel, extra="forbid"):
    command: str
    timeout: Optional[float] = 30.0


class GlobParams(BaseModel, extra="forbid"):
    pattern: str
    path: Optional[str] = None


class GrepParams(BaseModel, extra="forbid"):
    pattern: str
    path: Optional[str] = None
    include: Optional[str] = None


class LSParams(BaseModel, extra="forbid"):
    path: str = "."
    ignore: Optional[List[str]] = None


class ReadParams(BaseModel, extra="forbid"):
    file_path: str
    offset: Optional[int] = None
    limit: Optional[int] = None


class WriteParams(BaseModel, extra="forbid"):
    file_path: str
    content: str


class EditParams(BaseModel, extra="forbid"):
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class MultiEditParams(BaseModel, extra="forbid"):
    file_path: str
    edits: List[Dict[str, Any]]


class TodoWriteParams(BaseModel, extra="forbid"):
    todos: List[Dict[str, Any]]


class CLIEnvironment(Environment):
    """
    A CLI Environment that provides standard command-line interface tools.

    This environment gives agents access to 12 essential CLI tools:
    - 4 command line tools: bash, glob, grep, ls
    - 4 file management tools: read, write, edit, multi_edit
    - 1 control flow tools: todo_write
    """

    def __init__(self, task_spec: JSONObject = {}, secrets: dict[str, str] = {}) -> None:
        super().__init__(task_spec)
        self.todos: List[Dict[str, Any]] = []
        
    async def setup(self) -> None:
        await self.sandbox.start()

    async def teardown(self) -> None:
        await self.sandbox.stop()
    
    @tool
    async def bash(self, params: BashParams) -> ToolOutput:
        """Execute bash commands using the computer instance."""
        try:
            output, code = await self.sandbox.run(params.command.strip())
            
            return ToolOutput(
                blocks=[TextBlock(text=f"{output}\n\n(exit {code})")],
                metadata={"output": output, "exit_code": code},
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error executing command: {str(e)}")],
                finished=False
            )

    @tool
    async def glob(self, params: GlobParams) -> ToolOutput:
        """Find files matching a glob pattern using computer instance."""
        try:
            search_path = params.path or "."

            # Use find with glob pattern
            cmd = f"find '{search_path}' -name '{params.pattern}' -type f | sort"

            output, code = await self.sandbox.run(cmd)
            
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"{output}\\n\\n(exit {code})")],
                reward=0.0,
                finished=False
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error in glob search: {str(e)}")],
                finished=False
            )

    @tool
    async def grep(self, params: GrepParams) -> ToolOutput:
        """Search for patterns in files using computer instance grep."""
        try:
            # Build grep command
            search_path = params.path or "."

            if params.include:
                cmd = f"find '{search_path}' -name '{params.include}' -type f -exec grep -Hn '{params.pattern}' {{}} \\;"
            else:
                cmd = f"grep -r -n '{params.pattern}' '{search_path}'"

            output, code = await self.sandbox.run(cmd)
            
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"{output}\\n\\n(exit {code})")],
                reward=0.0,
                finished=False
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error in grep search: {str(e)}")],
                finished=False
            )

    @tool
    async def ls(self, params: LSParams) -> ToolOutput:
        """List files and directories using computer instance."""
        try:
            cmd = f"ls -la '{params.path}'"
            output, code = await self.sandbox.run(cmd)
            
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"{output}\n\n(exit {code})")],
                reward=0.0,
                finished=False
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error listing directory: {str(e)}")],
                finished=False
            )

    @tool
    async def read(self, params: ReadParams) -> ToolOutput:
        """Read file contents using computer instance."""
        try:
            if params.offset and params.limit:
                end_line = params.offset + params.limit
                cmd = f"sed -n '{params.offset},{end_line}p' '{params.file_path}' | cat -n"
                output, code = await self.sandbox.run(cmd)
            elif params.offset:
                cmd = f"tail -n +{params.offset} '{params.file_path}' | cat -n"
                output, code = await self.sandbox.run(cmd)
            elif params.limit:
                cmd = f"head -n {params.limit} '{params.file_path}' | cat -n"
                output, code = await self.sandbox.run(cmd)
            else:
                content = await download_text(self.sandbox, params.file_path)
                lines = content.splitlines()
                output = "\n".join(f"{idx + 1}\t{line}" for idx, line in enumerate(lines))
                if content.endswith("\n") and output:
                    output += "\n"
                code = 0
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"{output}\n\n(exit {code})")],
                reward=0.0,
                finished=False
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error reading file: {str(e)}")],
                finished=False
            )

    @tool
    async def write(self, params: WriteParams) -> ToolOutput:
        """Write content to a file using computer instance."""
        try:
            # Create directory if needed
            dir_name = os.path.dirname(params.file_path)
            if dir_name:
                await self.sandbox.run(f"mkdir -p '{dir_name}'")

            await upload_text(
                self.sandbox,
                params.file_path,
                params.content,
                ensure_trailing_newline=True,
            )
            output = ""
            code = 0
            
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"Successfully wrote to {params.file_path}\n\n(exit {code})")],
                reward=0.0,
                finished=False
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error writing file: {str(e)}")],
                finished=False
            )

    @tool
    async def edit(self, params: EditParams) -> ToolOutput:
        """Perform exact string replacement in a file using computer instance."""
        try:
            # Use sed to perform the replacement
            if params.replace_all:
                # Replace all occurrences
                escaped_old = params.old_string.replace('/', '\\/')
                escaped_new = params.new_string.replace('/', '\\/')
                cmd = f"sed -i 's/{escaped_old}/{escaped_new}/g' '{params.file_path}'"
            else:
                # Replace first occurrence only
                escaped_old = params.old_string.replace('/', '\\/')
                escaped_new = params.new_string.replace('/', '\\/')
                cmd = f"sed -i 's/{escaped_old}/{escaped_new}/' '{params.file_path}'"

            output, code = await self.sandbox.run(cmd)
            
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"Edit completed\\n\\n(exit {code})")],
                reward=0.0,
                finished=False
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error editing file: {str(e)}")],
                finished=False
            )

    @tool
    def multi_edit(self, params: MultiEditParams) -> ToolOutput:
        """Perform multiple edits on a single file."""
        try:
            if not os.path.exists(params.file_path):
                return ToolOutput(
                    metadata={"error": "File does not exist"},
                    blocks=[TextBlock(text="File does not exist")],
                    finished=False
                )
            
            with open(params.file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            total_replacements = 0
            
            for edit in params.edits:
                old_str = edit.get("old_string", "")
                new_str = edit.get("new_string", "")
                replace_all = edit.get("replace_all", False)
                
                if old_str not in content:
                    return ToolOutput(
                        metadata={"error": f"String '{old_str}' not found in file"},
                        blocks=[TextBlock(text=f"String '{old_str}' not found in file")],
                        finished=False
                    )
                
                if replace_all:
                    replacements = content.count(old_str)
                    content = content.replace(old_str, new_str)
                else:
                    replacements = 1
                    content = content.replace(old_str, new_str, 1)
                
                total_replacements += replacements
            
            with open(params.file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return ToolOutput(
                metadata={"total_replacements": total_replacements, "edits_applied": len(params.edits)},
                blocks=[TextBlock(text=f"Successfully applied {len(params.edits)} edits with {total_replacements} total replacements")],
                finished=False
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error in multi-edit: {str(e)}")],
                finished=False
            )

    @tool
    def todo_write(self, params: TodoWriteParams) -> ToolOutput:
        """
        Manage todo list for task planning and progress tracking.
        
        Creates or updates a structured todo list with the following properties:
        - Each todo item must have: id, content, status, priority
        - Status options: "pending", "in_progress", "completed"
        - Priority options: "high", "medium", "low"
        - Only one item should be "in_progress" at a time
        - Mark items "completed" immediately after finishing them
        
        Example usage:
        todos = [
            {"id": "1", "content": "Read project files", "status": "completed", "priority": "high"},
            {"id": "2", "content": "Implement feature X", "status": "in_progress", "priority": "high"},
            {"id": "3", "content": "Write tests", "status": "pending", "priority": "medium"}
        ]
        """
        try:
            self.todos = params.todos
            
            # Format todos for display
            output_lines = ["=== TODO LIST ==="]
            for todo in self.todos:
                status_icon = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}.get(todo.get("status", "pending"), "❓")
                priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(todo.get("priority", "medium"), "⚪")
                
                output_lines.append(f"{status_icon} {priority_icon} {todo.get('content', 'No description')}")
            
            content = "\n".join(output_lines)
            
            return ToolOutput(
                metadata={"todos": self.todos, "count": len(self.todos)},
                blocks=[TextBlock(text=content)],
                finished=False,
                reward=0.0
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error managing todos: {str(e)}")],
                finished=False
            )
