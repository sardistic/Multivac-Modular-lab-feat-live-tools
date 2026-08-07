BEHAVIOR_VERSION = "1"

BEHAVIOR_SETTINGS = {
    "deployment.smoke": {
        "ok": True,
        "channel": "behavior",
        "version": "1",
    }
}

async def setup(context):
    return {"ready": True}

async def healthcheck(context):
    return context.state["ready"] is True

async def teardown(context):
    context.state["ready"] = False
