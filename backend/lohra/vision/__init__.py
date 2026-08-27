"""Vision — image input for the agent (spec §9).

Canonical image parts are OpenAI-shaped (``image_url``), since the internal
message schema is an OpenAI superset; the anthropic transport converts them to
its ``image`` blocks at the boundary. The ``vision_analyze`` tool lets the agent
inspect an image file/url on demand.
"""
