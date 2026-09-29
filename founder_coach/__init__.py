"""founder_coach: the light runtime the coach plugin runs (ADR-0010).

Only what a founder's machine needs: the Knowledge pack reader, ONNX query models,
the search shared with the ytbrain pipeline, and (M3a) the founder store and MCP
server. Nothing here imports ytbrain, torch, LanceDB or yt-dlp.
"""
__version__ = "0.1.0"
