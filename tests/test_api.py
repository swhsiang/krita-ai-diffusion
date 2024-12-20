from PyQt5.QtCore import Qt

from ot_plugin.api import (
    WorkflowInput,
    WorkflowKind,
    ControlInput,
    ExtentInput,
    ImageInput,
    ConditioningInput,
)
from ot_plugin.image import Extent, Image, ImageFileFormat
from ot_plugin.resources import ControlMode
from ot_plugin.util import ensure


def test_defaults():
    input = WorkflowInput(WorkflowKind.refine)
    data = input.to_dict()
    assert data == {"kind": "refine"}
    result = WorkflowInput.from_dict(data)
    assert result == input


def _ensure_cmp(img: Image | None):
    return ensure(img).to_numpy_format()


def test_serialize():
    input = WorkflowInput(WorkflowKind.generate)
    input.images = ImageInput(ExtentInput(Extent(1, 1), Extent(2, 2), Extent(3, 3), Extent(4, 4)))
    input.images.initial_image = Image.create(Extent(2, 2), Qt.GlobalColor.green)
    input.conditioning = ConditioningInput(
        "prompt",
        control=[
            ControlInput(
                ControlMode.line_art,
                Image.create(Extent(2, 2), Qt.GlobalColor.red),
                0.4,
                (0.1, 0.9),
            ),
            ControlInput(
                ControlMode.depth, Image.create(Extent(4, 2), Qt.GlobalColor.blue), 0.8, (0.2, 0.5)
            ),
            ControlInput(ControlMode.blur, None, 0.5),
        ],
    )

    data = input.to_dict(ImageFileFormat.webp_lossless)
    result = WorkflowInput.from_dict(data)
    assert result.images is not None and result.images.initial_image is not None
    assert (
        result.images.initial_image.to_numpy_format()
        == input.images.initial_image.to_numpy_format()
    )
    input_control = ensure(input.conditioning).control
    result_control = ensure(result.conditioning).control
    assert _ensure_cmp(result_control[0].image) == _ensure_cmp(input_control[0].image)
    assert _ensure_cmp(result_control[1].image) == _ensure_cmp(input_control[1].image)
    assert result_control[2].image is None
    assert result == input
