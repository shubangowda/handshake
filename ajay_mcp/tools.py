from mcp.server.fastmcp import FastMCP

from llm import compile_contract
from models import CompilerOutput

mcp = FastMCP("Handshake")


@mcp.tool()
async def create_contract_draft(intent: str) -> dict:
    """
    Create a Handshake contract draft from a user's shopping intent.

    This does NOT authorize or sign the contract.
    It only generates a draft for human review.
    """

    result: CompilerOutput = await compile_contract(intent)

    # TODO : Add REST API call to store the contract draft in the database

    return result.model_dump(mode="json")

@mcp.tool()
async def create_contract_draft(intent: str):
    pass


@mcp.tool()
async def get_contract(contract_id: str):
    pass


@mcp.tool()
async def list_active_contracts():
    pass


@mcp.tool()
async def request_purchase(
    contract_id: str,
    checkout_url: str,
):
    pass