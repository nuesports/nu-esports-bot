import ast
from pathlib import Path


def loaded_cogs() -> list[str]:
    """bot.py's cogs_list, read without importing it -- that would start the bot."""
    tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "cogs_list":
            return ast.literal_eval(node.value)
    raise AssertionError("bot.py has no cogs_list")


def test_every_cog_is_loaded():
    # a cog left out of the list never loads, and nothing else would notice
    cogs = sorted(path.stem for path in Path("cogs").glob("*.py"))
    assert sorted(loaded_cogs()) == cogs
