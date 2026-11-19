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

### Token instead of secret (0.1.1)

Set `CUBE_TOKEN` to a JWT somebody else minted and leave `CUBE_API_SECRET` out. The server then
sends that token verbatim and never holds the signing key, which is the right shape for an agent
operator: a credential with fixed claims, not the key that mints credentials. `CUBE_API_SECRET`
(the server mints its own JWT from `CUBE_TOKEN_PAYLOAD`) stays for local development. One of the
two is required; the server exits with a usage error if neither is set.

Tests: `PYTHONPATH=src python -m unittest tests/test_token_mode.py`.

### Longer wait for slow queries

Cube answers `Continue wait` while the warehouse runs a query. Upstream gave up after 10
seconds and returned "Request timed out"; the server now waits up to 90 seconds, set with
`CUBE_MAX_WAIT_SECONDS`. Past the limit (0.1.5) the tool says the query is still running and
tells the model to ask the user whether to keep waiting, and if so to send the identical query
again.

Tests: `PYTHONPATH=src python -m unittest tests/test_wait.py`.

### Filters (0.1.5)

Upstream had the `filters` field commented out (since 2025-01-30), so a model could not ask
Cube for a subset: it fetched every row and searched them itself. `read_data` now takes
Cube REST filters, `{member, operator, values}`, applied in the warehouse; several filters
combine with AND. An unknown operator is rejected before the query reaches Cube.

### Asking the person whether to keep waiting (0.1.5)

When a query is still running at the wait limit and the MCP client supports elicitation, the
server asks the person, not the model, "keep waiting?", up to `CUBE_MAX_EXTENSIONS` times
(default 3). A client without elicitation gets the still-running text above instead.

Tests: `PYTHONPATH=src python -m unittest tests/test_filters_and_waiting.py`.

### Starting when Cube is unreachable (0.1.2, read limit 0.1.3)

Upstream fetched Cube's metadata at startup, with no timeout, and never used the answer. If
Cube could not be reached, the server hung before it started and the MCP client dropped it.
The server now starts without calling Cube, and every call to Cube has a connect timeout
(`CUBE_REQUEST_TIMEOUT_SECONDS`, default 10) and a read timeout (`CUBE_READ_TIMEOUT_SECONDS`,
default `CUBE_MAX_WAIT_SECONDS`, so 90), so an unreachable Cube becomes an error on the tool call instead of a hang. The
retry after a 403 now sends the refreshed token and returns the parsed answer.

Tests: `PYTHONPATH=src python -m unittest tests/test_startup.py`.

## Remote mode (OAuth) (0.2.0, 0.2.1)

Opt-in. Without `MCP_TRANSPORT` the server runs over stdio exactly as above.

With `MCP_TRANSPORT=streamable-http` the server listens over HTTP and is an OAuth resource
server for an OIDC issuer, usable as a remote MCP server (for example a custom connector in
Claude). The client signs each person in at the issuer and sends their access token. The
server:

- answers a request without a valid token with `401` and
  `WWW-Authenticate: Bearer ... resource_metadata="<metadata URL>"`;
- serves RFC 9728 protected resource metadata at
  `/.well-known/oauth-protected-resource<path of MCP_PUBLIC_URL>`, with `resource` equal to
  `MCP_PUBLIC_URL` and `authorization_servers` equal to `[OIDC_ISSUER]`;
- verifies a JWT access token against the issuer's JWKS (from OIDC discovery): signature,
  `iss`, `exp`, and `aud` if `OIDC_AUDIENCE` is set. A token that is not a JWT is checked at
  the issuer's userinfo endpoint, and the answer is cached briefly;
- for each call, mints a Cube JWT signed with `CUBE_API_SECRET`:
  `{"sub": <email>, "groups": [...], "agent": true}`, valid for 5 minutes, so Cube's
  security context names the person;
- returns query rows only: `data://` resources are not registered, since every signed-in
  person could list them.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `streamable-http` for remote mode |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | Listen address (the Dockerfile sets `0.0.0.0`) |
| `MCP_AUTH` | `oauth` | `none` turns authentication off; local testing only |
| `MCP_PUBLIC_URL` | required | The MCP URL as users enter it, e.g. `https://mcp.example.com/mcp` |
| `MCP_HTTP_PATH` | path of `MCP_PUBLIC_URL` | Path the server serves MCP on, if a proxy rewrites it |
| `MCP_HEALTH_PATH` | `/health` | Unauthenticated health check, answers `200 ok` |
| `MCP_KEEP_ALIVE_SECONDS` | `75` | How long an idle HTTP connection stays open; keep it longer than the idle timeout of a load balancer in front |
| `MCP_STATELESS_HTTP` | `false` | `true` for a new MCP session per request (several replicas) |
| `OIDC_ISSUER` | required | Issuer URL, exactly as in the issuer's discovery document |
| `OIDC_AUDIENCE` | unset | Required `aud` of JWT access tokens; unset means not checked |
| `OIDC_JWT_ALGORITHMS` | `RS256` | Comma-separated JWT algorithms accepted |
| `OIDC_EMAIL_CLAIM` | `email` | Claim holding the person's email (becomes Cube `sub`) |
| `OIDC_GROUPS_CLAIM` | `groups` | Claim holding the person's groups |
| `OIDC_SCOPES` | unset | Space-separated scopes advertised as `scopes_supported` |
| `OIDC_REQUIRED_SCOPES` | unset | Space-separated scopes a token must carry |
| `OIDC_USERINFO_CACHE_SECONDS` | `60` | How long a userinfo answer is reused |
| `OIDC_LEEWAY_SECONDS` | `0` | Clock skew allowed on `exp` |
| `CUBE_API_SECRET` | required | Signs the per-person Cube JWT. `CUBE_TOKEN` is refused in this mode |
| `CUBE_USER_TOKEN_TTL_SECONDS` | `300` | Lifetime of the per-person Cube JWT |

`MCP_PUBLIC_URL` and `OIDC_ISSUER` must be written the way they are published (a bare host
needs its trailing slash, the host in lower case, no default port); the server refuses to
start otherwise, because the metadata would not match the URL.

Without `OIDC_AUDIENCE`, and always for tokens checked at userinfo, any access token the issuer
accepts is accepted here, whichever application it was issued to.

Tests: `PYTHONPATH=src python -m unittest tests/test_remote_oauth.py` (a fake issuer runs
in-process; no network).

## License

GPL-3.0 (inherited from upstream).
