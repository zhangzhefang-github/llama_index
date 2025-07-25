import inspect
import re
import uuid
from typing import Awaitable, Callable, List, Sequence, Union, Optional

from llama_index.core.agent.workflow.base_agent import BaseWorkflowAgent
from llama_index.core.agent.workflow.workflow_events import (
    AgentInput,
    AgentOutput,
    AgentStream,
    ToolCallResult,
)
from llama_index.core.base.llms.types import ChatResponse
from llama_index.core.bridge.pydantic import BaseModel, Field
from llama_index.core.llms import ChatMessage
from llama_index.core.llms.llm import ToolSelection, LLM
from llama_index.core.llms.function_calling import FunctionCallingLLM
from llama_index.core.memory import BaseMemory
from llama_index.core.objects import ObjectRetriever
from llama_index.core.prompts import BasePromptTemplate, PromptTemplate
from llama_index.core.tools import BaseTool, FunctionTool
from llama_index.core.workflow import Context

DEFAULT_CODE_ACT_PROMPT = """You are a helpful AI assistant that can write and execute Python code to solve problems.

You will be given a task to perform. You should output:
- Python code wrapped in <execute>...</execute> tags that provides the solution to the task, or a step towards the solution. Any output you want to extract from the code should be printed to the console.
- Text to be shown directly to the user, if you want to ask for more information or provide the final answer.
- If the previous code execution can be used to respond to the user, then respond directly (typically you want to avoid mentioning anything related to the code execution in your response).

## Response Format:
Example of proper code format:
<execute>
import math

def calculate_area(radius):
    return math.pi * radius**2

# Calculate the area for radius = 5
area = calculate_area(5)
print(f"The area of the circle is {area:.2f} square units")
</execute>

In addition to the Python Standard Library and any functions you have already written, you can use the following functions:
{tool_descriptions}

Variables defined at the top level of previous code snippets can be also be referenced in your code.

## Final Answer Guidelines:
- When providing a final answer, focus on directly answering the user's question
- Avoid referencing the code you generated unless specifically asked
- Present the results clearly and concisely as if you computed them directly
- If relevant, you can briefly mention general methods used, but don't include code snippets in the final answer
- Structure your response like you're directly answering the user's query, not explaining how you solved it

Reminder: Always place your Python code between <execute>...</execute> tags when you want to run code. You can include explanations and other content outside these tags.
"""

EXECUTE_TOOL_NAME = "execute"


class CodeActAgent(BaseWorkflowAgent):
    """
    A workflow agent that can execute code.
    """

    scratchpad_key: str = "scratchpad"

    code_execute_fn: Union[Callable, Awaitable] = Field(
        description=(
            "The function to execute code. Required in order to execute code generated by the agent.\n"
            "The function protocol is as follows: async def code_execute_fn(code: str) -> Dict[str, Any]"
        ),
    )

    code_act_system_prompt: Union[str, BasePromptTemplate] = Field(
        default=DEFAULT_CODE_ACT_PROMPT,
        description="The system prompt for the code act agent.",
        validate_default=True,
    )

    def __init__(
        self,
        code_execute_fn: Union[Callable, Awaitable],
        name: str = "code_act_agent",
        description: str = "A workflow agent that can execute code.",
        system_prompt: Optional[str] = None,
        tools: Optional[List[Union[BaseTool, Callable]]] = None,
        tool_retriever: Optional[ObjectRetriever] = None,
        can_handoff_to: Optional[List[str]] = None,
        llm: Optional[LLM] = None,
        code_act_system_prompt: Union[
            str, BasePromptTemplate
        ] = DEFAULT_CODE_ACT_PROMPT,
    ):
        tools = tools or []
        tools.append(  # type: ignore
            FunctionTool.from_defaults(code_execute_fn, name=EXECUTE_TOOL_NAME)  # type: ignore
        )
        if isinstance(code_act_system_prompt, str):
            if system_prompt:
                code_act_system_prompt += "\n" + system_prompt
            code_act_system_prompt = PromptTemplate(code_act_system_prompt)
        elif isinstance(code_act_system_prompt, BasePromptTemplate):
            if system_prompt:
                code_act_system_str = code_act_system_prompt.get_template()
                code_act_system_str += "\n" + system_prompt
            code_act_system_prompt = PromptTemplate(code_act_system_str)

        super().__init__(
            name=name,
            description=description,
            system_prompt=system_prompt,
            tools=tools,
            tool_retriever=tool_retriever,
            can_handoff_to=can_handoff_to,
            llm=llm,
            code_act_system_prompt=code_act_system_prompt,
            code_execute_fn=code_execute_fn,
        )

    def _get_tool_fns(self, tools: Sequence[BaseTool]) -> List[Callable]:
        """Get the tool functions while validating that they are valid tools for the CodeActAgent."""
        callables = []
        for tool in tools:
            if (
                tool.metadata.name == "handoff"
                or tool.metadata.name == EXECUTE_TOOL_NAME
            ):
                continue

            if isinstance(tool, FunctionTool):
                if tool.requires_context:
                    raise ValueError(
                        f"Tool {tool.metadata.name} requires context. "
                        "CodeActAgent only supports tools that do not require context."
                    )

                callables.append(tool.real_fn)
            else:
                raise ValueError(
                    f"Tool {tool.metadata.name} is not a FunctionTool. "
                    "CodeActAgent only supports Functions and FunctionTools."
                )

        return callables

    def _extract_code_from_response(self, response_text: str) -> Optional[str]:
        """
        Extract code from the LLM response using XML-style <execute> tags.

        Args:
            response_text: The LLM response text

        Returns:
            Extracted code or None if no code found

        """
        # Match content between <execute> and </execute> tags
        execute_pattern = r"<execute>(.*?)</execute>"
        execute_matches = re.findall(execute_pattern, response_text, re.DOTALL)

        if execute_matches:
            return "\n\n".join([x.strip() for x in execute_matches])

        return None

    def _get_tool_descriptions(self, tools: Sequence[BaseTool]) -> str:
        """
        Generate tool descriptions for the system prompt using tool metadata.

        Args:
            tools: List of available tools

        Returns:
            Tool descriptions as a string

        """
        tool_descriptions = []

        tool_fns = self._get_tool_fns(tools)
        for fn in tool_fns:
            signature = inspect.signature(fn)
            fn_name: str = fn.__name__
            docstring: Optional[str] = inspect.getdoc(fn)

            tool_description = f"def {fn_name}{signature!s}:"
            if docstring:
                tool_description += f'\n  """\n{docstring}\n  """\n'

            tool_description += "\n  ...\n"
            tool_descriptions.append(tool_description)

        return "\n\n".join(tool_descriptions)

    async def take_step(
        self,
        ctx: Context,
        llm_input: List[ChatMessage],
        tools: Sequence[BaseTool],
        memory: BaseMemory,
    ) -> AgentOutput:
        """Take a single step with the code act agent."""
        if not self.code_execute_fn:
            raise ValueError("code_execute_fn must be provided for CodeActAgent")

        # Get current scratchpad
        scratchpad: List[ChatMessage] = await ctx.store.get(
            self.scratchpad_key, default=[]
        )
        current_llm_input = [*llm_input, *scratchpad]

        # Create a system message with tool descriptions
        tool_descriptions = self._get_tool_descriptions(tools)
        system_prompt = self.code_act_system_prompt.format(
            tool_descriptions=tool_descriptions
        )

        # Add or overwrite system message
        has_system = False
        for i, msg in enumerate(current_llm_input):
            if msg.role.value == "system":
                current_llm_input[i] = ChatMessage(role="system", content=system_prompt)
                has_system = True
                break

        if not has_system:
            current_llm_input.insert(
                0, ChatMessage(role="system", content=system_prompt)
            )

        # Write the input to the event stream
        ctx.write_event_to_stream(
            AgentInput(input=current_llm_input, current_agent_name=self.name)
        )

        # For now, only support the handoff tool
        # All other tools should be part of the code execution
        if any(tool.metadata.name == "handoff" for tool in tools):
            if not isinstance(self.llm, FunctionCallingLLM):
                raise ValueError("llm must be a function calling LLM to use handoff")

            tools = [tool for tool in tools if tool.metadata.name == "handoff"]
            response = await self.llm.astream_chat_with_tools(
                tools, chat_history=current_llm_input
            )
        else:
            response = await self.llm.astream_chat(current_llm_input)

        # Initialize for streaming
        last_chat_response = ChatResponse(message=ChatMessage())
        full_response_text = ""

        # Process streaming response
        async for last_chat_response in response:
            delta = last_chat_response.delta or ""
            full_response_text += delta

            # Create a raw object for the event stream
            raw = (
                last_chat_response.raw.model_dump()
                if isinstance(last_chat_response.raw, BaseModel)
                else last_chat_response.raw
            )

            # Write delta to the event stream
            ctx.write_event_to_stream(
                AgentStream(
                    delta=delta,
                    response=full_response_text,
                    # We'll add the tool call after processing the full response
                    tool_calls=[],
                    raw=raw,
                    current_agent_name=self.name,
                )
            )

        # Extract code from the response
        code = self._extract_code_from_response(full_response_text)

        # Create a tool call for executing the code if code was found
        tool_calls = []
        if code:
            tool_id = str(uuid.uuid4())

            tool_calls = [
                ToolSelection(
                    tool_id=tool_id,
                    tool_name=EXECUTE_TOOL_NAME,
                    tool_kwargs={"code": code},
                )
            ]

        if isinstance(self.llm, FunctionCallingLLM):
            extra_tool_calls = self.llm.get_tool_calls_from_response(
                last_chat_response, error_on_no_tool_call=False
            )
            tool_calls.extend(extra_tool_calls)

        # Add the response to the scratchpad
        message = ChatMessage(role="assistant", content=full_response_text)
        scratchpad.append(message)
        await ctx.store.set(self.scratchpad_key, scratchpad)

        # Create the raw object for the output
        raw = (
            last_chat_response.raw.model_dump()
            if isinstance(last_chat_response.raw, BaseModel)
            else last_chat_response.raw
        )

        return AgentOutput(
            response=message,
            tool_calls=tool_calls,
            raw=raw,
            current_agent_name=self.name,
        )

    async def handle_tool_call_results(
        self, ctx: Context, results: List[ToolCallResult], memory: BaseMemory
    ) -> None:
        """Handle tool call results for code act agent."""
        scratchpad: List[ChatMessage] = await ctx.store.get(
            self.scratchpad_key, default=[]
        )

        # handle code execution and handoff
        for tool_call_result in results:
            # Format the output as a tool response message
            if tool_call_result.tool_name == EXECUTE_TOOL_NAME:
                code_result = f"Result of executing the code given:\n\n{tool_call_result.tool_output.content}"
                scratchpad.append(
                    ChatMessage(
                        role="user",
                        content=code_result,
                    )
                )
            elif tool_call_result.tool_name == "handoff":
                scratchpad.append(
                    ChatMessage(
                        role="tool",
                        blocks=tool_call_result.tool_output.blocks,
                        additional_kwargs={"tool_call_id": tool_call_result.tool_id},
                    )
                )
            else:
                raise ValueError(f"Unknown tool name: {tool_call_result.tool_name}")

        await ctx.store.set(self.scratchpad_key, scratchpad)

    async def finalize(
        self, ctx: Context, output: AgentOutput, memory: BaseMemory
    ) -> AgentOutput:
        """
        Finalize the code act agent.

        Adds all in-progress messages to memory.
        """
        scratchpad: List[ChatMessage] = await ctx.store.get(
            self.scratchpad_key, default=[]
        )
        await memory.aput_messages(scratchpad)

        # reset scratchpad
        await ctx.store.set(self.scratchpad_key, [])

        return output
