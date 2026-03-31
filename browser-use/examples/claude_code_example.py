"""
Example: Using browser-use with Claude Max plan via Claude Code CLI.

No API key needed — uses your Claude Code CLI authentication.

Requirements:
- Claude Code CLI installed and authenticated (`claude` command available)
- Claude Max subscription (5x or 20x plan)
"""

import asyncio

from browser_use import Agent
from browser_use.llm.claude_code import ChatClaudeCode

llm = ChatClaudeCode(model='sonnet')


async def main():
	agent = Agent(
		task='Go to hackernews and find the top 3 stories. Return their titles.',
		llm=llm,
	)
	result = await agent.run()
	print('\n\nFinal result:')
	print(result.final_result())


if __name__ == '__main__':
	asyncio.run(main())
