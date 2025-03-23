import os
import sys
import logging
import json
import requests
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.sse import SseServerTransport
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Mount
import uvicorn

# reconfigure UnicodeEncodeError prone default (i.e. windows-1252) to utf-8
if sys.platform == "win32" and os.environ.get('PYTHONIOENCODING') is None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger('mcp_pox_server')
logger.info("Starting MCP POX OpenFlow Server with SSE Transport")

PROMPT_TEMPLATE = """
You are assisting a network administrator in managing and configuring an OpenFlow-based network using POX.
The topic provided is: {topic}

<network-info>
The network currently has several OpenFlow switches connected to a POX controller.
Each switch has a unique DPID (Datapath ID) that identifies it.
You can view the switches, their connections, and configure flow tables through this interface.
</network-info>

Your goal is to help the user analyze their network, configure it effectively, and solve any potential issues.

<objectives>
1. Explore the network topology by listing connected switches
2. Examine details about specific switches
3. Configure flow tables to implement the requested network behavior
4. Analyze flow statistics to understand network traffic patterns
5. Document and explain network configurations for reference
</objectives>

Use the provided tools to interact with the POX controller and help manage this OpenFlow network.
"""

class POXOpenFlowManager:
    def __init__(self, pox_server_url: str):
        self.pox_server_url = pox_server_url
        self.network_memo: list[str] = []
        self.configs: list[dict] = []
        self.insights: list[str] = []

    def _execute_of_command(self, method: str, params: dict = None) -> Dict[str, Any]:
        """Execute an OpenFlow command via the POX webservice"""
        logger.debug(f"Executing OpenFlow command: {method} with params: {params}")
        try:
            payload = {
                "method": method,
                "params": params or {},
                "id": 1  # JSON-RPC requires an ID
            }
            
            response = requests.post(f"{self.pox_server_url}/OF/", json=payload)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Error executing command: {response.status_code} - {response.text}")
                return {"error": f"HTTP Error: {response.status_code}", "details": response.text}
        except Exception as e:
            logger.error(f"Exception executing command: {str(e)}")
            return {"error": "Connection Error", "details": str(e)}

    def get_switches(self) -> List[Dict[str, Any]]:
        """Get list of all connected switches"""
        result = self._execute_of_command("get_switches")
        return result.get("result", [])

    def get_switch_desc(self, dpid: str) -> Dict[str, Any]:
        """Get description of a specific switch"""
        result = self._execute_of_command("get_switch_desc", {"dpid": dpid})
        return result.get("result", {})

    def get_flow_stats(self, dpid: str, match=None, table_id=None, out_port=None) -> List[Dict[str, Any]]:
        """Get flow statistics for a switch"""
        params = {"dpid": dpid}
        if match:
            params["match"] = match
        if table_id is not None:
            params["table_id"] = table_id
        if out_port is not None:
            params["out_port"] = out_port
            
        result = self._execute_of_command("get_flow_stats", params)
        return result.get("result", [])

    def set_table(self, dpid: str, flows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Set flow table for a switch"""
        params = {"dpid": dpid, "flows": flows}
        result = self._execute_of_command("set_table", params)
        
        # Save successful configuration to history
        if "error" not in result:
            self.configs.append({
                "dpid": dpid,
                "flows": flows,
                "timestamp": "__current_time__"  # Replace with actual timestamp logic
            })
            
        return result.get("result", {})

    def _synthesize_network_memo(self) -> str:
        """Create a network memo from the current configurations and insights"""
        memo = "📊 Network Configuration Memo 📊\n\n"
        
        # Add network topology section
        memo += "Network Topology:\n"
        switches = self.get_switches()
        for switch in switches:
            memo += f"- Switch DPID: {switch.get('dpid', 'unknown')}\n"
        
        # Add configurations section
        if self.configs:
            memo += "\nRecent Flow Configurations:\n"
            for idx, config in enumerate(self.configs[-5:]):  # Show last 5 configs
                memo += f"Configuration {idx+1} - Switch {config['dpid']}:\n"
                for i, flow in enumerate(config['flows']):
                    memo += f"  Flow {i+1}: {json.dumps(flow)[:100]}...\n"
        
        # Add insights section
        if self.insights:
            memo += "\nNetwork Insights:\n"
            for insight in self.insights:
                memo += f"- {insight}\n"
                
        return memo


def create_app(pox_server_url: str):
    logger.info(f"Creating POX OpenFlow MCP Server connecting to: {pox_server_url}")

    pox_manager = POXOpenFlowManager(pox_server_url)
    server = Server("pox-openflow-manager")

    # Register handlers
    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        logger.debug("Handling list_resources request")
        return [
            types.Resource(
                uri=AnyUrl("pox://network-config"),
                name="Network Configuration Memo",
                description="A document containing network topology, configurations, and insights",
                mimeType="text/plain",
            ),
            types.Resource(
                uri=AnyUrl("pox://topology"),
                name="Network Topology",
                description="Current network topology with connected switches",
                mimeType="text/plain",
            )
        ]

    @server.read_resource()
    async def handle_read_resource(uri: AnyUrl) -> str:
        logger.debug(f"Handling read_resource request for URI: {uri}")
        if uri.scheme != "pox":
            logger.error(f"Unsupported URI scheme: {uri.scheme}")
            raise ValueError(f"Unsupported URI scheme: {uri.scheme}")

        path = str(uri).replace("pox://", "")
        
        if path == "network-config":
            return pox_manager._synthesize_network_memo()
        elif path == "topology":
            switches = pox_manager.get_switches()
            return f"Network Topology:\n{json.dumps(switches, indent=2)}"
        else:
            logger.error(f"Unknown resource path: {path}")
            raise ValueError(f"Unknown resource path: {path}")

    @server.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        logger.debug("Handling list_prompts request")
        return [
            types.Prompt(
                name="pox-network-manager",
                description="A prompt to help you manage OpenFlow networks using POX",
                arguments=[
                    types.PromptArgument(
                        name="topic",
                        description="Network management topic or goal to focus on",
                        required=True,
                    )
                ],
            ),
            types.Prompt(
                name="simple-hub",
                description="Configure a switch to act as a simple hub that forwards all packets to all ports",
                arguments=[
                    types.PromptArgument(
                        name="dpid",
                        description="The DPID of the switch to configure",
                        required=True,
                    )
                ],
            ),
            types.Prompt(
                name="learning-switch",
                description="Configure a switch to act as a learning L2 switch",
                arguments=[
                    types.PromptArgument(
                        name="dpid",
                        description="The DPID of the switch to configure",
                        required=True,
                    )
                ],
            )
        ]

    @server.get_prompt()
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        logger.debug(f"Handling get_prompt request for {name} with args {arguments}")
        
        if name == "pox-network-manager":
            if not arguments or "topic" not in arguments:
                logger.error("Missing required argument: topic")
                raise ValueError("Missing required argument: topic")

            topic = arguments["topic"]
            prompt = PROMPT_TEMPLATE.format(topic=topic)
            
            return types.GetPromptResult(
                description=f"OpenFlow network management for topic: {topic}",
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(type="text", text=prompt.strip()),
                    )
                ],
            )
            
        elif name == "simple-hub":
            if not arguments or "dpid" not in arguments:
                logger.error("Missing required argument: dpid")
                raise ValueError("Missing required argument: dpid")
                
            dpid = arguments["dpid"]
            hub_prompt = f"""
            Please help me configure the switch with DPID {dpid} to act as a simple hub.
            
            A hub should forward all incoming packets to all other ports.
            Use the set_table tool to implement this configuration.
            
            Afterwards, please explain the configuration and how it works.
            """
            
            return types.GetPromptResult(
                description=f"Configure switch {dpid} as a simple hub",
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(type="text", text=hub_prompt.strip()),
                    )
                ],
            )
            
        elif name == "learning-switch":
            if not arguments or "dpid" not in arguments:
                logger.error("Missing required argument: dpid")
                raise ValueError("Missing required argument: dpid")
                
            dpid = arguments["dpid"]
            switch_prompt = f"""
            Please help me configure the switch with DPID {dpid} to act as a learning L2 switch.
            
            A learning switch should:
            1. Forward packets based on learned MAC addresses
            2. Flood packets with unknown destinations
            3. Learn MAC addresses from source addresses of incoming packets
            
            Please outline how this can be implemented in POX, even though the full implementation might require some programmatic components.
            """
            
            return types.GetPromptResult(
                description=f"Configure switch {dpid} as a learning switch",
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(type="text", text=switch_prompt.strip()),
                    )
                ],
            )
            
        else:
            logger.error(f"Unknown prompt: {name}")
            raise ValueError(f"Unknown prompt: {name}")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """List available tools"""
        return [
            types.Tool(
                name="get_switches",
                description="Get a list of all connected OpenFlow switches",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            types.Tool(
                name="get_switch_desc",
                description="Get detailed information about a specific switch",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dpid": {"type": "string", "description": "Datapath ID of the switch to describe"},
                    },
                    "required": ["dpid"],
                },
            ),
            types.Tool(
                name="get_flow_stats",
                description="Get flow statistics from a switch",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dpid": {"type": "string", "description": "Datapath ID of the switch"},
                        "match": {"type": "object", "description": "Match structure to filter flows (optional)"},
                        "table_id": {"type": "string", "description": "Table ID to filter flows (optional)"},
                        "out_port": {"type": "string", "description": "Filter by out port (optional)"},
                    },
                    "required": ["dpid"],
                },
            ),
            types.Tool(
                name="set_table",
                description="Set the flow table on a switch",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dpid": {"type": "string", "description": "Datapath ID of the switch"},
                        "flows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "match": {"type": "object", "description": "Flow match criteria"},
                                    "actions": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "type": {"type": "string", "description": "Action type (e.g., OFPAT_OUTPUT)"},
                                                "port": {"type": "string", "description": "Output port (e.g., port number or OFPP_ALL)"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "required": ["dpid", "flows"],
                },
            ),
            types.Tool(
                name="append_insight",
                description="Add a network insight to the configuration memo",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "insight": {"type": "string", "description": "Network insight discovered from analysis"},
                    },
                    "required": ["insight"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Handle tool execution requests"""
        try:
            if name == "get_switches":
                results = pox_manager.get_switches()
                return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

            elif name == "get_switch_desc":
                if not arguments or "dpid" not in arguments:
                    raise ValueError("Missing dpid argument")
                results = pox_manager.get_switch_desc(arguments["dpid"])
                return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

            elif name == "get_flow_stats":
                if not arguments or "dpid" not in arguments:
                    raise ValueError("Missing dpid argument")
                    
                results = pox_manager.get_flow_stats(
                    arguments["dpid"],
                    arguments.get("match"),
                    arguments.get("table_id"),
                    arguments.get("out_port")
                )
                return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

            elif name == "set_table":
                if not arguments or "dpid" not in arguments or "flows" not in arguments:
                    raise ValueError("Missing dpid or flows arguments")
                    
                results = pox_manager.set_table(arguments["dpid"], arguments["flows"])
                
                # Notify clients that the network config resource has changed
                await server.request_context.session.send_resource_updated(AnyUrl("pox://network-config"))
                
                return [types.TextContent(type="text", text=json.dumps(results, indent=2))]

            elif name == "append_insight":
                if not arguments or "insight" not in arguments:
                    raise ValueError("Missing insight argument")

                pox_manager.insights.append(arguments["insight"])
                
                # Notify clients that the network config resource has changed
                await server.request_context.session.send_resource_updated(AnyUrl("pox://network-config"))

                return [types.TextContent(type="text", text="Network insight added to memo")]

            else:
                raise ValueError(f"Unknown tool: {name}")

        except requests.RequestException as e:
            return [types.TextContent(type="text", text=f"POX communication error: {str(e)}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {str(e)}")]

    # Create SSE transport
    sse = SseServerTransport("/sse")
    
    # Define handlers
    async def handle_sse(scope, receive, send):
        logger.debug("Handling SSE connection")
        async with sse.connect_sse(scope, receive, send) as streams:
            await server.run(
                streams[0], 
                streams[1], 
                InitializationOptions(
                    server_name="pox-openflow",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                )
            )
    
    async def handle_messages(scope, receive, send):
        logger.debug("Handling POST message")
        await sse.handle_post_message(scope, receive, send)
    
    # Create Starlette app
    app = Starlette(
        routes=[
            Mount("/sse", app=handle_sse),
            Mount("/messages", app=handle_messages),
        ]
    )
    
    return app

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="POX OpenFlow MCP Server with SSE Transport")
    parser.add_argument("--pox-url", default="http://127.0.0.1:8000", help="URL of the POX controller's web service")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the SSE server to")
    parser.add_argument("--port", type=int, default=3000, help="Port to bind the SSE server to")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()
    
    logging_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=logging_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Create app and run with uvicorn
    app = create_app(args.pox_url)
    uvicorn.run(app, host=args.host, port=args.port)
    