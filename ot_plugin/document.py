from __future__ import annotations
from pathlib import Path
from typing import Literal, cast
from weakref import WeakValueDictionary
import krita
from krita import Krita
from PyQt5.QtCore import QObject, QUuid, QByteArray, QTimer, pyqtSignal
from PyQt5.QtGui import QImage
from typing import Optional
from .image import Extent, Bounds, Mask, Image
from .layer import Layer, LayerManager, LayerType
from .pose import Pose
from .localization import translate as _
from .util import acquire_elements, ot_client_logger 
from .eventloop import run as asyncio_run
import time
from .util import LRUCacheWithTTL, compare_qbytearray
from functools import partial

class Document(QObject):
    """Document interface. Used as placeholder when there is no open Document in Krita."""

    selection_bounds_changed = pyqtSignal()
    current_time_changed = pyqtSignal()
    pixel_level_changed = pyqtSignal()
    _layers: LayerManager

    def __init__(self):
        super().__init__()
        self._layers = LayerManager(None)

    @property
    def extent(self):
        return Extent(0, 0)

    @property
    def filename(self) -> str:
        return ""

    def check_color_mode(self) -> tuple[Literal[True], None] | tuple[Literal[False], str]:
        return True, None

    def create_mask_from_selection(
        self, padding: float = 0.0, multiple=8, min_size=0, square=False, invert=False
    ) -> tuple[Mask, Bounds] | tuple[None, None]:
        raise NotImplementedError

    def get_image(
        self, bounds: Bounds | None = None, exclude_layers: list[Layer] | None = None
    ) -> Image:
        raise NotImplementedError

    def resize(self, extent: Extent):
        raise NotImplementedError

    def annotate(self, key: str, value: QByteArray):
        pass

    def find_annotation(self, key: str) -> QByteArray | None:
        return None

    def remove_annotation(self, key: str):
        pass

    def add_pose_character(self, layer: Layer):
        raise NotImplementedError

    def import_animation(self, files: list[Path], offset: int = 0):
        raise NotImplementedError

    @property
    def layers(self) -> LayerManager:
        return self._layers

    @property
    def selection_bounds(self) -> Bounds | None:
        return None

    @property
    def resolution(self) -> float:
        return 0.0

    @property
    def playback_time_range(self) -> tuple[int, int]:
        return 0, 0

    @property
    def current_time(self) -> int:
        return 0

    @property
    def is_valid(self) -> bool:
        return True

    @property
    def is_active(self) -> bool:
        return Krita.instance().activeDocument() is None


class KritaDocument(Document):
    """Wrapper around a Krita Document (opened image). Allows to retrieve and modify pixel data.
    Keeps track of selection and current time changes by polling at a fixed interval.
    """

    _doc: krita.Document
    _id: QUuid
    _layers: LayerManager
    _poller: QTimer
    _selection_bounds: Bounds | None = None
    _current_time: int = 0
    _instances: WeakValueDictionary[str, KritaDocument] = WeakValueDictionary()
    _virtual_doc_timer: QTimer

    def __init__(self, krita_document: krita.Document):
        super().__init__()
        self._doc = krita_document
        self._id = krita_document.rootNode().uniqueId()
        self._poller = QTimer()
        self._poller.setInterval(20)
        self._poller.timeout.connect(self._poll)
        self._poller.start()
        self._instances[self._id.toString()] = self
        self._layers = LayerManager(krita_document)

        self._previous_image: Optional[QByteArray] = None
        self._diff_cache: LRUCacheWithTTL[str, dict[int, int]] = LRUCacheWithTTL(capacity=50, ttl=0.05)

        # NOTE periodically update current document with value from virtual document state
        self._virtual_doc_state: LRUCacheWithTTL[int, int] = LRUCacheWithTTL(capacity=self._doc.width() * self._doc.height(), ttl=0.05)
        self._virtual_doc_timer = QTimer()
        self._virtual_doc_timer.setInterval(1000)
        self._virtual_doc_timer.timeout.connect(self._schedule_virtual_doc_update)
        self._virtual_doc_timer.start()

    def _schedule_virtual_doc_update(self):
        # NOTE scan the doc every 1000ms.
        if self.is_valid:
            should_check = False 
            selection = self._doc.selection()
            selection_bounds = _selection_bounds(selection) if selection else None
            if selection_bounds:
                should_check = True
            current_time = self.current_time
            if current_time != self._current_time:
                should_check = True
            asyncio_run(partial(self._async_check_pixel_and_update_pixel, should_check)())
        else:
            self._virtual_doc_timer.stop()

    async def _async_check_pixel_and_update_pixel(self, should_check: bool):
        if should_check:
            await self._check_pixel_level_changes(_selection_bounds(self._doc.selection()))
        # TODO: uncomment this when we have a way to update the virtual document
        await self._virtual_doc_update()

    async def _virtual_doc_update(self):
        """Persist _virtual_doc_state by updating the entire document's pixels."""
        keys = list(await self._virtual_doc_state.keys())  # Convert keys to a list first
        for x, y in keys:
            bounds = Bounds(x, y, 1, 1)
            self._doc.setPixelData(self._virtual_doc_state.get((x, y)), *bounds)
        self._doc.refreshProjection()

    # def mousePressEvent(self, event: QMouseEvent):
    #     self._handle_local_change(event)

    # def mouseMoveEvent(self, event: QMouseEvent):
    #     self._handle_local_change(event)

    # def mouseReleaseEvent(self, event: QMouseEvent):
    #     self._handle_local_change(event)

    # def _handle_local_change(self, event: QMouseEvent):
    #     x, y = event.pos().x(), event.pos().y()
    #     pixel = self._doc.pixel(x, y)
    #     self._in_memory_array[(x, y)] = pixel
    #     self._virtual_doc_state[(x, y)] = pixel

    @classmethod
    def active(cls):
        if doc := Krita.instance().activeDocument():
            if (
                doc not in acquire_elements(Krita.instance().documents())
                or doc.activeNode() is None
            ):
                return None
            id = doc.rootNode().uniqueId().toString()
            return cls._instances.get(id) or KritaDocument(doc)
        return None

    @property
    def extent(self):
        return Extent(self._doc.width(), self._doc.height())

    @property
    def filename(self):
        return self._doc.fileName()

    @property
    def layers(self):
        return self._layers

    def check_color_mode(self):
        model = self._doc.colorModel()
        msg_fmt = _("Incompatible document: Color {0} must be {1} (current {0}: {2})")
        if model != "RGBA":
            return False, msg_fmt.format("model", "RGB/Alpha", model)
        depth = self._doc.colorDepth()
        if depth != "U8":
            return False, msg_fmt.format("depth", "8-bit integer", depth)
        return True, None

    def create_mask_from_selection(
        self, padding: float = 0.0, multiple=8, min_size=0, square=False, invert=False
    ):
        user_selection = self._doc.selection()
        if not user_selection:
            return None, None

        if _selection_is_entire_document(user_selection, self.extent):
            return None, None

        selection = user_selection.duplicate()
        original_bounds = Bounds(
            selection.x(), selection.y(), selection.width(), selection.height()
        )
        original_bounds = Bounds.clamp(original_bounds, self.extent)
        size_factor = original_bounds.extent.diagonal
        padding_pixels = int(padding * size_factor)

        if invert:
            selection.invert()

        bounds = _selection_bounds(selection)
        bounds = Bounds.pad(
            bounds, padding_pixels, multiple=multiple, min_size=min_size, square=square
        )
        bounds = Bounds.clamp(bounds, self.extent)
        data = selection.pixelData(*bounds)
        return Mask(bounds, data), original_bounds

    def get_image(self, bounds: Bounds | None = None, exclude_layers: list[Layer] | None = None):
        excluded: list[Layer] = []
        if exclude_layers:
            for layer in filter(lambda l: l.is_visible, exclude_layers):
                layer.hide()
                excluded.append(layer)
        if len(excluded) > 0:
            self._doc.refreshProjection()

        bounds = bounds or Bounds(0, 0, self._doc.width(), self._doc.height())
        img = QImage(self._doc.pixelData(*bounds), *bounds.extent, QImage.Format.Format_ARGB32)

        for layer in excluded:
            layer.show()
        if len(excluded) > 0:
            self._doc.refreshProjection()
        return Image(img)

    def resize(self, extent: Extent):
        res = self._doc.resolution()
        self._doc.scaleImage(extent.width, extent.height, res, res, "Bilinear")

    def annotate(self, key: str, value: QByteArray):
        self._doc.setAnnotation(f"ai_diffusion/{key}", f"AI Diffusion Plugin: {key}", value)

    def find_annotation(self, key: str) -> QByteArray | None:
        result = self._doc.annotation(f"ai_diffusion/{key}")
        return result if result.size() > 0 else None

    def remove_annotation(self, key: str):
        self._doc.removeAnnotation(f"ai_diffusion/{key}")

    def add_pose_character(self, layer: Layer):
        assert layer.type is LayerType.vector
        _pose_layers.add_character(cast(krita.VectorLayer, layer.node))

    def import_animation(self, files: list[Path], offset: int = 0):
        success = self._doc.importAnimation([str(f) for f in files], offset, 1)
        if not success and len(files) > 0:
            folder = files[0].parent
            raise RuntimeError(f"Failed to import animation from {folder}")

    @property
    def selection_bounds(self):
        return self._selection_bounds

    @property
    def resolution(self):
        return self._doc.resolution() / 72.0  # KisImage::xRes which is applied to vectors

    @property
    def playback_time_range(self):
        return self._doc.playBackStartTime(), self._doc.playBackEndTime()

    @property
    def current_time(self):
        return self._doc.currentTime()

    @property
    def is_valid(self):
        return self._doc in acquire_elements(Krita.instance().documents())

    @property
    def is_active(self):
        return self._doc == Krita.instance().activeDocument()

    def _poll(self):
        if self.is_valid:
            selection = self._doc.selection()
            selection_bounds = _selection_bounds(selection) if selection else None
            if selection_bounds != self._selection_bounds:
                self._selection_bounds = selection_bounds
                self.selection_bounds_changed.emit()
            
            current_time = self.current_time
            if current_time != self._current_time:
                self._current_time = current_time
                self.current_time_changed.emit() 
        else:
            self._poller.stop()

    def __eq__(self, other):
        if self is other:
            return True
        if isinstance(other, KritaDocument):
            return self._id == other._id
        return False

    async def _check_pixel_level_changes(self, bounds: Optional[Bounds] = None):
        """
        Check pixel level changes for document pixels within the given bounds.
        If no bounds are given, it will calculate the region of interest (i.e. the area that changes).
        """
        try:
            _bounds = bounds or self._get_region_of_interest()
            ot_client_logger.info(f"Checking pixel level changes for bounds: {_bounds}")
            current_image: QByteArray = self._doc.pixelData(
                _bounds.x, _bounds.y, _bounds.width, _bounds.height
            )
            ot_client_logger.info(f"Current image size: {current_image.size() if current_image else 'None'}, bounds: {_bounds}, doc size: {self._doc.width()}x{self._doc.height()}")
            current_time = time.time()
            if hasattr(self, "_previous_image") and self._previous_image:
                # FIXME compare_qbytearray is not implemented
                diff = compare_qbytearray(self._previous_image, current_image)
                if diff.any():
                    diff_dict = {index: value for index, value in enumerate(diff)}
                    ot_client_logger.info(f"Pixel level changes detected {time.time()}, {diff_dict}")
                    self._previous_image = current_image
                    await self._diff_cache.set(str(time.time()), diff_dict)
                    self.pixel_level_changed.emit()
                else:
                    ot_client_logger.info(f"No pixel level changes {time.time()}")
            else:
                self._previous_image = current_image
            ot_client_logger.info(f"time diff: {time.time() - current_time}")
        except Exception as e:
            ot_client_logger.error(f"Error getting pixel data: {e}")
            return

    @staticmethod
    async def pixel_equal(data1: QByteArray, data2: QByteArray) -> bool:
        """Compute the difference between two QByteArray objects representing pixel data."""
        
        if data1.size() != data2.size():
            raise ValueError("QByteArray objects must be of the same size to compute the difference.")

        for i in range(data1.size()):
            if data1[i] != data2[i]:
                return False
        return True

    @staticmethod
    async def compute_pixel_difference(original_data: QByteArray, incoming_data: QByteArray) -> dict[int, int]:
        """Compute the difference between two QByteArray objects representing pixel data."""
        if original_data.size() != incoming_data.size():
            raise ValueError("QByteArray objects must be of the same size to compute the difference.")

        difference_dict = {}

        for i in range(original_data.size()):
            if original_data[i] != incoming_data[i]:
                difference_dict[i] = incoming_data[i]

        return difference_dict 

    def _get_region_of_interest(self) -> Bounds:
        selection = self._doc.selection()
        if selection:
            ot_client_logger.info(f"Selection bounds: {_selection_bounds(selection)}")
            pass
            # return _selection_bounds(selection)
        # If no selection, fall back to the entire document
        return Bounds(0, 0, self._doc.width(), self._doc.height())
    

def _selection_bounds(selection: krita.Selection):
    return Bounds(selection.x(), selection.y(), selection.width(), selection.height())


def _selection_is_entire_document(selection: krita.Selection, extent: Extent):
    bounds = _selection_bounds(selection)
    if bounds.x > 0 or bounds.y > 0:
        return False
    if bounds.width + bounds.x < extent.width or bounds.height + bounds.y < extent.height:
        return False
    mask = selection.pixelData(*bounds)
    is_opaque = all(x == b"\xff" for x in mask)
    return is_opaque


class PoseLayers:
    _layers: dict[str, Pose] = {}
    _timer = QTimer()

    def __init__(self):
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.update)
        self._timer.start()

    def update(self):
        doc = KritaDocument.active()
        if not doc:
            return
        try:
            layer = doc.layers.active
        except Exception:
            return
        if not layer or layer.type is not LayerType.vector:
            return

        layer = cast(krita.VectorLayer, layer.node)
        pose = self._layers.setdefault(layer.uniqueId(), Pose(doc.extent))
        self._update(layer, acquire_elements(layer.shapes()), pose, doc.resolution)

    def add_character(self, layer: krita.VectorLayer):
        doc = KritaDocument.active()
        assert doc is not None
        pose = self._layers.setdefault(layer.uniqueId(), Pose(doc.extent))
        svg = Pose.create_default(doc.extent, pose.people_count).to_svg()
        shapes = acquire_elements(layer.addShapesFromSvg(svg))
        self._update(layer, shapes, pose, doc.resolution)

    def _update(
        self, layer: krita.VectorLayer, shapes: list[krita.Shape], pose: Pose, resolution: float
    ):
        changes = pose.update(shapes, resolution)  # type: ignore
        if changes:
            shapes = layer.addShapesFromSvg(changes)
            for shape in shapes:
                shape.setZIndex(-1)


_pose_layers = PoseLayers()
