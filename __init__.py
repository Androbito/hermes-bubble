"""Hermes Bubble — Hermes plugin entry point.

Registers the `hermes bubble` CLI command (install / uninstall / start / stop / status / config).
All the heavy lifting lives in bubble_cli.py so importing the plugin stays cheap.
"""


def register(ctx):
    from . import bubble_cli

    ctx.register_cli_command(
        name="bubble",
        help="Hold-🌐 voice assistant bubble for macOS (install, start, stop, status, config)",
        description=bubble_cli.DESCRIPTION,
        setup_fn=bubble_cli.setup_parser,
        handler_fn=bubble_cli.main,
    )
