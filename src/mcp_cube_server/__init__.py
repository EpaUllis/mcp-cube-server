import argparse
import asyncio
import json
import logging
import os

import dotenv

from . import server
from .remote import RemoteConfig, _env_bool


def args_to_kwargs(unknown):
    extras = {}
    i = 0
    while i < len(unknown):
        if unknown[i].startswith("--"):
            key = unknown[i][2:]  # Remove '--' prefix
            if i + 1 >= len(unknown) or unknown[i + 1].startswith("--"):
                extras[key] = True  # Flag argument
                i += 1
            else:
                extras[key] = unknown[i + 1]  # Key-value pair
                i += 2
        else:
            i += 1

    return extras


def main():
    """Main entry point for the package."""
    parser = argparse.ArgumentParser(description="Cube MCP Server")
    parser.add_argument(
        "--log_dir", required=False, default=None, help="Directory to log to"
    )
    parser.add_argument(
        "--log_level", required=False, default="INFO", help="Logging level"
    )

    dotenv.load_dotenv()

    required = {
        "endpoint": os.getenv("CUBE_ENDPOINT"),
        "api_secret": os.getenv("CUBE_API_SECRET"),
        "token": os.getenv("CUBE_TOKEN"),
        "token_payload": os.getenv("CUBE_TOKEN_PAYLOAD", "{}"),
    }

    parser.add_argument(
        "--endpoint", required=not required["endpoint"], default=required["endpoint"]
    )
    # Two ways to authenticate, and exactly one is needed:
    #   --token / CUBE_TOKEN            a JWT somebody else minted. The process
    #                                   then holds a credential with fixed claims,
    #                                   never the key that mints credentials.
    #   --api_secret / CUBE_API_SECRET  the signing secret; the server mints its
    #                                   own JWT from CUBE_TOKEN_PAYLOAD (+ extra
    #                                   --key value args). Local development only.
    parser.add_argument("--token", required=False, default=required["token"])
    parser.add_argument("--api_secret", required=False, default=required["api_secret"])
    # Remote mode is opt-in. stdio (the default) is the local server, unchanged.
    parser.add_argument("--transport", choices=["stdio", "streamable-http"],
                        default=os.getenv("MCP_TRANSPORT", "stdio"))

    args, unknown = parser.parse_known_args()
    if not args.token and not args.api_secret:
        parser.error(
            "no credential: set CUBE_TOKEN (a pre-minted JWT) or CUBE_API_SECRET "
            "(the signing secret), or pass --token / --api_secret"
        )
    additional_kwargs = args_to_kwargs(unknown)

    token_payload = json.loads(required["token_payload"])
    for key, value in additional_kwargs.items():
        token_payload[key] = value

    logger = logging.getLogger(__name__)
    logger.propagate = False
    logger.setLevel(args.log_level)
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if args.log_dir:
        file_handler = logging.FileHandler(
            os.path.join(args.log_dir, "mcp_cube_server.log")
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    remote = None
    if args.transport == "streamable-http":
        # Over HTTP anyone who can reach the port can call the tools, so OAuth is the
        # default; MCP_AUTH=none turns it off deliberately (local testing only).
        auth_mode = os.getenv("MCP_AUTH", "oauth")
        if auth_mode not in ("oauth", "none"):
            parser.error(f"MCP_AUTH={auth_mode!r}: use oauth or none")
        if auth_mode == "oauth":
            try:
                remote = RemoteConfig.from_env()
            except ValueError as e:
                parser.error(str(e))
            if not args.api_secret:
                parser.error("OAuth mode mints a Cube JWT per person and needs CUBE_API_SECRET")
            if args.token:
                parser.error("OAuth mode does not use CUBE_TOKEN: unset it")

    try:
        credentials = {
            "endpoint": args.endpoint,
            "api_secret": args.api_secret,
            "token": args.token,
            "token_payload": token_payload,
        }
    except json.JSONDecodeError:
        logger.error("Invalid JSON in token_payload: %s", args.token_payload)
        return

    server.main(
        credentials,
        logger,
        transport=args.transport,
        remote=remote,
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8000")),
        stateless_http=_env_bool("MCP_STATELESS_HTTP"),
    )


# Optionally expose other important items at package level
__all__ = ["main", "server"]

if __name__ == "__main__":
    main()
