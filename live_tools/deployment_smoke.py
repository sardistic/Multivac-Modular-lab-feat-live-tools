TOOL_VERSION = "1"

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "hotload_deployment_smoke",
            "description": "Verify live tool deployment activation.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    }
]


def hotload_deployment_smoke(args):
    return {"ok": True, "channel": "tool", "version": "1"}


TOOL_HANDLERS = {"hotload_deployment_smoke": hotload_deployment_smoke}
