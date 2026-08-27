"""Image generation — the ``image_gen`` tool (text prompt -> saved image file).

The model returns base64 image data (OpenAI Images API / gpt-image-1); this
package decodes it, persists it to disk, and hands the agent back file paths.
The tool is intercepted and runner-bound, mirroring ``vision``: the schema lives
in the registry (the model sees it) while execution binds to a session runner.
"""
