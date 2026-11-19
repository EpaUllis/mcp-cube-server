from __future__ import annotations
from typing import Annotated, Any, Literal, Optional, Union
import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ClientCapabilities, ElicitationCapability, TextContent, EmbeddedResource, TextResourceContents
import jwt
from pydantic import BaseModel, Field
import requests
import json
import logging
import yaml
import os
import uuid
import time


def data_to_yaml(data: Any) -> str:
    return yaml.dump(data, indent=2, sort_keys=False)


class CubeClient:
    Route = Literal["meta", "load"]
    # Cube answers "Continue wait" while the warehouse runs a query. Queries can take
    # longer than the original 10 seconds; past the limit the tool says the query is still
    # running. CUBE_MAX_WAIT_SECONDS overrides the limit.
    max_wait_time = int(os.getenv("CUBE_MAX_WAIT_SECONDS", "90"))
    # Without a timeout an unreachable Cube hangs a request forever. Connect and read are
    # separate so a slow answer from a reachable Cube is not cut off by the connect limit.
    # Cube may hold one answer open while a query runs, so the read limit defaults to the
    # wait limit: no query the wait would allow is cut off earlier by the read.
    connect_timeout = int(os.getenv("CUBE_REQUEST_TIMEOUT_SECONDS", "10"))
    read_timeout = int(os.getenv("CUBE_READ_TIMEOUT_SECONDS", str(max_wait_time)))
    request_backoff = 1
    API_PREFIX = "/cubejs-api/v1"

    def __init__(
        self,
        endpoint: str,
        api_secret: Optional[str],
        token_payload: dict,
        logger: logging.Logger,
        token: Optional[str] = None,
    ):
        # Cube's REST API lives under /cubejs-api/v1. Append it if the caller
        # passed the bare host so existing configs (CUBE_ENDPOINT=http://localhost:4000)
        # keep working.
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith(self.API_PREFIX):
            endpoint = endpoint + self.API_PREFIX
        self.endpoint = endpoint
        self.api_secret = api_secret
        self.token_payload = token_payload
        # A pre-minted token is used as-is; otherwise one is minted from the secret.
        self.static_token = token
        self.token = None
        self.logger = logger
        # No call to Cube here: the server must start even when Cube is unreachable.
        # Every tool call fetches what it needs, and reports the error if Cube is down.
        self._refresh_token()

    def _generate_token(self):
        if self.static_token:
            return self.static_token
        if not self.api_secret:
            raise ValueError("no credential: neither a pre-minted token nor an API secret was given")
        return jwt.encode(self.token_payload, self.api_secret, algorithm="HS256")

    def _refresh_token(self):
        self.token = self._generate_token()

    def _get(self, url, params):
        return requests.get(url, headers={"Authorization": self.token}, params=params,
                            timeout=(self.connect_timeout, self.read_timeout))

    def _request(self, route: Route, **params):
        request_time = time.time()
        url = f"{self.endpoint}/{route}"
        serialized_params = {k: json.dumps(v) for k, v in params.items()}

        try:
            response = self._get(url, serialized_params)

            # Handle "continue wait" responses
            while response.json().get("error") == "Continue wait":
                if time.time() - request_time > self.max_wait_time:
                    self.logger.warning(f"Still running after {self.max_wait_time} seconds")
                    # Each poll is this same request again; Cube answers it from the run in progress.
                    # Whether a resend after the connector stopped polling still joins that run is
                    # not verified (live test 2026-11-18 inconclusive), so the text does not claim it.
                    return {"still_running": True, "error": (
                        f"Still running in Cube after {self.max_wait_time} seconds. Right after the "
                        "warehouse has been idle, starting it takes 1 to 2 minutes on its own. Tell the "
                        "user it is still running and ask whether to keep waiting. If yes, send the "
                        "identical query again straight away. If not, offer something smaller (a shorter "
                        "date range, fewer dimensions)."
                    )}
                self.logger.warning(f"Request incomplete, polling again in {self.request_backoff} second(s)")
                time.sleep(self.request_backoff)
                response = self._get(url, serialized_params)

            # Handle 403 responses by trying to refresh the token once
            if response.status_code == 403:
                self.logger.warning("Received 403, attempting token refresh")
                self._refresh_token()
                response = self._get(url, serialized_params)

            if response.status_code != 200:
                self.logger.error(f"Request failed with error: {str(response.json().get('error'))}")

            return response.json()

        except Exception as e:
            self.logger.error(f"Request failed with error: {str(e)}")
            return {"error": f"Request failed: {str(e)}"}

    def describe(self):
        return self._request("meta")

    def _cast_numerics(self, response):
        if response.get("data") and response.get("annotation"):
            # Find which keys are numeric
            numeric_keys = set()
            dimensions_and_measures = dict(
                response["annotation"].get("dimensions", {}), **response["annotation"].get("measures", [])
            )
            for column_name, column in dimensions_and_measures.items():
                if column.get("type") == "number":
                    numeric_keys.add(column_name)
            # Cast numeric values to numbers
            for row in response["data"]:
                for key in numeric_keys:
                    try:
                        row[key] = float(row[key])
                        if row[key].is_integer():
                            row[key] = int(row[key])
                    except (ValueError, TypeError):
                        pass
        return response

    def query(self, query, cast_numerics=True):
        response = self._request("load", query=query)
        if cast_numerics:
            response = self._cast_numerics(response)
        return response


FilterOperator = Literal[
    "equals", "notEquals", "contains", "notContains", "startsWith", "notStartsWith",
    "endsWith", "notEndsWith", "gt", "gte", "lt", "lte", "set", "notSet",
    "inDateRange", "notInDateRange", "beforeDate", "beforeOrOnDate", "afterDate", "afterOrOnDate",
]


class Filter(BaseModel):
    """One Cube REST filter. Several filters in a query are combined with AND."""

    member: str = Field(..., description="Dimension or measure to filter on, e.g. orders.status")
    operator: FilterOperator = Field(
        ...,
        description="equals/notEquals: exact match. contains/notContains, startsWith, endsWith: text match, "
        "case-insensitive. gt/gte/lt/lte: numbers. set/notSet: not null / null, no values. "
        "inDateRange/notInDateRange: two ISO dates. beforeDate/afterDate (and the OrOn variants): one ISO date.",
    )
    values: list[str] = Field(
        [], description="Values as strings. With several values a row matches if it matches any of them."
    )


class KeepWaiting(BaseModel):
    keep_waiting: bool = Field(True, description="Keep waiting for this query")


# How many times the person can say "keep waiting" before the tool gives up for this call.
MAX_EXTENSIONS = int(os.getenv("CUBE_MAX_EXTENSIONS", "3"))


class TimeDimension(BaseModel):
    dimension: str = Field(..., description="Name of the time dimension")
    granularity: Literal["second", "minute", "hour", "day", "week", "month", "quarter", "year"] = Field(
        ..., description="Time granularity"
    )
    dateRange: Union[list[str], str] = Field(
        ...,
        description="Pair of dates ISO dates representing the start and end of the range. Alternatively, a string representing a relative date range of the form: 'last N days', 'today', 'yesterday', 'last year', etc.",
    )

    model_config = {"exclude_none": True}


class Query(BaseModel):

    measures: list[str] = Field([], description="Names of measures to query")
    dimensions: list[str] = Field([], description="Names of dimensions to group by")
    timeDimensions: list[TimeDimension] = Field([], description="Time dimensions to group by")
    filters: list[Filter] = Field(
        [],
        description="Filters, applied in the warehouse before anything is returned. To look at a subset, "
        "filter on it (e.g. orders.status equals 'shipped'); never fetch every row to search the result.",
    )
    limit: Optional[int] = Field(500, description="Maximum number of rows to return. Defaults to 500")
    offset: Optional[int] = Field(0, description="Number of rows to skip. Defaults to 0")
    order: dict[str, Literal["asc", "desc"]] = Field(
        {}, description="Optional ordering of the results. The order is sensitive to the order of keys."
    )
    ungrouped: bool = Field(
        False,
        description="Return results without grouping by dimensions. Instead, return all rows. This can be useful for fetching a single row by its ID as well.",
    )

    model_config = {"exclude_none": True}


def build_server(credentials, logger) -> FastMCP:
    mcp = FastMCP("Cube.dev")

    client = CubeClient(**credentials, logger=logger)

    @mcp.resource("context://data_description")
    def data_description() -> str:
        """Describe the data available in Cube."""
        meta = client.describe()
        if error := meta.get("error"):
            logger.error("Error in data_description: %s\n\n%s", error, meta.get("stack"))
            logger.error("Full response: %s", json.dumps(meta))
            return f"Error: Description of the data is not available: {error}, {meta}"

        description = [
            {
                "name": cube.get("name"),
                "title": cube.get("title"),
                "description": cube.get("description"),
                "dimensions": [
                    {
                        "name": dimension.get("name"),
                        "title": dimension.get("shortTitle") or dimension.get("title"),
                        "description": dimension.get("description"),
                    }
                    for dimension in cube.get("dimensions", [])
                ],
                "measures": [
                    {
                        "name": measure.get("name"),
                        "title": measure.get("shortTitle") or measure.get("title"),
                        "description": measure.get("description"),
                    }
                    for measure in cube.get("measures", [])
                ],
            }
            for cube in meta.get("cubes", [])
        ]
        return "Here is a description of the data available via the read_data tool:\n\n" + yaml.dump(
            description, indent=2, sort_keys=True
        )

    @mcp.tool("describe_data")
    def describe_data() -> str:
        """Describe the data available in Cube."""
        return data_description()

    def can_ask(ctx: Context) -> bool:
        try:
            return ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability()))
        except Exception:
            return False

    @mcp.tool("read_data")
    async def read_data(query: Query, ctx: Context) -> str:
        """Read data from Cube. Filter in the query (filters) rather than fetching rows and searching
        them: to answer about one value of a dimension, filter on that value. If a query is still running
        after the wait limit, the tool says so; tell the user rather than giving up."""
        try:
            query_dict = query.model_dump(by_alias=True, exclude_none=True)
            logger.info("read_data called with query: %s", json.dumps(query_dict))
            # requests blocks; run it off the event loop so the server stays responsive.
            response = await anyio.to_thread.run_sync(client.query, query_dict)
            extensions = 0
            while response.get("still_running") and extensions < MAX_EXTENSIONS and can_ask(ctx):
                # The client shows this to the person; the model cannot answer it.
                waited = (extensions + 1) * client.max_wait_time
                try:
                    answer = await ctx.elicit(
                        f"This Cube query is still running after {waited} seconds. "
                        f"Keep waiting up to {client.max_wait_time} seconds more?",
                        KeepWaiting,
                    )
                except Exception as e:
                    logger.warning("elicitation failed: %s", e)
                    break
                if not (answer.action == "accept" and answer.data.keep_waiting):
                    return ("Stopped waiting: the user chose not to wait longer. Offer something smaller "
                            "(a shorter date range, fewer dimensions) or to try again later.")
                extensions += 1
                response = await anyio.to_thread.run_sync(client.query, query_dict)
            if error := response.get("error"):
                logger.error("Error in read_data: %s\n\n%s", error, response.get("stack"))
                logger.error("Full response: %s", json.dumps(response))
                return f"Error: {error}"
            data = response.get("data", [])
            logger.info("read_data returned %s rows", len(data))

            data_id = str(uuid.uuid4())

            @mcp.resource(f"data://{data_id}")
            def data_resource() -> str:
                return json.dumps(data)

            logger.info("Added results as resource with ID: %s", data_id)

            output = {
                "type": "data",
                "data_id": data_id,
                "data": data,
            }
            return data_to_yaml(output)

        except Exception as e:
            logger.error("Error in read_data: %s", str(e))
            return f"Error: {str(e)}"

    return mcp


def main(credentials, logger):
    mcp = build_server(credentials, logger)
    logger.info("Starting Cube MCP server")
    mcp.run()
