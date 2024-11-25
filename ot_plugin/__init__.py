"""Generative AI plugin for Krita"""

__version__ = "1.27.1"

import importlib.util

if not importlib.util.find_spec(".websockets.src", "ot_plugin"):
    raise ImportError(
        "Could not find websockets module. This indicates that it was not installed with the"
        " plugin. Please make sure to download a plugin release package (NOT just the source!). You"
        " can find the latest release package here:"
        " https://github.com/Acly/krita-ai-diffusion/releases"
    )

# if not importlib.util.find_spec(".pydantic", "ot_plugin"):
#     raise ImportError(
#         "Could not find pydantic module. This indicates that it was not installed with the"
#         " plugin. Please make sure to download a plugin release package (NOT just the source!). You"
#         " can find the latest release package here:"
#         " https://github.com/Acly/krita-ai-diffusion/releases"
#     )

# The following imports depend on the code running inside Krita, so the cannot be imported in tests.
if importlib.util.find_spec("krita"):
    from .extension import AIToolsExtension