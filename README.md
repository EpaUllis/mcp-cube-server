# Cube MCP Server (fork)

MCP server for interacting with [Cube.dev](https://cube.dev) semantic layers.

**This is a fork of [isaacwasserman/mcp_cube_server](https://github.com/isaacwasserman/mcp_cube_server) 0.0.2** (last upstream release 2025-01-30). Upstream has been inactive for over a year and several bugs prevented the server from working with current MCP clients and Cube versions. This fork is maintained for personal use; PRs welcome.

## What's fixed in this fork (0.1.0)

| # | Bug | Symptom | Fix |
|---|-----|---------|-----|
| 1 | Wrong REST endpoint path | `read_data` returned `Error: Request failed: Expecting value: line 1 column 1 (char 0)` because the server hit `{endpoint}/meta` and `{endpoint}/load` instead of `{endpoint}/cubejs-api/v1/meta` and `…/load`. Cube returned a 404 HTML page, `response.json()` raised `JSONDecodeError`. | `CubeClient.__init__` appends `/cubejs-api/v1` to the endpoint if not already present. Existing configs (`CUBE_ENDPOINT=http://localhost:4000`) keep working. |
| 2 | `describe_data` returned a `dict` while declared as `str` | Pydantic validation error on the client side: `(description = null) must be a string`. | Drop the unnecessary `{"type": "text", "text": ...}` wrapper. Return the plain string. |
| 3 | `read_data` returned `list[TextContent \| EmbeddedResource]` while declared as `str` | Same Pydantic class of error in some clients; intermittent dropped responses in others. | Return the YAML serialization directly. The `data://{data_id}` resource is still registered for clients that want raw JSON. |

## Resources

### `context://data_description`
Description of the data available in the Cube deployment. Application-controlled equivalent of the `describe_data` tool.

### `data://{data_id}`
Raw JSON of the data returned by a `read_data` call. The `data_id` is included in every `read_data` response.

## Tools

### `read_data`
Accepts a query for the Cube REST API and returns the data as YAML along with a `data_id` that can be used to fetch raw JSON via the `data://{data_id}` resource.

### `describe_data`
Returns a YAML description of every cube, dimension and measure available in the deployment.

## Install

```bash
pipx install git+https://github.com/EpaUllis/mcp-cube-server.git
```

(Replace `EpaUllis` if you're forking further.)

## Configure in Claude Desktop

```json
{
  "mcpServers": {
    "cube": {
      "command": "/Users/<you>/.local/bin/mcp_cube_server",
      "env": {
        "CUBE_ENDPOINT": "http://localhost:4000",
        "CUBE_API_SECRET": "your-cube-api-secret"
      }
    }
  }
}
```

`CUBE_ENDPOINT` can be either the host (`http://localhost:4000`) or include the API prefix (`http://localhost:4000/cubejs-api/v1`). Both work.

## License

GPL-3.0 (inherited from upstream).
