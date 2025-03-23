import os
import sys
import logging
import json
import requests
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts import base
from pydantic import AnyUrl

# Reconfigure UnicodeEncodeError-prone default (e.g., windows-1252) to UTF-8
if sys.platform == "win32" and os.environ.get('PYTHONIOENCODING') is None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger('mcp_pox_server')
logger.setLevel(logging.DEBUG)  # Ensure debug-level logging
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)
logger.info("Starting MCP POX OpenFlow Server with FastMCP")

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
        logger.debug(f"Executing OpenFlow command: {method} with params: {params}")
        try:
            payload = {"method": method, "params": params or {}, "id": 1}
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
        return self._execute_of_command("get_switches").get("result", [])

    def get_switch_desc(self, dpid: str) -> Dict[str, Any]:
        return self._execute_of_command("get_switch_desc", {"dpid": dpid}).get("result", {})

    def get_flow_stats(self, dpid: str, match: dict = None, table_id: str = None, out_port: str = None) -> List[Dict[str, Any]]:
        params = {"dpid": dpid}
        if match: params["match"] = match
        if table_id is not None: params["table_id"] = table_id
        if out_port is not None: params["out_port"] = out_port
        return self._execute_of_command("get_flow_stats", params).get("result", [])

    def set_table(self, dpid: str, flows: List[Dict[str, Any]]) -> Dict[str, Any]:
        params = {"dpid": dpid, "flows": flows}
        result = self._execute_of_command("set_table", params)
        if "error" not in result:
            self.configs.append({"dpid": dpid, "flows": flows, "timestamp": "__current_time__"})
        return result.get("result", {})

    def append_insight(self, insight: str):
        self.insights.append(insight)

    def _synthesize_network_memo(self) -> str:
        memo = "📊 Network Configuration Memo 📊\n\n"
        memo += "Network Topology:\n"
        switches = self.get_switches()
        for switch in switches:
            memo += f"- Switch DPID: {switch.get('dpid', 'unknown')}\n"
        if self.configs:
            memo += "\nRecent Flow Configurations:\n"
            for idx, config in enumerate(self.configs[-5:]):
                memo += f"Configuration {idx+1} - Switch {config['dpid']}:\n"
                for i, flow in enumerate(config['flows']):
                    memo += f"  Flow {i+1}: {json.dumps(flow)[:100]}...\n"
        if self.insights:
            memo += "\nNetwork Insights:\n"
            for insight in self.insights:
                memo += f"- {insight}\n"
        return memo

pox_server_url = os.getenv("POX_SERVER_URL", "http://localhost:8080")
logging_level = os.getenv("LOGGING_LEVEL", "INFO")

logging.basicConfig(level=logging_level, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

logger.info(f"Starting POX OpenFlow MCP Server connecting to: {pox_server_url}")
print(f"Initializing POXOpenFlowManager with URL: {pox_server_url}", file=sys.stderr)
pox_manager = POXOpenFlowManager(pox_server_url)

# Explicitly configure stdio transport
mcp = FastMCP(name="pox-openflow", version="0.1.0", transport="stdio")

@mcp.tool()
def get_switches() -> List[Dict[str, Any]]:
    """Get a list of all connected OpenFlow switches"""
    return pox_manager.get_switches()

@mcp.tool()
def get_switch_desc(dpid: str) -> Dict[str, Any]:
    """Get detailed information about a specific switch"""
    return pox_manager.get_switch_desc(dpid)

@mcp.tool()
def get_flow_stats(dpid: str, match: dict = None, table_id: str = None, out_port: str = None) -> List[Dict[str, Any]]:
    """Get flow statistics from a switch"""
    return pox_manager.get_flow_stats(dpid, match, table_id, out_port)

@mcp.tool()
def set_table(dpid: str, flows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Set the flow table on a switch"""
    return pox_manager.set_table(dpid, flows)

@mcp.tool()
def append_insight(insight: str):
    """Add a network insight to the configuration memo"""
    pox_manager.append_insight(insight)
    return "Network insight added to memo"

@mcp.resource("pox://network-config", name="Network Configuration Memo", description="A document containing network topology, configurations, and insights", mime_type="text/plain")
def get_network_config() -> str:
    return pox_manager._synthesize_network_memo()

@mcp.resource("pox://topology", name="Network Topology", description="Current network topology with connected switches", mime_type="text/plain")
def get_topology() -> str:
    switches = pox_manager.get_switches()
    return f"Network Topology:\n{json.dumps(switches, indent=2)}"

@mcp.prompt(name="pox-network-manager", description="A prompt to help you manage OpenFlow networks using POX")
def pox_network_manager_prompt(topic: str) -> List[base.Message]:
    prompt = PROMPT_TEMPLATE.format(topic=topic).strip()
    return [base.Message(role="user", content=[base.TextContent(type="text", text=prompt)])]

@mcp.prompt(name="simple-hub", description="Configure a switch to act as a simple hub that forwards all packets to all ports")
def simple_hub_prompt(dpid: str) -> List[base.Message]:
    hub_prompt = f"""
    Please help me configure the switch with DPID {dpid} to act as a simple hub.
    A hub should forward all incoming packets to all other ports.
    Use the set_table tool to implement this configuration.
    Afterwards, please explain the configuration and how it works.
    """.strip()
    return [base.Message(role="user", content=[base.TextContent(type="text", text=hub_prompt)])]

@mcp.prompt(name="learning-switch", description="Configure a switch to act as a learning L2 switch")
def learning_switch_prompt(dpid: str) -> List[base.Message]:
    switch_prompt = f"""
    Please help me configure the switch with DPID {dpid} to act as a learning L2 switch.
    A learning switch should:
    1. Forward packets based on learned MAC addresses
    2. Flood packets with unknown destinations
    3. Learn MAC addresses from source addresses of incoming packets
    Please outline how this can be implemented in POX, even though the full implementation might require some programmatic components.
    """.strip()
    return [base.Message(role="user", content=[base.TextContent(type="text", text=switch_prompt)])]

print("Starting FastMCP server...", file=sys.stderr)
logger.debug("Entering server.run()")
try:
    mcp.run()
except Exception as e:
    logger.error(f"Server crashed: {str(e)}", exc_info=True)
    print(f"Server crashed with exception: {str(e)}", file=sys.stderr)
    raise
    