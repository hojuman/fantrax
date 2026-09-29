"""Optional AI layer: Claude reads the engine's numbers through read-only tools, adds news and judgment.

Requires `uv sync --extra ai` and ANTHROPIC_API_KEY in .env. Nothing here can act on Fantrax: the tools
only read the local database, and the only outbound call is to the Anthropic API.
"""
