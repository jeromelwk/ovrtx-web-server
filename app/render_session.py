"""ovrtx/ovstage integration: owns the renderer, the currently opened USD scene,
the orbit camera, and per-frame RTX rendering + picking.

Runs ovrtx in **standalone** mode (no ovstage attach): the composite scene is
opened directly on the renderer via the synchronous, deprecated-but-fully-
functional ``Renderer.open_usd_from_string`` / ``query_prims`` / ``read_attribute``
/ ``write_attribute`` convenience API. This avoids the ordinal/write-floor/
async-handle bookkeeping that the newer ovstage-attached workflow requires,
which is unnecessary complexity for a single-session interactive viewer.
Deprecation warnings are suppressed via ``RendererConfig``.
"""

from __future__ import annotations

import io
import logging
import math
import os
import threading
import time
from typing import Any, Optional

import numpy as np
from PIL import Image

import ovrtx
from ovrtx import AttributeFilterMode, Device, FilterKind, RendererConfig, Semantic

log = logging.getLogger("ovrtx_server.render_session")

CAMERA_PATH = "/OVCamera"
RENDER_PRODUCT_PATH = "/Render/OVServer/ViewportTexture0"
COLOR_VAR_PATH = "/Render/Vars/LdrColor"
PICK_HIT_VAR = "ovrtx_pick_hit"

# Must match the Camera prim authored in _build_composite_usda: used again by
# the framing heuristic to convert a scene bounding box into a distance that
# actually fits within this field of view, instead of an arbitrary constant.
CAMERA_FOCAL_LENGTH = 18.15
CAMERA_H_APERTURE = 20.955
CAMERA_V_APERTURE = 11.7872

# Move gizmo: three colored axis handles (a Cylinder shaft + Cone arrowhead
# each, oriented along their own local axis) parented under one Xform whose
# 'omni:xform' is rewritten every frame to sit at the selected prim's world
# position and to scale with camera distance, so it stays a constant size on
# screen. Picking one of these paths (instead of scene geometry) starts a
# constrained single-axis drag rather than a selection change.
GIZMO_ROOT = "/OVGizmo"
GIZMO_AXIS_PATHS = {"X": f"{GIZMO_ROOT}/AxisX", "Y": f"{GIZMO_ROOT}/AxisY", "Z": f"{GIZMO_ROOT}/AxisZ"}
GIZMO_SHAFT_LENGTH = 1.0
GIZMO_HEAD_LENGTH = 0.28
GIZMO_SCALE_FACTOR = 0.05  # world-space gizmo size per unit of camera distance

_SELECTION_GROUP = 1  # ovrtx selection-outline group id used for the selected prim

# Prims authored by us to host the camera / render config / fallback lighting /
# move gizmo, plus internal bookkeeping prims injected by the ovrtx/Fabric
# runtime itself; hidden from the scene-graph tree and properties panel, which
# show only the user's opened scene.
_RESERVED_PREFIXES = (
    "/Render", "/OVCamera", "/OVFallbackLighting", GIZMO_ROOT,
    "/TempChangeTracking", "/__Fabric_StageInfo",
)

# USD type names probed one-by-one to label tree nodes and pick an icon, since
# ovrtx's query API reports attribute schemas per prim but not each prim's own
# type name directly.
_CANDIDATE_TYPES = [
    "Xform", "Mesh", "Scope", "Camera", "PointInstancer",
    "DomeLight", "DistantLight", "SphereLight", "RectLight", "DiskLight", "CylinderLight",
    "Material", "Shader", "SkelRoot", "Skeleton", "BasisCurves", "Points", "NurbsPatch",
    "GeomSubset", "Volume",
]

_DTYPE_KIND = {0: "int", 1: "uint", 2: "float", 3: "handle", 4: "bfloat", 5: "complex", 6: "bool"}

# USD/TfType base-class and API-schema names that ovrtx's query_prims() reports
# as zero-data pseudo-attributes on every prim of a matching schema (in addition
# to the prim's own concrete type name, filtered separately per-prim).
_TFTYPE_TAG_NAMES = {
    "Boundable", "Gprim", "Imageable", "Xformable", "PointBased", "PrimvarsAPI",
    "SchemaBase", "Typed", "APISchemaBase", "CollectionAPI", "TfType::_Root",
}

_MIN_PITCH = math.radians(-89.0)
_MAX_PITCH = math.radians(89.0)


def _as_int(value) -> int:
    return int(value.value) if hasattr(value, "value") else int(value)


def _compose_local_matrix(translate: list[float], rotate_deg: list[float]) -> np.ndarray:
    """Row-vector 4x4 transform (v' = v @ M, translation in the last row,
    USD/GfMatrix4d convention) from a translation and an intrinsic XYZ Euler
    rotation in degrees (rotation applied as Rx then Ry then Rz)."""
    tx, ty, tz = (float(v) for v in translate)
    rx, ry, rz = (math.radians(float(v)) for v in rotate_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    rot_x = np.array([[1, 0, 0], [0, cx, sx], [0, -sx, cx]])
    rot_y = np.array([[cy, 0, -sy], [0, 1, 0], [sy, 0, cy]])
    rot_z = np.array([[cz, sz, 0], [-sz, cz, 0], [0, 0, 1]])
    rot = rot_x @ rot_y @ rot_z
    m = np.eye(4, dtype=np.float64)
    m[0:3, 0:3] = rot
    m[3, 0:3] = [tx, ty, tz]
    return m


def _fit_distance_for_radius(radius: float, margin: float = 1.3) -> float:
    """Distance along the camera's forward axis at which a bounding sphere of
    the given radius fits inside the authored camera's field of view (using
    whichever of horizontal/vertical FOV is narrower, so the object fits both
    ways), plus a comfort margin so it isn't touching the frame edges."""
    half_hfov = math.atan(CAMERA_H_APERTURE / (2.0 * CAMERA_FOCAL_LENGTH))
    half_vfov = math.atan(CAMERA_V_APERTURE / (2.0 * CAMERA_FOCAL_LENGTH))
    fit_tan = min(math.tan(half_hfov), math.tan(half_vfov))
    return (radius / fit_tan) * margin


def _compose_translate_scale_matrix(translate: np.ndarray, scale: float) -> np.ndarray:
    """Row-vector 4x4 transform: uniform scale then translate, no rotation
    (used to place/size the move gizmo, whose per-axis orientation is already
    baked into each handle's own local geometry)."""
    m = np.eye(4, dtype=np.float64) * scale
    m[3, 3] = 1.0
    m[3, 0:3] = translate
    return m


def _dtype_str(dtype) -> str:
    code = _as_int(dtype.code)
    kind = _DTYPE_KIND.get(code, f"code{code}")
    name = f"{kind}{_as_int(dtype.bits)}"
    lanes = _as_int(dtype.lanes)
    if lanes > 1:
        name += f"x{lanes}"
    return name


def _jsonable(value: Any):
    if isinstance(value, np.ndarray):
        shape = list(value.shape)
        value = value.tolist()
        if len(value) > 24:
            return {"shape": shape, "preview": value[:12], "truncated": True}
        return value
    if isinstance(value, (list, tuple)):
        flat = list(value)
        if len(flat) > 24:
            return {"shape": [len(flat)], "preview": flat[:12], "truncated": True}
        return flat
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


class RenderSession:
    """Single global viewer session: one renderer, one open scene at a time."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.renderer: Optional[ovrtx.Renderer] = None
        self.is_open = False
        self.width = 960
        self.height = 540
        self.source_path: Optional[str] = None

        self.prim_attrs: dict[str, dict[str, Any]] = {}
        self.prim_types: dict[str, str] = {}
        self.tree_children: dict[str, list[str]] = {}

        # Orbit camera state.
        self.target = np.array([0.0, 0.0, 0.0], dtype=np.float64)
        self.yaw = math.radians(135.0)
        self.pitch = math.radians(25.0)
        self.distance = 5.0

        self._camera_dirty = True
        self._pending_resize: Optional[tuple[int, int]] = None
        self._pending_pick: Optional[tuple[float, float]] = None
        self._last_render_ts = 0.0

        # Move gizmo state.
        self.selected_path: Optional[str] = None
        self.last_transform: dict[str, dict[str, list[float]]] = {}
        self._gizmo_state: Optional[dict[str, Any]] = None
        self._gizmo_shown = False
        self._outlined_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Renderer / scene lifecycle
    # ------------------------------------------------------------------
    def _ensure_renderer(self) -> None:
        if self.renderer is None:
            log.info("Creating ovrtx.Renderer (first call compiles/caches shaders, can take minutes)")
            cfg = RendererConfig(
                suppress_deprecation_warnings=True,
                selection_outline_enabled=True,
                selection_outline_width=3,
                selection_fill_mode=ovrtx.SelectionFillMode.EDGE_ONLY,
            )
            self.renderer = ovrtx.Renderer(config=cfg)
            self.renderer.set_selection_group_styles({
                _SELECTION_GROUP: ovrtx.SelectionGroupStyle(
                    outline_color=(0.906, 0.318, 0.075, 1.0), fill_color=(0.0, 0.0, 0.0, 0.0)
                ),
            })

    def open_usd(self, path: str, width: int, height: int) -> None:
        with self._lock:
            self._ensure_renderer()
            self.width = max(64, int(width))
            self.height = max(64, int(height))
            usda = self._build_composite_usda(path)
            self.renderer.open_usd_from_string(usda)
            self.source_path = path
            self._refresh_schema()
            self._reset_camera_heuristic()
            self._camera_dirty = True
            self.selected_path = None
            self.is_open = True
            log.info("Opened %s (%d prims)", path, len(self.prim_attrs))

    def _build_composite_usda(self, path: str) -> str:
        src = os.path.abspath(path).replace("\\", "/")
        shaft = GIZMO_SHAFT_LENGTH
        head = GIZMO_HEAD_LENGTH
        axes = [
            ("AxisX", "X", (0.85, 0.2, 0.2)),
            ("AxisY", "Y", (0.25, 0.8, 0.3)),
            ("AxisZ", "Z", (0.25, 0.45, 0.95)),
        ]
        gizmo_axes_usda = ""
        for name, axis, color in axes:
            gizmo_axes_usda += f"""
    def Xform "{name}"
    {{
        def Cylinder "Shaft"
        {{
            uniform token axis = "{axis}"
            double height = {shaft}
            double radius = {shaft * 0.02}
            color3f[] primvars:displayColor = [{color}]
            double3 xformOp:translate = {tuple(shaft / 2 if a == axis else 0 for a in "XYZ")}
            uniform token[] xformOpOrder = ["xformOp:translate"]
        }}
        def Cone "Head"
        {{
            uniform token axis = "{axis}"
            double height = {head}
            double radius = {shaft * 0.06}
            color3f[] primvars:displayColor = [{color}]
            double3 xformOp:translate = {tuple(shaft + head / 2 if a == axis else 0 for a in "XYZ")}
            uniform token[] xformOpOrder = ["xformOp:translate"]
        }}
    }}"""

        return f"""#usda 1.0
(
    subLayers = [
        @{src}@
    ]
)

def Camera "OVCamera"
{{
    float2 clippingRange = (0.01, 1000000)
    float focalLength = {CAMERA_FOCAL_LENGTH}
    float horizontalAperture = {CAMERA_H_APERTURE}
    float verticalAperture = {CAMERA_V_APERTURE}
    token projection = "perspective"
    double3 xformOp:translate = (0, 0, 5)
    uniform token[] xformOpOrder = ["xformOp:translate"]
}}

def "Render"
{{
    def "OVServer"
    {{
        def RenderProduct "ViewportTexture0"
        {{
            rel camera = </OVCamera>
            rel orderedVars = [
                </Render/Vars/LdrColor>,
            ]
            uniform int2 resolution = ({self.width}, {self.height})
        }}
    }}

    def "Vars"
    {{
        def RenderVar "LdrColor"
        {{
            uniform string sourceName = "LdrColor"
        }}
    }}

    def RenderSettings "OVRenderSettings"
    {{
        rel products = [</Render/OVServer/ViewportTexture0>]
    }}
}}

def Scope "OVFallbackLighting"
{{
    def DomeLight "OVFallbackDome"
    {{
        float inputs:intensity = 1000
        color3f inputs:color = (1, 1, 1)
    }}
    def DistantLight "OVFallbackKey"
    {{
        float inputs:intensity = 2500
        float inputs:angle = 2
        double3 xformOp:rotateXYZ:rotate = (-35, 25, 0)
        uniform token[] xformOpOrder = ["xformOp:rotateXYZ:rotate"]
    }}
}}

def Xform "OVGizmo"
{{
    token visibility = "invisible"
    double3 xformOp:translate = (0, 0, 0)
    uniform token[] xformOpOrder = ["xformOp:translate"]
{gizmo_axes_usda}
}}
"""

    # ------------------------------------------------------------------
    # Scene graph / properties (read-only queries against the runtime stage)
    # ------------------------------------------------------------------
    def _is_reserved(self, path: str) -> bool:
        if path in ("", "/"):
            return True
        return any(path == p or path.startswith(p + "/") for p in _RESERVED_PREFIXES)

    def _refresh_schema(self) -> None:
        r = self.renderer
        all_prims = r.query_prims(attribute_filter_mode=AttributeFilterMode.ALL)

        # Snapshot each AttributeInfo into a plain dict *now*: ovrtx's query
        # result buffers are apparently reused by later query_prims() calls
        # (the per-type queries below), which was observed to corrupt
        # AttributeInfo.dtype on already-fetched results if we kept the
        # ctypes-backed objects around instead of copying out what we need.
        prim_attrs: dict[str, dict[str, dict[str, Any]]] = {}
        for p, attrs in all_prims.items():
            if self._is_reserved(p):
                continue
            schema: dict[str, dict[str, Any]] = {}
            for name, info in attrs.items():
                is_array = bool(info.is_array)
                try:
                    type_display = _dtype_str(info.dtype) + ("[]" if is_array else "")
                except Exception:  # noqa: BLE001 - defensive: dtype metadata is not always reliable
                    type_display = "?"
                schema[name] = {"is_array": is_array, "semantic": info.semantic, "type_display": type_display}
            prim_attrs[p] = schema
        self.prim_attrs = prim_attrs

        prim_types: dict[str, str] = {}
        for type_name in _CANDIDATE_TYPES:
            try:
                matches = r.query_prims(
                    require_all=[(FilterKind.PRIM_TYPE, type_name)],
                    attribute_filter_mode=AttributeFilterMode.NONE,
                )
            except Exception:  # noqa: BLE001 - unsupported type name on this build; skip it
                continue
            for p in matches:
                if p in prim_attrs:
                    prim_types[p] = type_name
        self.prim_types = prim_types

        children: dict[str, list[str]] = {}
        for p in prim_attrs:
            parent = p.rsplit("/", 1)[0] or "/"
            children.setdefault(parent, []).append(p)
        for k in children:
            children[k].sort()
        self.tree_children = children

    def get_children(self, path: str) -> list[dict[str, Any]]:
        with self._lock:
            if not self.is_open:
                return []
            kids = self.tree_children.get(path, [])
            out = []
            for p in kids:
                out.append({
                    "path": p,
                    "name": p.rsplit("/", 1)[-1],
                    "type_name": self.prim_types.get(p, ""),
                    "has_children": p in self.tree_children,
                })
            return out

    def get_prim_info(self, path: str) -> dict[str, Any]:
        with self._lock:
            if not self.is_open:
                raise RuntimeError("Aucune scène ouverte")
            attrs = self.prim_attrs.get(path)
            if attrs is None:
                raise KeyError(f"Prim inconnu : {path}")

            metadata = {
                "Chemin": path,
                "Type USD": self.prim_types.get(path, "(inconnu)"),
                "Nombre d'attributs": len(attrs),
            }

            # USD/TfType schema tag markers (e.g. "Mesh", "Xformable", "SchemaBase")
            # surface as zero-data pseudo-attributes on every prim of that schema;
            # skip them by name rather than by their reported dtype, since dtype
            # metadata for some real attributes has been observed to read back
            # corrupted after later query_prims() calls reuse ovrtx's internal
            # query buffers.
            skip_names = _TFTYPE_TAG_NAMES | {self.prim_types.get(path, "")}

            out_attrs = []
            for name, info in sorted(attrs.items()):
                if name in skip_names:
                    continue
                try:
                    if info["is_array"]:
                        tensors = self.renderer.read_array_attribute(name, [path])
                        value = _jsonable(np.from_dlpack(tensors[path]))
                    else:
                        tensor = self.renderer.read_attribute(name, [path])
                        arr = np.from_dlpack(tensor)
                        value = _jsonable(arr[0] if arr.shape and arr.shape[0] == 1 else arr)
                    value = self._resolve_semantic(info, value)
                except Exception:  # noqa: BLE001 - some semantics/types aren't readable generically
                    log.debug("Skipping unreadable attribute %r on %s", name, path, exc_info=True)
                    continue

                out_attrs.append({"name": name, "type_name": info["type_display"], "value": value})

            return {
                "path": path,
                "metadata": metadata,
                "attributes": out_attrs,
                "transform": self._current_translate(path, attrs),
            }

    def _current_translate(self, path: str, attrs: dict[str, Any]) -> dict[str, list[float]]:
        """Best-effort current translation, read from whichever transform
        attribute is authored, to prefill the move/rotate panel. Rotation is
        not decomposed back out of the matrix (ambiguous without also
        tracking scale/shear) and always starts at 0 — applying the panel
        replaces the prim's local transform outright rather than nudging it."""
        for name in ("omni:xform", "omni:fabric:localMatrix"):
            if name not in attrs:
                continue
            try:
                tensor = self.renderer.read_attribute(name, [path])
                arr = np.asarray(np.from_dlpack(tensor), dtype=np.float64).reshape(-1, 4, 4)[0]
                return {"translate": [float(arr[3, 0]), float(arr[3, 1]), float(arr[3, 2])], "rotate": [0.0, 0.0, 0.0]}
            except Exception:  # noqa: BLE001 - best-effort prefill only
                continue
        return {"translate": [0.0, 0.0, 0.0], "rotate": [0.0, 0.0, 0.0]}

    def set_prim_transform(self, path: str, translate: list[float], rotate_deg: list[float]) -> None:
        """Replace a prim's local transform with the given translation
        (world units) and XYZ Euler rotation (degrees), via the same
        'omni:xform' write path used for the camera."""
        with self._lock:
            if not self.is_open:
                raise RuntimeError("Aucune scène ouverte")
            if path not in self.prim_attrs:
                raise KeyError(f"Prim inconnu : {path}")
            self._write_prim_transform(path, translate, rotate_deg)

    def _write_prim_transform(self, path: str, translate: list[float], rotate_deg: list[float]) -> None:
        matrix = _compose_local_matrix(translate, rotate_deg).reshape(1, 4, 4)
        self.renderer.write_attribute([path], "omni:xform", matrix, semantic=Semantic.XFORM_MAT4x4)
        self.last_transform[path] = {"translate": [float(v) for v in translate], "rotate": [float(v) for v in rotate_deg]}

    def _gizmo_axis_from_path(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        for axis, axis_path in GIZMO_AXIS_PATHS.items():
            if path == axis_path or path.startswith(axis_path + "/"):
                return axis
        return None

    def begin_gizmo_drag(self, axis: str) -> None:
        with self._lock:
            path = self.selected_path
            if not path or path not in self.prim_attrs or axis not in ("X", "Y", "Z"):
                return
            base = self.last_transform.get(path)
            if base is None:
                base = self._current_translate(path, self.prim_attrs[path])
            self._gizmo_state = {
                "path": path,
                "axis": axis,
                "translate": np.array(base["translate"], dtype=np.float64),
                "rotate": list(base["rotate"]),
            }

    def update_gizmo_drag(self, dx_px: float, dy_px: float) -> None:
        with self._lock:
            state = self._gizmo_state
            if state is None:
                return
            axis_vec = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}[state["axis"]]
            axis_vec = np.array(axis_vec, dtype=np.float64)
            right, up, _ = self._camera_basis()
            screen_x = float(np.dot(axis_vec, right))
            screen_y = -float(np.dot(axis_vec, up))
            sensitivity = self.distance * 0.0025
            scalar = (float(dx_px) * screen_x + float(dy_px) * screen_y) * sensitivity
            state["translate"] = state["translate"] + axis_vec * scalar
            self._write_prim_transform(state["path"], state["translate"].tolist(), state["rotate"])

    def end_gizmo_drag(self) -> None:
        with self._lock:
            self._gizmo_state = None

    def _resolve_semantic(self, info, value):
        """Resolve interned token/path ids (e.g. 'purpose', 'visibility',
        'material:binding') to human-readable strings when possible."""
        semantic = info.get("semantic")
        if semantic == Semantic.TOKEN_ID:
            pd = self.renderer._get_path_dict()
            if isinstance(value, list):
                return [pd.token_to_string(int(v)) for v in value]
            return pd.token_to_string(int(value))
        if semantic == Semantic.PATH_ID:
            if isinstance(value, list):
                return [self.renderer.resolve_prim_path_id(int(v)) for v in value]
            return self.renderer.resolve_prim_path_id(int(value))
        return value

    def _reset_camera_heuristic(self) -> None:
        """Best-effort initial framing from bounding-box attributes; falls back
        to a fixed distance when none are found. Prefers ovrtx's computed
        world-space '_worldExtent' (already resolved through each prim's
        transform) over the raw authored local-space 'extent', since combining
        local-space boxes from different prims without their transforms would
        misplace/mis-scale the framing. PointInstancer prims are excluded from
        this pass and handled via their instance 'positions' instead, since a
        PointInstancer's own extent does not describe the swept volume of all
        of its instances."""
        mins, maxs = [], []
        attr_name = "_worldExtent" if any("_worldExtent" in a for a in self.prim_attrs.values()) else "extent"
        for path, attrs in self.prim_attrs.items():
            if attr_name not in attrs or self.prim_types.get(path) == "PointInstancer":
                continue
            try:
                info = attrs[attr_name]
                if info["is_array"]:
                    tensor = self.renderer.read_array_attribute(attr_name, [path])[path]
                else:
                    tensor = self.renderer.read_attribute(attr_name, [path])
                arr = np.asarray(np.from_dlpack(tensor), dtype=np.float64).reshape(-1, 3)
                if arr.shape[0] >= 2:
                    mins.append(arr[0])
                    maxs.append(arr[1])
            except Exception:  # noqa: BLE001 - best-effort heuristic only
                continue

        for path, type_name in self.prim_types.items():
            if type_name != "PointInstancer" or "positions" not in self.prim_attrs.get(path, {}):
                continue
            try:
                tensor = self.renderer.read_array_attribute("positions", [path])[path]
                arr = np.asarray(np.from_dlpack(tensor), dtype=np.float64).reshape(-1, 3)
                if arr.shape[0] >= 1:
                    mins.append(arr.min(axis=0))
                    maxs.append(arr.max(axis=0))
            except Exception:  # noqa: BLE001 - best-effort heuristic only
                continue

        if mins:
            bbox_min = np.min(mins, axis=0)
            bbox_max = np.max(maxs, axis=0)
            center = (bbox_min + bbox_max) / 2.0
            diag = float(np.linalg.norm(bbox_max - bbox_min))
            self.target = center
            self.distance = max(_fit_distance_for_radius(diag / 2.0), 0.1)
        else:
            self.target = np.array([0.0, 0.0, 0.0], dtype=np.float64)
            self.distance = 15.0
        self.yaw = math.radians(135.0)
        self.pitch = math.radians(25.0)

    # ------------------------------------------------------------------
    # Camera control (called from the WebSocket handler thread)
    # ------------------------------------------------------------------
    def handle_control(self, msg: dict[str, Any]) -> None:
        with self._lock:
            kind = msg.get("type")
            if kind == "orbit":
                self.yaw -= float(msg.get("dx", 0)) * 0.006
                self.pitch = max(_MIN_PITCH, min(_MAX_PITCH, self.pitch - float(msg.get("dy", 0)) * 0.006))
                self._camera_dirty = True
            elif kind == "pan":
                dx, dy = float(msg.get("dx", 0)), float(msg.get("dy", 0))
                right, up, _ = self._camera_basis()
                scale = self.distance * 0.0015
                self.target = self.target - right * dx * scale + up * dy * scale
                self._camera_dirty = True
            elif kind == "dolly":
                factor = math.exp(float(msg.get("delta", 0)) * 0.01)
                self.distance = max(0.01, min(1.0e6, self.distance * factor))
                self._camera_dirty = True
            elif kind == "resize":
                w, h = int(msg.get("width", self.width)), int(msg.get("height", self.height))
                if abs(w - self.width) > 4 or abs(h - self.height) > 4:
                    self._pending_resize = (max(64, w), max(64, h))
            elif kind == "pick":
                self._pending_pick = (float(msg.get("x", 0.5)), float(msg.get("y", 0.5)))
            elif kind == "select":
                self.selected_path = msg.get("path") or None
            elif kind == "gizmo_drag_start":
                self.begin_gizmo_drag(msg.get("axis", ""))
            elif kind == "gizmo_drag":
                self.update_gizmo_drag(float(msg.get("dx", 0)), float(msg.get("dy", 0)))
            elif kind == "gizmo_drag_end":
                self.end_gizmo_drag()
            elif kind == "frame_selected":
                self._frame_selected()

    def _frame_selected(self) -> None:
        """Re-center and dolly the camera onto the selected prim (the 'F' shortcut)."""
        path = self.selected_path
        if not path or path not in self.prim_attrs:
            return
        anchor, radius = self._selected_gizmo_anchor(path)
        self.target = anchor
        if radius > 1e-6:
            self.distance = max(_fit_distance_for_radius(radius), 0.1)
        self._camera_dirty = True

    def _camera_basis(self):
        forward = np.array([
            math.cos(self.pitch) * math.cos(self.yaw),
            math.cos(self.pitch) * math.sin(self.yaw),
            math.sin(self.pitch),
        ])
        world_up = np.array([0.0, 0.0, 1.0])
        right = np.cross(forward, world_up)
        norm = np.linalg.norm(right)
        right = right / norm if norm > 1e-9 else np.array([1.0, 0.0, 0.0])
        up = np.cross(right, forward)
        return right, up, forward

    def _camera_matrix(self) -> np.ndarray:
        right, up, forward = self._camera_basis()
        eye = self.target - forward * self.distance
        m = np.eye(4, dtype=np.float64)
        m[0, 0:3] = right
        m[1, 0:3] = up
        m[2, 0:3] = -forward
        m[3, 0:3] = eye
        return m

    def _apply_camera(self) -> None:
        if not self._camera_dirty:
            return
        matrix = self._camera_matrix().reshape(1, 4, 4)
        try:
            self.renderer.write_attribute([CAMERA_PATH], "omni:xform", matrix, semantic=Semantic.XFORM_MAT4x4)
        except Exception:
            log.exception("Failed to write camera transform")
        self._camera_dirty = False

    def _apply_resize(self) -> None:
        if self._pending_resize is None:
            return
        w, h = self._pending_resize
        self._pending_resize = None
        try:
            self.renderer.write_attribute([RENDER_PRODUCT_PATH], "resolution", np.array([[w, h]], dtype=np.uint32))
            self.width, self.height = w, h
        except Exception:
            log.warning("Live RenderProduct resize to %dx%d failed; keeping %dx%d", w, h, self.width, self.height)

    def _read_extent_world(self, path: str, attrs: dict[str, Any]) -> Optional[np.ndarray]:
        """Read a (2, 3) [min, max] array from the given extent-like attribute."""
        if "_worldExtent" not in attrs:
            return None
        try:
            info = attrs["_worldExtent"]
            if info["is_array"]:
                tensor = self.renderer.read_array_attribute("_worldExtent", [path])[path]
            else:
                tensor = self.renderer.read_attribute("_worldExtent", [path])
            arr = np.asarray(np.from_dlpack(tensor), dtype=np.float64).reshape(-1, 3)
            if arr.shape[0] >= 2:
                return arr[:2]
        except Exception:  # noqa: BLE001 - best-effort only
            pass
        return None

    def _selected_gizmo_anchor(self, path: str) -> tuple[np.ndarray, float]:
        """World-space (position, radius) to attach the gizmo/outline to.

        Anchors to the center of ovrtx's resolved world-space extent
        ('_worldExtent'), not the prim's local-to-world translation: several
        real-world assets (this repo's Pot sample included) bake a large
        offset into their mesh vertex data that a matching large translate
        then cancels out, so the prim's own origin can sit far from where its
        geometry actually appears. The extent's center is correct regardless
        of how a given asset's pivot happens to be authored.
        """
        attrs = self.prim_attrs.get(path, {})
        extent = self._read_extent_world(path, attrs)
        if extent is not None:
            center = (extent[0] + extent[1]) / 2.0
            radius = float(np.linalg.norm(extent[1] - extent[0])) / 2.0
            return center, radius

        # No resolved world extent available: fall back to the prim's own
        # local-to-world translation (correct whenever its origin already
        # sits on its geometry, which is the common case).
        for name in ("omni:fabric:worldMatrix", "omni:xform", "omni:fabric:localMatrix"):
            if name not in attrs:
                continue
            try:
                tensor = self.renderer.read_attribute(name, [path])
                arr = np.asarray(np.from_dlpack(tensor), dtype=np.float64).reshape(-1, 4, 4)[0]
                return arr[3, 0:3].copy(), 0.0
            except Exception:  # noqa: BLE001 - best-effort only
                continue
        return np.array([0.0, 0.0, 0.0], dtype=np.float64), 0.0

    def _apply_gizmo(self) -> None:
        """Position/scale the move gizmo on the selected prim each frame (so it
        tracks drags and stays a constant size as the camera zooms), or hide it
        when nothing is selected."""
        path = self.selected_path
        if not path or path not in self.prim_attrs:
            if self._gizmo_shown:
                try:
                    self.renderer.write_attribute([GIZMO_ROOT], "visibility", ["invisible"])
                except Exception:
                    log.exception("Failed to hide move gizmo")
                self._gizmo_shown = False
            return

        world_pos, radius = self._selected_gizmo_anchor(path)
        scale = max(self.distance * GIZMO_SCALE_FACTOR, radius * 0.6, 1e-4)
        matrix = _compose_translate_scale_matrix(world_pos, scale).reshape(1, 4, 4)
        try:
            self.renderer.write_attribute([GIZMO_ROOT], "omni:xform", matrix, semantic=Semantic.XFORM_MAT4x4)
            if not self._gizmo_shown:
                self.renderer.write_attribute([GIZMO_ROOT], "visibility", ["inherited"])
                self._gizmo_shown = True
        except Exception:
            log.exception("Failed to update move gizmo")

    def _apply_selection_outline(self) -> None:
        """Keep the RTX selection-outline pass in sync with the selected prim:
        clear the previous prim's outline group and assign the new one,
        only when the selection actually changed."""
        path = self.selected_path if (self.selected_path in self.prim_attrs) else None
        if path == self._outlined_path:
            return
        try:
            if self._outlined_path is not None:
                self.renderer.set_selection_outline_group_strings([self._outlined_path], 0)
            if path is not None:
                self.renderer.set_selection_outline_group_strings([path], _SELECTION_GROUP)
        except Exception:
            log.exception("Failed to update selection outline")
        self._outlined_path = path

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render_frame(self) -> tuple[Optional[bytes], Optional[dict[str, Any]]]:
        with self._lock:
            if not self.is_open or self.renderer is None:
                return None, None

            now = time.monotonic()
            dt = now - self._last_render_ts if self._last_render_ts else 1.0 / 30.0
            self._last_render_ts = now

            self._apply_resize()
            self._apply_camera()
            self._apply_gizmo()
            self._apply_selection_outline()

            pick_rect = None
            if self._pending_pick is not None:
                x, y = self._pending_pick
                self._pending_pick = None
                x = min(max(x, 0.0), 1.0)
                y = min(max(y, 0.0), 1.0)
                px = min(int(x * self.width), self.width - 1)
                py = min(int(y * self.height), self.height - 1)
                pick_rect = (px / self.width, py / self.height, (px + 1) / self.width, (py + 1) / self.height)
                try:
                    self.renderer.enqueue_pick_query(RENDER_PRODUCT_PATH, *pick_rect)
                except Exception:
                    log.exception("Failed to enqueue pick query")
                    pick_rect = None

            try:
                products = self.renderer.step({RENDER_PRODUCT_PATH}, min(max(dt, 1.0 / 60.0), 1.0))
            except Exception:
                log.exception("step() failed")
                return None, None

            jpeg_bytes = None
            pick_path = None
            try:
                product = products[RENDER_PRODUCT_PATH]
                frame = product.frames[-1]

                color_var = frame.render_vars.get(COLOR_VAR_PATH)
                if color_var is not None:
                    with color_var.map(device=Device.CPU) as rv:
                        arr = np.from_dlpack(rv)
                        jpeg_bytes = self._encode_jpeg(arr)

                if pick_rect is not None:
                    pick_var = frame.render_vars.get(PICK_HIT_VAR)
                    if pick_var is not None:
                        with pick_var.map(device=Device.CPU) as rv:
                            hit_count = int(np.from_dlpack(rv.params["hitCount"]))
                            if hit_count > 0:
                                prim_path_ids = np.from_dlpack(rv["primPath"])
                                pid = int(np.asarray(prim_path_ids).reshape(-1)[0])
                                pick_path = self.renderer.resolve_prim_path_id(pid)
            except Exception:
                log.exception("Failed reading step outputs")

            pick_info = None
            if pick_path is not None:
                pick_info = {"path": pick_path, "axis": self._gizmo_axis_from_path(pick_path)}

            return jpeg_bytes, pick_info

    @staticmethod
    def _encode_jpeg(arr: np.ndarray) -> bytes:
        if arr.ndim == 3 and arr.shape[-1] == 4:
            img = Image.fromarray(arr, mode="RGBA").convert("RGB")
        elif arr.ndim == 3 and arr.shape[-1] == 3:
            img = Image.fromarray(arr, mode="RGB")
        else:
            img = Image.fromarray(arr)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
