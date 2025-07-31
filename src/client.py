import os, time
import asyncio
import json
from dotenv import load_dotenv
from contextlib import AsyncExitStack
# Add references
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import FunctionTool, MessageRole, ListSortOrder
from azure.identity import DefaultAzureCredential


# Clear the console
os.system('cls' if os.name=='nt' else 'clear')

# Load environment variables from .env file
load_dotenv()
project_endpoint = os.getenv("PROJECT_ENDPOINT")
model_deployment = os.getenv("MODEL_DEPLOYMENT_NAME")

async def connect_to_server(exit_stack: AsyncExitStack):
    server_params = StdioServerParameters(
        command="python",
        args=["C:\\MCP APPLICATION\\MCP-Application\\src\\server.py"],
        env=None
    )

    # Start the MCP server
    stdio_transport = await exit_stack.enter_async_context(stdio_client(server_params))
    stdio, write = stdio_transport
    
    # Create an MCP client session
    session = await exit_stack.enter_async_context(ClientSession(stdio, write))
    await session.initialize()

    # List available tools
    response = await session.list_tools()
    tools = response.tools 
    print("\nConnected to server with tools:", [tool.name for tool in tools]) 

    return session

async def chat_loop(session):

    # Connect to the agents client
    agents_client = AgentsClient(
        endpoint=project_endpoint,
        credential=DefaultAzureCredential(
            exclude_environment_credential=True,
            exclude_managed_identity_credential=True
        )
   )
    

    # List tools available on the server
    response = await session.list_tools()
    tools = response.tools
    

    # Build a function for each tool
    def make_tool_func(tool_name):
        async def tool_func(**kwargs):
            result = await session.call_tool(tool_name, kwargs)
            return result
        
        tool_func.__name__ = tool_name
        return tool_func
    
    functions_dict = {tool.name: make_tool_func(tool.name) for tool in tools}
    mcp_function_tool = FunctionTool(functions=list(functions_dict.values()))
    
    # Create the agent
    agent = agents_client.create_agent(
        model=model_deployment,
        name="mcp-agent-v-r",
        instructions="""
        You are an powerbi semantic model assistant. Here are some general guidelines:
        - you to need help the data analyst to build the semantic models/datasets.
        - you need to generate the dax queries as requested by the user and execute and show the output for it.
            - **Tool Usage Guide:**
        - for 'connect', use this exact server_name and database_name simple json format parameters with out any kwargs. for example connect(server_name = "powerbi://api.powerbi.com/v1.0/myorg/workspace", database_name = "catalog_name")
        - for 'update_column_names', use this exact table_name, old_col_name and new_col_name simple json format parameters with out any kwargs. 
        - for 'connect', use use  server_name and database_name as standalone paraemters instead of kwargs.
        """,
        tools=mcp_function_tool.definitions
   )
    # # Get the agent if it already exists
    # agent = agents_client.get_agent("asst_rYaAidb0xZ9sFsdVPNpguohp"
    # ,tools=mcp_function_tool.definitions)

    # Enable auto function calling
    agents_client.enable_auto_function_calls(tools=mcp_function_tool)
    

    # # Create a thread for the chat session
    thread = agents_client.threads.create()
    

    while True:
        user_input = input("Enter a prompt for the inventory agent. Use 'quit' to exit.\nUSER: ").strip()
        if user_input.lower() == "quit":
            print("Exiting chat.")
            break

        # Invoke the prompt
        message = agents_client.messages.create(
            thread_id=thread.id,
            role=MessageRole.USER,
            content=user_input,
        )
        run = agents_client.runs.create(thread_id=thread.id, agent_id=agent.id)


        # Monitor the run status
        while run.status in ["queued", "in_progress", "requires_action"]:
            time.sleep(1)
            run = agents_client.runs.get(thread_id=thread.id, run_id=run.id)
            tool_outputs = []

            if run.status == "requires_action":

                tool_calls = run.required_action.submit_tool_outputs.tool_calls

                for tool_call in tool_calls:

                    # Retrieve the matching function tool
                    function_name = tool_call.function.name
                    args_json = tool_call.function.arguments
                    kwargs = json.loads(args_json)
                    required_function = functions_dict.get(function_name)

                    # Invoke the function
                    output = await required_function(**kwargs)
                    

                    # Append the output text
                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": output.content[0].text,
                    })
                    
                
                # Submit the tool call output
                agents_client.runs.submit_tool_outputs(thread_id=thread.id, run_id=run.id, tool_outputs=tool_outputs)
                
        # Check for failure
        if run.status == "failed":
            print(f"Run failed: {run.last_error}")

        # Display the response
        messages = agents_client.messages.list(thread_id=thread.id, order=ListSortOrder.ASCENDING)
        for message in messages:
            if message.text_messages:
                last_msg = message.text_messages[-1]
                print(f"{message.role}:\n{last_msg.text.value}\n")
        

    # Delete the agent when done
    print("Cleaning up agents:")
    agents_client.delete_agent(agent.id)
    print("Deleted inventory agent.")


async def main():
    import sys
    exit_stack = AsyncExitStack()
    try:
        session = await connect_to_server(exit_stack)
        await chat_loop(session)
    finally:
        await exit_stack.aclose()

if __name__ == "__main__":
    asyncio.run(main())
