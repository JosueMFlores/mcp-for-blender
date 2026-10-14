"""Validated, additive recipes executed on Blender's main thread.

Arguments are JSON data, never interpolated as Python source. The result uses
the same marker as blender_scripts so the server can return a structured report.
"""

from __future__ import annotations

import copy
import json
import math

from .blender_scripts import RESULT_MARKER

RECIPE_NAMES = ("prepare_game_asset", "product_lighting", "create_variants")

_CATALOG = {
    "prepare_game_asset": {
        "name": "prepare_game_asset",
        "description": (
            "Create static game-ready copies in a new collection, preserving the originals. "
            "Bake evaluated geometry at the current frame and world rotation/scale, optionally "
            "decimate to a per-object triangle budget and triangulate, and create missing UVs. "
            "The report includes actual triangle counts, budget warnings, and world bounds. "
            "Rigging, animation, and live modifiers are baked into the static copies."
        ),
        "options": {
            "collection_name": {"type": "string", "default": "MCP_GameAssets"},
            "triangulate": {"type": "boolean", "default": True},
            "triangle_budget": {"type": ["integer", "null"], "minimum": 1,
                                "maximum": 1_000_000, "default": None},
            "uv_method": {"enum": ["smart", "planar", "none"], "default": "smart"},
        },
    },
    "product_lighting": {
        "name": "product_lighting",
        "description": (
            "Add a camera and three area lights in a new studio collection, framed around "
            "the targets' evaluated world bounds. Existing lights and cameras remain. "
            "Optionally assign the new camera as the scene camera."
        ),
        "options": {
            "collection_name": {"type": "string", "default": "MCP_ProductStudio"},
            "set_scene_camera": {"type": "boolean", "default": True},
            "camera_lens": {"type": "number", "minimum": 1, "maximum": 500, "default": 50},
            "margin": {"type": "number", "minimum": 1.05, "maximum": 5, "default": 1.2},
            "light_energy": {"type": "number", "minimum": 0, "maximum": 1_000_000,
                             "default": 1000},
        },
    },
    "create_variants": {
        "name": "create_variants",
        "description": (
            "Duplicate each named object into a new collection, offset each set along a world "
            "axis, and preserve the originals. A selected parent/child hierarchy is remapped "
            "within each set; object animation and constraints are frozen at their current "
            "world transform. Data is copied unless linked_data is true."
        ),
        "options": {
            "collection_name": {"type": "string", "default": "MCP_Variants"},
            "count": {"type": "integer", "minimum": 1, "maximum": 32, "default": 3},
            "spacing": {"type": "number", "exclusiveMinimum": 0, "maximum": 1_000_000,
                        "default": 2},
            "axis": {"enum": ["X", "Y", "Z"], "default": "X"},
            "linked_data": {"type": "boolean", "default": False},
        },
    },
}


def list_recipes() -> list[dict]:
    """Return the public catalog, including accepted options and their defaults."""
    return [describe_recipe(name) for name in RECIPE_NAMES]


def describe_recipe(name: str) -> dict:
    if not isinstance(name, str) or name not in _CATALOG:
        raise ValueError(f"Unknown recipe {name!r}; choose one of {', '.join(RECIPE_NAMES)}")
    description = copy.deepcopy(_CATALOG[name])
    description["object_names"] = {"type": "array", "minItems": 1, "maxItems": 100,
                                   "uniqueItems": True, "items": {"type": "string"}}
    return description


def _name(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 255 or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty string of at most 255 characters without NUL")
    return value


def _options(name: str, options: dict | None) -> dict:
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    schema = _CATALOG[name]["options"]
    unknown = set(options) - set(schema)
    if unknown:
        raise ValueError("Unknown options: " + ", ".join(sorted(str(key) for key in unknown)))
    result = {key: copy.deepcopy(spec["default"]) for key, spec in schema.items()}
    result.update(options)
    for key, value in result.items():
        spec = schema[key]
        if key == "collection_name":
            _name(value, key)
        elif "enum" in spec:
            if not isinstance(value, str) or value not in spec["enum"]:
                raise ValueError(f"{key} must be one of {spec['enum']}")
        elif spec["type"] == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
        else:
            if value is None and isinstance(spec["type"], list) and "null" in spec["type"]:
                continue
            integer = spec["type"] == "integer" or (
                isinstance(spec["type"], list) and "integer" in spec["type"]
            )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a finite {'integer' if integer else 'number'}")
            if integer and not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{key} must be finite")
            if value < spec.get("minimum", 0) or value > spec["maximum"]:
                raise ValueError(f"{key} is outside the allowed range")
            if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
                raise ValueError(f"{key} must be greater than {spec['exclusiveMinimum']}")
    return result


def build_recipe(name: str, object_names: list[str], options: dict | None = None) -> str:
    """Build a complete Blender script after validating all user-controlled data.

    Raises ValueError before contacting Blender for unknown recipes/options or
    invalid targets. Missing objects and incompatible types are checked in
    Blender before a collection or object is created.
    """
    describe_recipe(name)
    if not isinstance(object_names, (list, tuple)) or not 1 <= len(object_names) <= 100:
        raise ValueError("object_names must contain between 1 and 100 explicit object names")
    names = [_name(value, "object_names item") for value in object_names]
    if len(set(names)) != len(names):
        raise ValueError("object_names must not contain duplicate names")
    args = {"recipe": name, "object_names": names, "options": _options(name, options)}
    script = _COMMON + "\n" + _SCRIPTS[name]
    body = "\n".join("    " + line if line.strip() else "" for line in script.strip("\n").splitlines())
    # The regular observation wrapper aliases the json module. Safe mode rejects
    # module aliases, so use permitted from-imports while keeping its wire format.
    encoded_args = json.dumps(json.dumps(args, allow_nan=False))
    return (
        "from json import loads as _recipe_loads, dumps as _recipe_dumps\n"
        f"ARGS = _recipe_loads({encoded_args})\n"
        f"def _main():\n{body}\n"
        f"print({RESULT_MARKER!r} + _recipe_dumps(_main()))\n"
    )


_COMMON = r'''
import bpy, math
from mathutils import Vector, Matrix

scene = bpy.context.scene
view_layer = bpy.context.view_layer
opts = ARGS["options"]
sources = [scene.objects.get(name) for name in ARGS["object_names"]]
missing = [name for name, obj in zip(ARGS["object_names"], sources) if obj is None]
if missing:
    return {"recipe": ARGS["recipe"], "error": "Missing objects in the active scene: " + ", ".join(missing)}
warnings = []
report = {"recipe": ARGS["recipe"], "objects": [], "warnings": warnings}
collection = None

def new_collection():
    coll = bpy.data.collections.new(opts["collection_name"])
    scene.collection.children.link(coll)
    coll["mcp_recipe"] = ARGS["recipe"]
    report["collection"] = coll.name
    return coll

def bounds(objects):
    dg = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        if obj.type in {"MESH", "CURVE", "SURFACE", "FONT", "META", "VOLUME"}:
            evaluated = obj.evaluated_get(dg)
            points.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
        else:
            points.append(obj.matrix_world.translation.copy())
    if not all(math.isfinite(float(component)) for point in points for component in point):
        raise ValueError("Targets have non-finite world bounds")
    lo = Vector([min(point[index] for point in points) for index in range(3)])
    hi = Vector([max(point[index] for point in points) for index in range(3)])
    return lo, hi

def bounds_report(objects):
    lo, hi = bounds(objects)
    return {"world_bounding_box": [list(lo), list(hi)], "size": list(hi - lo)}

saved_active = view_layer.objects.active
saved_selected = list(bpy.context.selected_objects)
saved_mode = saved_active.mode if saved_active else "OBJECT"

def object_mode():
    active = view_layer.objects.active
    if active and active.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

def select_only(obj):
    object_mode()
    for candidate in view_layer.objects:
        candidate.select_set(False)
    obj.hide_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj

def restore_context():
    try:
        object_mode()
        for candidate in view_layer.objects:
            candidate.select_set(False)
        for candidate in saved_selected:
            if view_layer.objects.get(candidate.name) == candidate:
                candidate.select_set(True)
        view_layer.objects.active = saved_active
        if saved_active and saved_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=saved_mode)
    except Exception as exc:
        warnings.append("Could not fully restore selection/mode: " + str(exc))
'''


_PREPARE_GAME_ASSET = r'''
import bmesh

if any(obj.type != "MESH" for obj in sources):
    return {"recipe": ARGS["recipe"], "error": "prepare_game_asset requires mesh objects only"}
if any(view_layer.objects.get(obj.name) != obj for obj in sources):
    return {"recipe": ARGS["recipe"], "error": "Targets must belong to the active view layer"}

def triangle_count(mesh):
    mesh.calc_loop_triangles()
    return len(mesh.loop_triangles)

def triangulate_mesh(mesh):
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bmesh.ops.triangulate(bm, faces=list(bm.faces))
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()

def planar_uv(mesh):
    # Project to the two widest local axes, including flat meshes without a
    # divide-by-zero. No view/area or edit-mode context is needed.
    uv = mesh.uv_layers.new(name="UVMap")
    if not mesh.vertices:
        return
    lo = [min(vertex.co[index] for vertex in mesh.vertices) for index in range(3)]
    hi = [max(vertex.co[index] for vertex in mesh.vertices) for index in range(3)]
    extents = [(hi[index] - lo[index], index) for index in range(3)]
    axes = [index for extent, index in sorted(extents, reverse=True)[:2]]
    span = [max(hi[index] - lo[index], 0.000001) for index in axes]
    for loop in mesh.loops:
        co = mesh.vertices[loop.vertex_index].co
        uv.data[loop.index].uv = [(co[index] - lo[index]) / extent for index, extent in zip(axes, span)]

try:
    object_mode()
    collection = new_collection()
    dg = bpy.context.evaluated_depsgraph_get()
    for source in sources:
        world = source.matrix_world.copy()
        mesh = bpy.data.meshes.new_from_object(source.evaluated_get(dg),
                                               preserve_all_data_layers=True, depsgraph=dg)
        obj = bpy.data.objects.new(source.name + "_Game", mesh)
        collection.objects.link(obj)
        view_layer.update()
        # Data API baking handles parents, negative scale, and shear without
        # altering the source object's transform or shared original mesh.
        mesh.transform(world.to_3x3().to_4x4())
        obj.matrix_world = Matrix.Translation(world.translation)
        if world.to_3x3().determinant() < 0:
            bm = bmesh.new()
            try:
                bm.from_mesh(mesh)
                bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
                bm.to_mesh(mesh)
            finally:
                bm.free()
        mesh.update()
        before = triangle_count(mesh)
        item = {"source": source.name, "name": obj.name, "triangles_before": before}
        report["objects"].append(item)
        budget = opts["triangle_budget"]
        if budget is not None and before > budget:
            select_only(obj)
            modifier = obj.modifiers.new(name="MCP_TriangleBudget", type="DECIMATE")
            modifier.ratio = max(0.000001, budget / before)
            modifier.use_collapse_triangulate = opts["triangulate"]
            bpy.ops.object.modifier_apply(modifier=modifier.name)
            mesh = obj.data
        if opts["triangulate"]:
            triangulate_mesh(mesh)
        uv_created = False
        if not mesh.uv_layers and opts["uv_method"] != "none":
            if opts["uv_method"] == "planar" or not mesh.polygons:
                planar_uv(mesh)
            else:
                select_only(obj)
                try:
                    bpy.ops.object.mode_set(mode="EDIT")
                    bpy.ops.mesh.select_all(action="SELECT")
                    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.02)
                finally:
                    object_mode()
            uv_created = True
        after = triangle_count(mesh)
        within_budget = budget is None or after <= budget
        if not within_budget:
            warnings.append(obj.name + ": decimation produced " + str(after) + " triangles; budget is " + str(budget))
        if source.modifiers or source.animation_data or source.parent:
            warnings.append(obj.name + ": current-frame evaluated geometry was baked as a static mesh")
        obj["mcp_recipe"] = ARGS["recipe"]
        obj["mcp_source"] = source.name
        obj["mcp_triangles"] = after
        if budget is not None:
            obj["mcp_triangle_budget"] = budget
        item.update({"triangles": after, "triangle_budget": budget,
                     "within_budget": within_budget, "uv_layers": len(mesh.uv_layers),
                     "uv_created": uv_created, "static_mesh": True})
        item.update(bounds_report([obj]))
except Exception as exc:
    report["error"] = str(exc)
finally:
    restore_context()
return report
'''


_PRODUCT_LIGHTING = r'''
try:
    # Compute and validate bounds before creating anything.
    lo, hi = bounds(sources)
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.05)
    aspect = (scene.render.resolution_x * scene.render.pixel_aspect_x /
              max(scene.render.resolution_y * scene.render.pixel_aspect_y, 0.000001))
    half_horizontal = math.atan(36 / (2 * opts["camera_lens"]))
    half_vertical = math.atan(math.tan(half_horizontal) / max(aspect, 0.000001))
    distance = radius / math.sin(min(half_horizontal, half_vertical)) * opts["margin"]
    collection = new_collection()
    camera_data = bpy.data.cameras.new("MCP_ProductCamera")
    camera_data.lens = opts["camera_lens"]
    camera_data.sensor_width = 36
    camera_data.sensor_fit = "HORIZONTAL"
    camera_data.clip_start = max(0.001, distance - radius * 2)
    camera_data.clip_end = max(100, distance + radius * 8)
    camera = bpy.data.objects.new("MCP_ProductCamera", camera_data)
    collection.objects.link(camera)
    camera.location = center + Vector((1, -1, 0.65)).normalized() * distance
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    report["objects"].append({"name": camera.name, "type": "CAMERA"})
    lights = [
        ("MCP_Key", (2, -2, 3), 1.0, 2.0, (1.0, 0.95, 0.9)),
        ("MCP_Fill", (-2, -1, 1), 0.45, 2.5, (0.9, 0.95, 1.0)),
        ("MCP_Rim", (0, 2, 2.5), 0.8, 1.5, (1.0, 1.0, 1.0)),
    ]
    for label, direction, strength, size, color in lights:
        data = bpy.data.lights.new(label, type="AREA")
        data.energy = opts["light_energy"] * strength
        data.shape = "DISK"
        data.size = max(radius * size, 0.01)
        data.color = color
        light = bpy.data.objects.new(label, data)
        collection.objects.link(light)
        light.location = center + Vector(direction) * radius
        light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
        report["objects"].append({"name": light.name, "type": "LIGHT", "energy": data.energy})
    if opts["set_scene_camera"]:
        scene.camera = camera
    report.update({"camera": camera.name, "scene_camera_assigned": opts["set_scene_camera"],
                   "world_bounding_box": [list(lo), list(hi)], "center": list(center)})
except Exception as exc:
    report["error"] = str(exc)
finally:
    restore_context()
return report
'''


_CREATE_VARIANTS = r'''
try:
    object_mode()
    collection = new_collection()
    axis = {"X": 0, "Y": 1, "Z": 2}[opts["axis"]]
    for index in range(1, opts["count"] + 1):
        copies = {}
        offset = Vector((0, 0, 0))
        offset[axis] = opts["spacing"] * index
        for source in sources:
            obj = source.copy()
            if source.data is not None and not opts["linked_data"]:
                obj.data = source.data.copy()
            obj.animation_data_clear()
            obj.constraints.clear()
            if source.animation_data or source.constraints:
                warnings.append(obj.name + ": object animation/constraints frozen at the current transform")
            obj.name = source.name + "_Variant_" + str(index).zfill(2)
            collection.objects.link(obj)
            copies[source.name] = obj
            obj["mcp_recipe"] = ARGS["recipe"]
            obj["mcp_source"] = source.name
            obj["mcp_variant"] = index
            report["objects"].append({"source": source.name, "name": obj.name,
                                       "variant": index, "offset": list(offset)})
        # Remap selected parents within this variant; detach external parents
        # so its world offset does not depend on an original parent transform.
        for source in sources:
            obj = copies[source.name]
            obj.parent = copies.get(source.parent.name) if source.parent else None
        # Set parents first, then matrices in hierarchy order. An unordered
        # input must not add the same offset twice to a selected child's parent.
        remaining = list(sources)
        while remaining:
            ready = [source for source in remaining if source.parent not in remaining]
            if not ready:
                raise ValueError("Cyclic target parent hierarchy")
            for source in ready:
                obj = copies[source.name]
                obj.matrix_world = Matrix.Translation(offset) @ source.matrix_world
                remaining.remove(source)
            view_layer.update()
    report["count_per_source"] = opts["count"]
    report["linked_data"] = opts["linked_data"]
except Exception as exc:
    report["error"] = str(exc)
finally:
    restore_context()
return report
'''

_SCRIPTS = {
    "prepare_game_asset": _PREPARE_GAME_ASSET,
    "product_lighting": _PRODUCT_LIGHTING,
    "create_variants": _CREATE_VARIANTS,
}
