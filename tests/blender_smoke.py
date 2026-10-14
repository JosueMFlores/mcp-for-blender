"""Real Blender smoke test; run only in an isolated factory-startup process.

blender -b --factory-startup --python-exit-code 1 --python tests/blender_smoke.py

All .blend files belong to one TemporaryDirectory. This script does not start
the socket server, install an addon, or access a running Blender session.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import math
import sys
import tempfile
import traceback
import types
from pathlib import Path

import bpy
import bmesh
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# These pure-Python modules need no MCP/HTTP dependencies. The package's public
# __init__ imports server.py, so isolate the package namespace when Blender's
# bundled Python does not have the server environment installed.
package = types.ModuleType("blender_mcp")
package.__path__ = [str(ROOT / "src" / "blender_mcp")]
sys.modules["blender_mcp"] = package

from blender_mcp.blender_scripts import parse_result
from blender_mcp.recipes import RECIPE_NAMES, build_recipe
from blender_mcp.safe_mode import validate_code


def assert_vector(actual, expected, tolerance=1e-4):
    assert len(actual) == len(expected)
    assert max(abs(a - b) for a, b in zip(actual, expected)) < tolerance, (list(actual), list(expected))


def selected():
    return sorted(obj.name for obj in bpy.context.selected_objects)


def select_only(obj):
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for candidate in bpy.context.view_layer.objects:
        candidate.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def run_recipe(name, objects, options=None):
    code = build_recipe(name, objects, options)
    validate_code(code)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(code, {})
    result = parse_result(output.getvalue())
    assert not result.get("error"), result
    assert not any("Could not fully restore" in message for message in result["warnings"]), result
    return result


def recovery_server(directory):
    """Extract production recovery methods without importing addon dependencies."""
    tree = ast.parse((ROOT / "addon.py").read_text(encoding="utf-8"))
    original = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == "BlenderMCPServer")
    methods = [node for node in original.body
               if (isinstance(node, ast.FunctionDef) and
                   ("checkpoint" in node.name or node.name == "execute_code")) or
               (isinstance(node, ast.Assign) and
                any(isinstance(target, ast.Name) and target.id == "_checkpoint_store"
                    for target in node.targets))]
    cls = ast.ClassDef(name="SmokeRecoveryServer", bases=[], keywords=[], body=methods,
                       decorator_list=[])
    extracted = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    namespace = {"bpy": bpy, "io": io, "json": json, "traceback": traceback,
                 "redirect_stdout": contextlib.redirect_stdout}
    exec(compile(extracted, str(ROOT / "addon.py"), "exec"), namespace)
    server_class = namespace["SmokeRecoveryServer"]
    directory.mkdir()
    server_class._checkpoint_store = {"directory": str(directory), "entries": []}
    return server_class()


def mesh_snapshot(obj):
    obj.update_from_editmode()
    return {
        "coords": [tuple(vertex.co) for vertex in obj.data.vertices],
        "world": [tuple(row) for row in obj.matrix_world],
        "uvs": len(obj.data.uv_layers),
        "polygons": len(obj.data.polygons),
    }


def recipe_checks():
    for recipe in RECIPE_NAMES:
        validate_code(build_recipe(recipe, ["Cube"]))

    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=1)
    source = bpy.context.object
    source.name = "SmokeAsset"
    for uv in list(source.data.uv_layers):
        source.data.uv_layers.remove(uv)
    source.location = (4, 2, 3)
    source.rotation_euler = (0.2, 0.4, 0.6)
    source.scale = (-2, 1.5, 0.7)
    parent = bpy.data.objects.new("SmokeParent", None)
    bpy.context.scene.collection.objects.link(parent)
    parent.location = (1, 2, 0)
    parent.rotation_euler = (0, 0, 0.15)
    source.parent = parent
    modifier = source.modifiers.new("OriginalSubdivision", "SUBSURF")
    modifier.levels = 1
    bpy.context.view_layer.update()
    select_only(source)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(source.data)
    bm.verts.ensure_lookup_table()
    bm.verts[0].co.z += 0.12
    bmesh.update_edit_mesh(source.data)
    original = mesh_snapshot(source)
    original_selected = selected()

    game = run_recipe("prepare_game_asset", [source.name], {"triangle_budget": 200})
    assert source.mode == "EDIT"
    assert selected() == original_selected
    assert bpy.context.view_layer.objects.active == source
    assert mesh_snapshot(source) == original
    assert source.modifiers[0] == modifier
    prepared = bpy.data.objects[game["objects"][0]["name"]]
    assert prepared.data != source.data and not prepared.modifiers and prepared.parent is None
    assert_vector(prepared.scale, (1, 1, 1))
    assert_vector(prepared.rotation_euler, (0, 0, 0))
    assert_vector(prepared.location, source.matrix_world.translation)
    assert all(len(polygon.vertices) == 3 for polygon in prepared.data.polygons)
    assert prepared.data.uv_layers
    item = game["objects"][0]
    prepared.data.calc_loop_triangles()
    assert item["triangles"] == len(prepared.data.loop_triangles)
    assert item["within_budget"] == (item["triangles"] <= 200)
    assert prepared["mcp_triangle_budget"] == 200

    planar = run_recipe("prepare_game_asset", [source.name],
                        {"triangulate": False, "uv_method": "planar"})
    planar_obj = bpy.data.objects[planar["objects"][0]["name"]]
    assert len(planar_obj.data.uv_layers) == 1
    assert all(math.isfinite(component) for entry in planar_obj.data.uv_layers[0].data for component in entry.uv)
    assert mesh_snapshot(source) == original

    old_camera = bpy.context.scene.camera
    studio = run_recipe("product_lighting", [source.name], {"set_scene_camera": False})
    assert bpy.context.scene.camera == old_camera
    camera = bpy.data.objects[studio["camera"]]
    assert camera.type == "CAMERA"
    assert len(studio["objects"]) == 4
    assert source.mode == "EDIT" and selected() == original_selected
    bpy.context.view_layer.update()
    evaluated = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
    for corner in evaluated.bound_box:
        projected = world_to_camera_view(bpy.context.scene, camera, evaluated.matrix_world @ Vector(corner))
        assert 0 <= projected.x <= 1 and 0 <= projected.y <= 1 and projected.z > 0, tuple(projected)
    assigned = run_recipe("product_lighting", [source.name])
    assert assigned["camera"] != studio["camera"]
    assert assigned["collection"] != studio["collection"]
    assert bpy.context.scene.camera.name == assigned["camera"]
    assert mesh_snapshot(source) == original

    source_world = source.matrix_world.copy()
    parent_world = parent.matrix_world.copy()
    variants = run_recipe("create_variants", [source.name, parent.name], {"count": 2, "spacing": 5})
    assert len(variants["objects"]) == 4
    assert source.mode == "EDIT" and selected() == original_selected
    for item in variants["objects"]:
        duplicate = bpy.data.objects[item["name"]]
        world = source_world if item["source"] == source.name else parent_world
        expected = Matrix.Translation((5 * item["variant"], 0, 0)) @ world
        for row, expected_row in zip(duplicate.matrix_world, expected):
            assert_vector(row, expected_row)
        if item["source"] == source.name:
            assert duplicate.data != source.data
            assert duplicate.parent is not None and duplicate.parent["mcp_source"] == parent.name
    assert mesh_snapshot(source) == original

    linked = run_recipe("create_variants", [source.name], {"count": 1, "linked_data": True, "axis": "Z"})
    assert bpy.data.objects[linked["objects"][0]["name"]].data == source.data
    assert mesh_snapshot(source) == original
    bpy.ops.object.mode_set(mode="OBJECT")
    return {"safe_mode_scripts": len(RECIPE_NAMES), "recipes": list(RECIPE_NAMES),
            "prepared_triangles": game["objects"][0]["triangles"],
            "smart_and_planar_uv": True, "negative_scale": True,
            "edit_mode_preserved": True, "camera_framing": True,
            "variant_parenting": True, "originals_preserved": True}


def checkpoint_checks(directory):
    server = recovery_server(directory / "recovery")
    project = directory / "original.blend"
    assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=str(project), check_existing=False)
    original_bytes = hashlib.sha256(project.read_bytes()).hexdigest()
    source = bpy.data.objects["SmokeAsset"]
    select_only(source)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(source.data)
    bm.verts.ensure_lookup_table()
    bm.verts[0].co.x += 0.321
    expected_vertex = tuple(bm.verts[0].co)
    bmesh.update_edit_mesh(source.data)
    before_mode, before_selection = source.mode, selected()
    checkpoint = server.create_checkpoint("Real Blender edit mode")
    assert source.mode == before_mode and selected() == before_selection
    assert bpy.data.filepath == str(project)
    assert hashlib.sha256(project.read_bytes()).hexdigest() == original_bytes
    checkpoint_id = checkpoint["checkpoint_id"]
    assert checkpoint["bytes"] > 0
    bpy.ops.object.mode_set(mode="OBJECT")
    original_location = tuple(source.location)
    source.location.x += 100
    source.data.vertices[0].co.x += 30
    bpy.data.objects.remove(bpy.data.objects["Cube"], do_unlink=True)
    added = bpy.data.objects.new("AfterCheckpoint", None)
    bpy.context.scene.collection.objects.link(added)
    bpy.context.view_layer.update()
    restoration = server.restore_checkpoint(checkpoint_id)
    assert restoration["restored"] is True
    assert bpy.data.objects.get("Cube") is not None
    assert bpy.data.objects.get("AfterCheckpoint") is None
    restored = bpy.data.objects["SmokeAsset"]
    assert_vector(restored.location, original_location)
    assert_vector(restored.data.vertices[0].co, expected_vertex)
    assert bpy.data.filepath == restoration["restored_filepath"]
    assert hashlib.sha256(project.read_bytes()).hexdigest() == original_bytes
    safety_id = restoration["safety_checkpoint_id"]
    assert any(entry["checkpoint_id"] == safety_id for entry in server.list_checkpoints()["checkpoints"])
    server.restore_checkpoint(safety_id)
    assert bpy.data.objects.get("Cube") is None
    assert bpy.data.objects.get("AfterCheckpoint") is not None
    assert_vector(bpy.data.objects["SmokeAsset"].location, (original_location[0] + 100, *original_location[1:]))
    assert hashlib.sha256(project.read_bytes()).hexdigest() == original_bytes
    success = server.execute_code("bpy.context.scene['smoke_change'] = True", checkpoint=True)
    assert success["checkpoint_id"]
    try:
        server.execute_code("bpy.context.scene['partial_smoke_change'] = True\nraise ValueError('expected failure')", checkpoint=True)
    except Exception as exc:
        failure = json.loads(str(exc))
        assert failure["message"] == "expected failure" and failure["checkpoint_id"]
        server.restore_checkpoint(failure["checkpoint_id"])
        assert "partial_smoke_change" not in bpy.context.scene
    else:
        raise AssertionError("execute_code failure was not propagated")
    return {"checkpoint_saved_in_edit_mode": True, "edit_geometry_restored": True,
            "deleted_object_restored": True, "original_file_untouched": True,
            "safety_checkpoint_restorable": True, "failed_code_recoverable": True}


def main():
    assert bpy.app.background, "Run this test in a separate background Blender process"
    assert not bpy.data.filepath, "Pass --factory-startup; this test must not open a user file"
    assert set(bpy.context.scene.objects.keys()) == {"Camera", "Cube", "Light"}, "Expected factory-startup scene"
    with tempfile.TemporaryDirectory(prefix="blendermcp-smoke-") as temporary:
        summary = {"blender": bpy.app.version_string}
        summary.update(recipe_checks())
        summary.update(checkpoint_checks(Path(temporary)))
        print("__BLENDER_SMOKE__" + json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
