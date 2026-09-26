from openai import AsyncOpenAI
from dotenv import load_dotenv
from models import CompilerOutput, ContractDraft
import os
from prompts import COMPILER_PROMPT


load_dotenv()

client = AsyncOpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
    timeout=15 * 60
)

async def compile_contract(intent: str) -> CompilerOutput:
    response = await client.responses.parse(
        model="gpt-5.6",
        input=[
            {
                "role": "system",
                "content": COMPILER_PROMPT,
            },
            {
                "role": "user",
                "content": intent,
            },
        ],
        text_format=CompilerOutput,
        service_tier="flex"
    )

    return response.output_parsed



