import os
import logging
import contextlib
from collections.abc import AsyncIterator
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
import asyncio

import click
import aiohttp
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.responses import Response, RedirectResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send
from starlette.requests import Request

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
LINKEDIN_ACCESS_TOKEN = os.getenv("LINKEDIN_ACCESS_TOKEN")  # Direct access token
LINKEDIN_MCP_SERVER_PORT = int(os.getenv("LINKEDIN_MCP_SERVER_PORT", "5000"))

# LinkedIn API Base URL - Using v2 API
LINKEDIN_API_BASE = "https://api.linkedin.com/v2"

# --- Helper functions for LinkedIn API interaction ---

async def _make_linkedin_request(
    method: str, 
    endpoint: str, 
    json_data: Optional[Dict] = None, 
    params: Optional[Dict] = None,
    expect_empty_response: bool = False
) -> Any:
    """
    Makes an HTTP request to the LinkedIn API using the configured access token.
    """
    if not LINKEDIN_ACCESS_TOKEN:
        raise RuntimeError("LinkedIn access token not configured. Please set LINKEDIN_ACCESS_TOKEN in your .env file.")

    url = f"{LINKEDIN_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "LinkedIn-Version": "202407",  # Current LinkedIn API version
        "X-Restli-Protocol-Version": "2.0.0"
    }
    
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            if method.upper() == "GET":
                async with session.get(url, params=params) as response:
                    response.raise_for_status()
                    if expect_empty_response or response.status == 204:
                        return None
                    # Handle empty responses gracefully
                    response_text = await response.text()
                    if not response_text.strip():
                        return None
                    return await response.json()
                    
            elif method.upper() == "POST":
                async with session.post(url, json=json_data, params=params) as response:
                    response.raise_for_status()
                    if expect_empty_response or response.status == 204:
                        return None
                    response_text = await response.text()
                    if not response_text.strip():
                        return {"status": "success", "id": response.headers.get("x-restli-id")}
                    return await response.json()
                    
            elif method.upper() == "PUT":
                async with session.put(url, json=json_data, params=params) as response:
                    response.raise_for_status()
                    if expect_empty_response or response.status == 204:
                        return None
                    response_text = await response.text()
                    if not response_text.strip():
                        return None
                    return await response.json()
                    
            elif method.upper() == "DELETE":
                async with session.delete(url, params=params) as response:
                    response.raise_for_status()
                    if expect_empty_response or response.status == 204:
                        return None
                    response_text = await response.text()
                    if not response_text.strip():
                        return None
                    return await response.json()
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
                
        except aiohttp.ClientResponseError as e:
            logger.error(f"LinkedIn API request failed: {e.status} {e.message} for {method} {url}")
            error_details = e.message
            try:
                if e.response:
                    error_body = await e.response.text()
                    if error_body:
                        error_details = f"{e.message} - {error_body}"
            except Exception:
                pass
            raise RuntimeError(f"LinkedIn API Error ({e.status}): {error_details}") from e
        except Exception as e:
            logger.error(f"An unexpected error occurred during LinkedIn API request: {e}")
            raise RuntimeError(f"Unexpected error during API call to {method} {url}: {str(e)}") from e

# --- LinkedIn Tool Implementations ---

async def linkedin_get_my_profile() -> Dict[str, Any]:
    """
    Retrieves the authenticated user's basic profile information.
    Uses the correct LinkedIn v2 API endpoint.
    """
    logger.info("Executing tool: linkedin_get_my_profile")
    try:
        # LinkedIn v2 API endpoint for basic profile - simplified approach
        endpoint = "/me"
        profile_data = await _make_linkedin_request("GET", endpoint)
        
        # Get additional profile details
        detailed_endpoint = "/people/~:(id,localizedFirstName,localizedLastName,vanityName)"
        try:
            detailed_data = await _make_linkedin_request("GET", detailed_endpoint)
            profile_data.update(detailed_data)
        except Exception as e:
            logger.warning(f"Could not fetch detailed profile data: {e}")
        
        return {
            "id": profile_data.get("id", ""),
            "firstName": profile_data.get("localizedFirstName", profile_data.get("firstName", "")),
            "lastName": profile_data.get("localizedLastName", profile_data.get("lastName", "")),
            "vanityName": profile_data.get("vanityName", ""),
            "profileUrl": f"https://linkedin.com/in/{profile_data.get('vanityName', '')}" if profile_data.get('vanityName') else ""
        }
    except Exception as e:
        logger.exception(f"Error getting LinkedIn profile: {e}")
        raise e

async def linkedin_share_text_post(text_content: str, visibility: str = "PUBLIC") -> Dict[str, Any]:
    """
    Shares a text-based post on the authenticated user's LinkedIn feed.
    Uses LinkedIn's v2 ugcPosts API with correct payload structure.
    """
    logger.info("Executing tool: linkedin_share_text_post")
    try:
        # First get the user's profile to get the person URN
        profile_data = await _make_linkedin_request("GET", "/me")
        person_urn = f"urn:li:person:{profile_data['id']}"

        endpoint = "/ugcPosts"
        
        # Correct payload structure for LinkedIn v2 ugcPosts API
        payload = {
            "author": person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {
                        "text": text_content
                    },
                    "shareMediaCategory": "NONE"
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": visibility.upper()
            }
        }
        
        share_response = await _make_linkedin_request("POST", endpoint, json_data=payload)
        
        # Handle both successful response formats
        post_id = ""
        if isinstance(share_response, dict):
            post_id = share_response.get("id", share_response.get("status", "unknown"))
        
        return {
            "message": "Post shared successfully!",
            "post_id": post_id,
            "content_preview": text_content[:100] + ("..." if len(text_content) > 100 else ""),
            "visibility": visibility.upper()
        }
    except Exception as e:
        logger.exception(f"Error sharing LinkedIn post: {e}")
        # Provide more specific error handling
        if "insufficient permissions" in str(e).lower():
            raise RuntimeError("Insufficient permissions to post. Please check your LinkedIn app permissions include 'w_member_social' scope.")
        raise e

async def linkedin_get_profile_by_id(person_id: str) -> Dict[str, Any]:
    """
    Retrieves a LinkedIn profile by person ID (if accessible).
    Note: This requires special permissions and may not work for all profiles.
    """
    logger.info(f"Executing tool: linkedin_get_profile_by_id for ID: {person_id}")
    try:
        endpoint = f"/people/id={person_id}:(id,localizedFirstName,localizedLastName,vanityName)"
        profile_data = await _make_linkedin_request("GET", endpoint)
        
        return {
            "id": profile_data.get("id", ""),
            "firstName": profile_data.get("localizedFirstName", ""),
            "lastName": profile_data.get("localizedLastName", ""),
            "vanityName": profile_data.get("vanityName", ""),
            "profileUrl": f"https://linkedin.com/in/{profile_data.get('vanityName', '')}" if profile_data.get('vanityName') else ""
        }
    except Exception as e:
        logger.exception(f"Error getting LinkedIn profile by ID {person_id}: {e}")
        if "forbidden" in str(e).lower() or "not authorized" in str(e).lower():
            raise RuntimeError(f"Access denied to profile {person_id}. You may not have permission to view this profile or it may not exist.")
        raise e

# --- Main MCP Server Application ---

def create_mcp_app() -> Server:
    """Create and configure the MCP server application."""
    app = Server("linkedin-mcp-server")

    # --- MCP Tool Definitions ---
    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        """Lists the available LinkedIn tools."""
        return [
            types.Tool(
                name="linkedin_get_my_profile",
                description="Retrieves the authenticated user's basic LinkedIn profile information including ID, name, and vanity URL.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                },
            ),
            types.Tool(
                name="linkedin_share_text_post",
                description="Shares a text-based post on the authenticated user's LinkedIn feed. Requires 'w_member_social' permission.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text_content": {
                            "type": "string",
                            "description": "The text content of the post to share. Maximum 3000 characters.",
                            "maxLength": 3000
                        },
                        "visibility": {
                            "type": "string",
                            "description": "Post visibility level",
                            "enum": ["PUBLIC", "CONNECTIONS"],
                            "default": "PUBLIC"
                        }
                    },
                    "required": ["text_content"]
                },
            ),
            types.Tool(
                name="linkedin_get_profile_by_id",
                description="Retrieves a LinkedIn profile by person ID. Note: This requires special permissions and may not work for all profiles due to LinkedIn's privacy settings.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "person_id": {
                            "type": "string",
                            "description": "The LinkedIn person ID to retrieve (e.g., 'abc123')"
                        }
                    },
                    "required": ["person_id"]
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(
        name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Handles the execution of LinkedIn tools based on the tool name."""
        try:
            if name == "linkedin_get_my_profile":
                result = await linkedin_get_my_profile()
                return [
                    types.TextContent(
                        type="text",
                        text=f"LinkedIn Profile Information:\n"
                             f"ID: {result['id']}\n"
                             f"Name: {result['firstName']} {result['lastName']}\n"
                             f"Vanity Name: {result['vanityName']}\n"
                             f"Profile URL: {result['profileUrl']}"
                    )
                ]
            
            elif name == "linkedin_share_text_post":
                text_content = arguments.get("text_content")
                visibility = arguments.get("visibility", "PUBLIC")
                
                if not text_content:
                    raise ValueError("text_content argument is required for linkedin_share_text_post.")
                
                if len(text_content) > 3000:
                    raise ValueError("text_content must be 3000 characters or less.")
                
                result = await linkedin_share_text_post(text_content, visibility)
                return [
                    types.TextContent(
                        type="text",
                        text=f"LinkedIn Post Shared Successfully!\n"
                             f"Post ID: {result['post_id']}\n"
                             f"Visibility: {result['visibility']}\n"
                             f"Content Preview: {result['content_preview']}"
                    )
                ]
            
            elif name == "linkedin_get_profile_by_id":
                person_id = arguments.get("person_id")
                if not person_id:
                    raise ValueError("person_id argument is required for linkedin_get_profile_by_id.")
                
                result = await linkedin_get_profile_by_id(person_id)
                return [
                    types.TextContent(
                        type="text",
                        text=f"LinkedIn Profile Information:\n"
                             f"ID: {result['id']}\n"
                             f"Name: {result['firstName']} {result['lastName']}\n"
                             f"Vanity Name: {result['vanityName']}\n"
                             f"Profile URL: {result['profileUrl']}"
                    )
                ]
            
            else:
                return [
                    types.TextContent(
                        type="text",
                        text=f"Unknown tool: {name}. Available tools: linkedin_get_my_profile, linkedin_share_text_post, linkedin_get_profile_by_id"
                    )
                ]
                
        except Exception as e:
            logger.exception(f"Error executing tool {name}: {e}")
            return [
                types.TextContent(
                    type="text",
                    text=f"Error executing tool {name}: {str(e)}"
                )
            ]

    return app

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
    """Main entry point for the LinkedIn MCP server."""
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Validate required configuration
    if not LINKEDIN_ACCESS_TOKEN:
        logger.error("LINKEDIN_ACCESS_TOKEN is required. Please set it in your .env file.")
        logger.error("Follow these steps to get a LinkedIn access token:")
        logger.error("1. Go to https://www.linkedin.com/developers/")
        logger.error("2. Create a new app or use an existing one")
        logger.error("3. Configure the app with required scopes: r_liteprofile, w_member_social")
        logger.error("4. Use LinkedIn's OAuth2 flow to get an access token")
        return 1

    # Create the MCP server instance
    mcp_app = create_mcp_app()

    # --- Server Transport Setup (SSE and StreamableHTTP) ---
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        """Handles SSE connections."""
        logger.info("Handling SSE connection")
        async with sse.connect_sse(
            request.scope, request.receive, request.send  # Fixed: use request.send instead of request._send
        ) as streams:
            await mcp_app.run(
                streams[0], streams[1], mcp_app.create_initialization_options()
            )
        return Response()

    session_manager = StreamableHTTPSessionManager(
        app=mcp_app,
        event_store=None,  # Stateless mode
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
            logger.info("LinkedIn MCP Server started successfully!")
            logger.info(f"Server is running on port {port}")
            logger.info(f"Available endpoints:")
            logger.info(f"  - SSE endpoint: http://localhost:{port}/sse")
            logger.info(f"  - StreamableHTTP endpoint: http://localhost:{port}/mcp")
            logger.info(f"Available tools: linkedin_get_my_profile, linkedin_share_text_post, linkedin_get_profile_by_id")
            try:
                yield
            finally:
                logger.info("Application shutting down...")

    # Create ASGI application with MCP transport routes
    starlette_app = Starlette(
        debug=True,
        routes=[
            # SSE routes
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
            
            # StreamableHTTP route
            Mount("/mcp", app=handle_streamable_http),
            
            # Health check endpoint
            Route("/health", endpoint=lambda request: JSONResponse({"status": "healthy"}), methods=["GET"]),
        ],
        lifespan=lifespan,
    )

    logger.info(f"Starting LinkedIn MCP Server on port {port}")

    try:
        import uvicorn
        uvicorn.run(starlette_app, host="0.0.0.0", port=port)
        return 0
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        return 0
    except Exception as e:
        logger.error(f"Server failed to start: {e}")
        return 1

if __name__ == "__main__":
    exit(main())
