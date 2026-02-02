import asyncio
import json
import os

from openai import AsyncOpenAI
from openreward import AsyncOpenReward

MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-5.2")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]


async def main() -> None:
    or_client = AsyncOpenReward()
    oai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    environment = or_client.environments.get(
        name="local/apex-agents", base_url="http://localhost:8080"
    )

    tasks = await environment.list_tasks(split="test")
    tools = await environment.list_tools(format="openai")

    print(f"Found {len(tasks)} tasks")

    # Test first console message task
    task = tasks[0]
    print(f"\n=== Testing Console Message Task ===")
    print(f"Task ID: {task.task_spec['task_id']}")
    print(f"Domain: {task.task_spec['domain']}")

    await run_task(environment, oai_client, task, tools)

async def run_task(environment, oai_client, task, tools) -> None:
    """Run a single task with the agent."""
    finished = False
    turn_count = 0
    max_turns = 200  # Prevent infinite loops

    async with environment.session(
        task=task, secrets={"openai_api_key": OPENAI_API_KEY}
    ) as session:
        prompt = await session.get_prompt()
        input_list = [{"role": "system", "content": "Always explore the file system before answering"}, {"role": "user", "content": prompt[0].text}]

        print(f"\n--- Initial Prompt ---")
        print(prompt)

        while not finished and turn_count < max_turns:
            turn_count += 1
            print(f"\n--- Turn {turn_count} ---")

            response = await oai_client.responses.create(
                model=MODEL_NAME,
                tools=tools,
                input=input_list,
            )

            # Process response
            for item in response.output:
                input_list.append(item.model_dump())

                if item.type == "function_call":
                    print(f"Tool Call: {item.name}")
                    print(f"Arguments: {item.arguments}")

                    tool_result = await session.call_tool(
                        item.name,
                        json.loads(str(item.arguments)),
                    )
                    finished = tool_result.finished

                    # Add tool result to input
                    tool_output = {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": (
                            tool_result.blocks[0].text if tool_result.blocks else ""
                        ),
                    }
                    input_list.append(tool_output)

                    print(f"Tool Output: {tool_output['output'][:200]}...")
                    print(f"Reward: {tool_result.reward}")
                    print(f"Finished: {finished}")

                elif item.type == "text":
                    print(f"Model Response: {item.text[:200]}...")

            # If no tool call, break (model might be stuck)
            if not any(i.type == "function_call" for i in response.output):
                print("No tool call in response, ending task")
                break

        print(f"\nTask completed in {turn_count} turns")


if __name__ == "__main__":
    asyncio.run(main())
