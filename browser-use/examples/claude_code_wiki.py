import asyncio

from browser_use import Agent
from browser_use.llm.claude_code import ChatClaudeCode


async def main():
	agent = Agent(
		task='Go to wikipedia.org and tell me what the featured article is today',
		llm=ChatClaudeCode(model='sonnet'),
	)
	result = await agent.run()
	print(result.final_result())


asyncio.run(main())
