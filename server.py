import os
import logging
import contextlib
from collections.abc import AsyncIterator
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import click
import aiohttp
from dotenv import load_dotenv
from requests_oauthlib import OAuth2Session
from starlette.applications import Starlette
from starlette.responses import Response, RedirectResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("linkedin-mcp-server")

# LinkedIn API constants and configuration
LINKEDIN_CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID")
LINKEDIN_CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET")
LINKEDIN_REDIRECT_URI = os.getenv("LINKEDIN_REDIRECT_URI")
LINKEDIN_SCOPES = os.getenv("LINKEDIN_SCOPES", "r_liteprofile,r_emailaddress,w_member_social").split(',')
LINKEDIN_MCP_SERVER_PORT = int(os.getenv("LINKEDIN_MCP_SERVER_PORT", "5000"))

# OAuth 2.0 Endpoints
AUTHORIZATION_BASE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_API_BASE = "https://api.linkedin.com/v2"

# In a real application, you would store and retrieve tokens securely (e.g., database, encrypted session)
# For this example, we'll use a simple in-memory store for demonstration purposes.
# This will not persist across server restarts or multiple concurrent users.
# For multi-user scenarios, you'd need a per-user token storage.
user_access_tokens: Dict[str, str] = {} # A simple dict to store access tokens (user_id -> access_token)

# --- Helper functions for LinkedIn API interaction ---

def get_linkedin_oauth_session(state: Optional[str] = None) -> OAuth2Session:
    """
    Creates an OAuth2Session for LinkedIn.
    """
    if not all([LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET, LINKEDIN_REDIRECT_URI]):
        raise ValueError("LinkedIn API credentials (CLIENT_ID, CLIENT_SECRET, REDIRECT_URI) are required.")
    
    return OAuth2Session(
        client_id=LINKEDIN_CLIENT_ID,
        redirect_uri=LINKEDIN_REDIRECT_URI,
        scope=LINKEDIN_SCOPES,
        state=state
    )

async def _make_linkedin_request(
    method: str, 
    endpoint: str, 
    user_id: str, # Assuming a user_id to retrieve their token
    json_data: Optional[Dict] = None, 
    params: Optional[Dict] = None,
    expect_empty_response: bool = False
) -> Any:
    """
    Makes an HTTP request to the LinkedIn API using the stored access token.
    """
    access_token = user_access_tokens.get(user_id)
    if not access_token:
        raise RuntimeError("User not authenticated with LinkedIn. Please authorize first.")

    url = f"{LINKEDIN_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0" # Required for LinkedIn API v2
    }
    
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            if method.upper() == "GET":
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    if expect_empty_response:
                        return None if response.status == 204 else await response.json()
                    return await response.json()
            elif method.upper() == "POST":
                async with session.post(url, json=json_data, params=params) as response:
                    response.raise_for_status()
                    if expect_empty_response:
                        return None if response.status == 204 else await response.json()
                    return await response.json()
            # Add other methods (PUT, DELETE) as needed
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
        except aiohttp.ClientResponseError as e:
            logger.error(f"LinkedIn API request failed: {e.status} {e.message} for {method} {url}")
            error_details = e.message
            try:
                error_body = await e.response.json()
                error_details = f"{e.message} - {error_body}"
            except Exception:
                pass
            raise RuntimeError(f"LinkedIn API Error ({e.status}): {error_details}") from e
        except Exception as e:
            logger.error(f"An unexpected error occurred during LinkedIn API request: {e}")
            raise RuntimeError(f"Unexpected error during API call to {method} {url}") from e

# --- Placeholder Tool Implementations (move to tools/ directory later) ---
# For now, these are direct implementations. In a real scenario, you would
# import these from `tools/profiles.py`, `tools/shares.py`, etc.

async def linkedin_get_my_profile(user_id: str) -> Dict[str, Any]:
    """
    Retrieves the authenticated user's basic profile information.
    Requires 'r_liteprofile' scope.
    """
    logger.info(f"Executing tool: linkedin_get_my_profile for user_id: {user_id}")
    try:
        # LinkedIn API endpoint for basic profile
        endpoint = "/me"
        profile_data = await _make_linkedin_request("GET", endpoint, user_id)
        return {
            "id": profile_data.get("id"),
            "localizedFirstName": profile_data.get("localizedFirstName"),
            "localizedLastName": profile_data.get("localizedLastName"),
            "profilePicture": profile_data.get("profilePicture", {}).get("displayImage", ""),
            "vanityName": profile_data.get("vanityName", "") # Public profile URL part
        }
    except Exception as e:
        logger.exception(f"Error getting LinkedIn profile for user {user_id}: {e}")
        raise e

async def linkedin_share_text_post(user_id: str, text_content: str) -> Dict[str, Any]:
    """
    Shares a text-based post on the authenticated user's LinkedIn feed.
    Requires 'w_member_social' scope.
    """
    logger.info(f"Executing tool: linkedin_share_text_post for user_id: {user_id}")
    try:
        # Get the URN of the authenticated user to create the author URN
        profile_data = await _make_linkedin_request("GET", "/me", user_id)
        author_urn = f"urn:li:person:{profile_data['id']}"

        endpoint = "/shares"
        payload = {
            "owner": author_urn,
            "text": {
                "text": text_content
            },
            "distribution": {
                "linkedInTargetAudience": "PUBLIC"
            }
        }
        share_response = await _make_linkedin_request("POST", endpoint, user_id, json_data=payload)
        return {
            "message": "Post shared successfully!",
            "share_urn": share_response.get("id"),
            "content_preview": text_content[:100] + ("..." if len(text_content) > 100 else "")
        }
    except Exception as e:
        logger.exception(f"Error sharing LinkedIn post for user {user_id}: {e}")
        raise e

# --- Main MCP Server Application ---

@click.command()
@click.option("--port", default=LINKEDIN_MCP_SERVER_PORT, help="Port to listen on for HTTP")
@click.option(
    "--log-level",
    default="INFO",
    help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
)
@click.option(
    "--json-response",
    is_flag=True,
    default=False,
    help="Enable JSON responses for StreamableHTTP instead of SSE streams",
)
def main(
    port: int,
    log_level: str,
    json_response: bool,
) -> int:
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Create the MCP server instance
    app = Server("linkedin-mcp-server")

    # --- OAuth 2.0 Routes ---
    @app.route("/auth/linkedin", methods=["GET"])
    async def linkedin_auth_init(request):
        """
        Initiates the LinkedIn OAuth 2.0 authorization flow.
        Redirects the user to LinkedIn's authorization page.
        """
        # Generate a unique state parameter to prevent CSRF.
        # In a real app, this should be stored securely (e.g., in a session)
        # and validated upon callback. For simplicity, we'll use a fixed one.
        # For multi-user, you'd need a per-user state.
        state = "random_string_for_csrf_protection" 
        
        linkedin_oauth = get_linkedin_oauth_session(state=state)
        authorization_url, state = linkedin_oauth.authorization_url(AUTHORIZATION_BASE_URL)
        logger.info(f"Redirecting to LinkedIn for authorization: {authorization_url}")
        return RedirectResponse(authorization_url)

    @app.route("/auth/linkedin/callback", methods=["GET"])
    async def linkedin_auth_callback(request):
        """
        Handles the callback from LinkedIn after user authorization.
        Exchanges the authorization code for an access token.
        """
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")
        error_description = request.query_params.get("error_description")

        if error:
            logger.error(f"LinkedIn OAuth error: {error} - {error_description}")
            return JSONResponse({"error": f"LinkedIn authorization failed: {error_description}"}, status_code=400)

        if not code:
            logger.error("LinkedIn OAuth callback: No authorization code received.")
            return JSONResponse({"error": "Authorization code missing."}, status_code=400)
        
        # In a real app, validate the 'state' parameter here against the one stored earlier.
        # if state != "your_stored_state":
        #    return JSONResponse({"error": "CSRF attack detected or invalid state."}, status_code=400)

        linkedin_oauth = get_linkedin_oauth_session(state=state)
        try:
            # Fetch the token using the authorization code
            token = linkedin_oauth.fetch_token(
                TOKEN_URL,
                client_secret=LINKEDIN_CLIENT_SECRET,
                code=code,
                include_client_id=True # Important for LinkedIn
            )
            access_token = token.get("access_token")
            expires_in = token.get("expires_in")

            if not access_token:
                raise ValueError("Access token not received from LinkedIn.")

            # For demonstration, store the token with a dummy user_id.
            # In a real application, you'd associate this token with the actual user
            # who initiated the OAuth flow. This might involve looking up a session ID
            # or a temporary user identifier.
            dummy_user_id = "default_linkedin_user" # Replace with actual user ID mechanism
            user_access_tokens[dummy_user_id] = access_token
            logger.info(f"Successfully obtained LinkedIn access token for user: {dummy_user_id}")
            
            return JSONResponse({
                "message": "LinkedIn authorization successful!",
                "access_token_expires_in": expires_in,
                "user_id": dummy_user_id,
                "note": "For multi-user scenarios, you need to manage access tokens per user securely."
            })

        except Exception as e:
            logger.exception(f"Error during LinkedIn token exchange: {e}")
            return JSONResponse({"error": f"Failed to exchange token: {str(e)}"}, status_code=500)

    # --- MCP Tool Definitions ---
    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        """
        Lists the available LinkedIn tools.
        """
        return [
            types.Tool(
                name="linkedin_get_my_profile",
                description="Retrieves the authenticated user's basic LinkedIn profile information. Requires prior LinkedIn authorization.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "The internal user ID associated with the LinkedIn access token. (e.g., 'default_linkedin_user')",
                            "default": "default_linkedin_user"
                        }
                    },
                    "required": ["user_id"]
                },
            ),
            types.Tool(
                name="linkedin_share_text_post",
                description="Shares a text-based post on the authenticated user's LinkedIn feed. Requires prior LinkedIn authorization and 'w_member_social' scope.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "The internal user ID associated with the LinkedIn access token. (e.g., 'default_linkedin_user')",
                            "default": "default_linkedin_user"
                        },
                        "text_content": {
                            "type": "string",
                            "description": "The text content of the post to share."
                        }
                    },
                    "required": ["user_id", "text_content"]
                },
            ),
            # Add more LinkedIn tools here as you implement them in the tools/ directory
            # e.g., linkedin_get_connections, linkedin_get_company_updates, etc.
        ]

    @app.call_tool()
    async def call_tool(
        name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """
        Handles the execution of LinkedIn tools based on the tool name.
        """
        try:
            # Extract user_id from arguments, defaulting if not provided
            user_id = arguments.get("user_id", "default_linkedin_user")

            if name == "linkedin_get_my_profile":
                result = await linkedin_get_my_profile(user_id)
                return [
                    types.TextContent(
                        type="text",
                        text=f"LinkedIn Profile: {result}",
                    )
                ]
            elif name == "linkedin_share_text_post":
                text_content = arguments.get("text_content")
                if not text_content:
                    raise ValueError("text_content argument is required for linkedin_share_text_post.")
                result = await linkedin_share_text_post(user_id, text_content)
                return [
                    types.TextContent(
                        type="text",
                        text=f"LinkedIn Share Result: {result}",
                    )
                ]
            # Add more tool handlers here
            # elif name == "linkedin_get_connections":
            #     result = await linkedin_get_connections(user_id)
            #     return [types.TextContent(type="text", text=str(result))]
            else:
                return [
                    types.TextContent(
                        type="text",
                        text=f"Unknown tool: {name}",
                    )
                ]
        except Exception as e:
            logger.exception(f"Error executing tool {name}: {e}")
            return [
                types.TextContent(
                    type="text",
                    text=f"Error executing tool {name}: {str(e)}",
                )
            ]

    # --- Server Transport Setup (SSE and StreamableHTTP) ---
    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        """Handles SSE connections."""
        logger.info("Handling SSE connection")
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(
                streams[0], streams[1], app.create_initialization_options()
            )
        return Response()

    session_manager = StreamableHTTPSessionManager(
        app=app,
        event_store=None,  # Stateless mode - can be changed to use an event store
        json_response=json_response,
        stateless=True,
    )

    async def handle_streamable_http(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Handles StreamableHTTP requests."""
        logger.info("Handling StreamableHTTP request")
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Context manager for session manager."""
        async with session_manager.run():
            logger.info("Application started with dual transports!")
            try:
                yield
            finally:
                logger.info("Application shutting down...")

    # Create an ASGI application with routes for both transports and OAuth
    starlette_app = Starlette(
        debug=True,
        routes=[
            # OAuth routes
            Route("/auth/linkedin", endpoint=linkedin_auth_init, methods=["GET"]),
            Route("/auth/linkedin/callback", endpoint=linkedin_auth_callback, methods=["GET"]),
            
            # SSE routes
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
            
            # StreamableHTTP route
            Mount("/mcp", app=handle_streamable_http),
        ],
        lifespan=lifespan,
    )

    logger.info(f"Server starting on port {port} with dual transports and OAuth:")
    logger.info(f"  - LinkedIn OAuth Init: http://localhost:{port}/auth/linkedin")
    logger.info(f"  - SSE endpoint: http://localhost:{port}/sse")
    logger.info(f"  - StreamableHTTP endpoint: http://localhost:{port}/mcp")

    import uvicorn
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)

    return 0

if __name__ == "__main__":
    main()
