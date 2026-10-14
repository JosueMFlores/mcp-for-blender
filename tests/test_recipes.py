"""Recipe validation and executable behavior without importing Blender."""

from __future__ import annotations

import ast
import contextlib
import copy
import io
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blender_mcp.blender_scripts import parse_result
from blender_mcp.recipes import RECIPE_NAMES, build_recipe, describe_recipe, list_recipes
from blender_mcp.safe_mode import validate_code


def test_catalog_is_complete_and_does_not_expose_mutable_defaults():
    assert tuple(item["name"] for item in list_recipes()) == RECIPE_NAMES
    first = describe_recipe("create_variants")
    first["options"]["count"]["default"] = 999
    assert describe_recipe("create_variants")["options"]["count"]["default"] == 3


@pytest.mark.parametrize("recipe", RECIPE_NAMES)
def test_generated_script_compiles_and_passes_safe_mode(recipe):
    script = build_recipe(recipe, ["Cube"])
    compile(script, "<recipe>", "exec")
    validate_code(script)


def test_arguments_are_encoded_as_data():
    malicious_name = "Cube'); import os; print('injected\n"
    script = build_recipe("create_variants", [malicious_name], {"collection_name": malicious_name})
    validate_code(script)
    tree = ast.parse(script)
    args_literal = tree.body[1].value.args[0].value
    import json
    args = json.loads(args_literal)
    assert args["object_names"] == [malicious_name]
    assert args["options"]["collection_name"] == malicious_name


@pytest.mark.parametrize("names", [None, "Cube", [], [""], ["  "], ["Cube", "Cube"], [2],
                                  ["bad\x00name"], ["x" * 256], ["Cube"] * 101])
def test_invalid_names_are_rejected(names):
    with pytest.raises(ValueError):
        build_recipe("create_variants", names)


@pytest.mark.parametrize("recipe, options", [
    ("create_variants", {"unknown": True}),
    ("create_variants", {"count": True}),
    ("create_variants", {"count": 1.5}),
    ("create_variants", {"count": 0}),
    ("create_variants", {"count": 33}),
    ("create_variants", {"spacing": 0}),
    ("create_variants", {"spacing": float("nan")}),
    ("create_variants", {"spacing": float("inf")}),
    ("create_variants", {"spacing": 10 ** 1000}),
    ("create_variants", {"axis": "x"}),
    ("create_variants", {"axis": ["X"]}),
    ("create_variants", {"linked_data": 1}),
    ("prepare_game_asset", {"triangle_budget": 0}),
    ("prepare_game_asset", {"triangle_budget": 1_000_001}),
    ("prepare_game_asset", {"triangle_budget": 20.0}),
    ("prepare_game_asset", {"triangle_budget": False}),
    ("prepare_game_asset", {"uv_method": "unwrap"}),
    ("prepare_game_asset", {"triangulate": "false"}),
    ("product_lighting", {"camera_lens": -50}),
    ("product_lighting", {"camera_lens": 501}),
    ("product_lighting", {"margin": 1}),
    ("product_lighting", {"light_energy": float("-inf")}),
    ("product_lighting", {"set_scene_camera": 1}),
    ("product_lighting", {"collection_name": ""}),
])
def test_invalid_options_are_rejected(recipe, options):
    with pytest.raises(ValueError):
        build_recipe(recipe, ["Cube"], options)


@pytest.mark.parametrize("options", [False, [], "{}", 1])
def test_options_must_be_a_mapping(options):
    with pytest.raises(ValueError):
        build_recipe("create_variants", ["Cube"], options)


def test_unknown_recipe_is_rejected():
    with pytest.raises(ValueError, match="Unknown recipe"):
        build_recipe("delete_scene", ["Cube"])


class Vector(list):
    def copy(self):
        return Vector(self)

    def __add__(self, other):
        return Vector(a + b for a, b in zip(self, other))

    def __sub__(self, other):
        return Vector(a - b for a, b in zip(self, other))

    def __mul__(self, scalar):
        return Vector(value * scalar for value in self)

    def __truediv__(self, scalar):
        return self * (1 / scalar)

    @property
    def length(self):
        return math.sqrt(sum(value * value for value in self))

    def normalized(self):
        return self / self.length

    def to_track_quat(self, *args):
        return SimpleNamespace(to_euler=lambda: (0, 0, 0))


class Matrix:
    """Diagonal affine math sufficient to exercise scale, bounds, and parenting."""
    def __init__(self, translation=(0, 0, 0), scale=(1, 1, 1)):
        self.translation = Vector(translation)
        self.scale = Vector(scale)

    @staticmethod
    def Translation(translation):
        return Matrix(translation)

    def copy(self):
        return Matrix(self.translation, self.scale)

    def __matmul__(self, other):
        if isinstance(other, Matrix):
            return Matrix(self @ other.translation, [a * b for a, b in zip(self.scale, other.scale)])
        return Vector(a * b + c for a, b, c in zip(self.scale, other, self.translation))

    def inverted(self):
        return Matrix([-a / b for a, b in zip(self.translation, self.scale)], [1 / s for s in self.scale])

    def to_3x3(self):
        return Matrix(scale=self.scale)

    def to_4x4(self):
        return self.copy()

    def determinant(self):
        return math.prod(self.scale)


class UVLayers(list):
    def __init__(self, mesh):
        super().__init__()
        self.mesh = mesh

    def new(self, name):
        layer = SimpleNamespace(name=name, data=[SimpleNamespace(uv=None) for _ in self.mesh.loops])
        self.append(layer)
        return layer


class Mesh:
    def __init__(self):
        self.vertices = [SimpleNamespace(co=Vector(co)) for co in [(0, 0, 0), (2, 0, 0), (0, 1, 0)]]
        self.polygons = [(0, 1, 2)]
        self.loops = [SimpleNamespace(index=index, vertex_index=index) for index in range(3)]
        self.uv_layers = UVLayers(self)
        self.loop_triangles = []

    def copy(self):
        return copy.deepcopy(self)

    def transform(self, matrix):
        for vertex in self.vertices:
            vertex.co = matrix @ vertex.co

    def update(self):
        pass

    def calc_loop_triangles(self):
        self.loop_triangles = [None] * sum(len(face) - 2 for face in self.polygons)


class Object(dict):
    def __init__(self, name, data=None, *, matrix=None):
        super().__init__()
        self.name = name
        self.data = data
        self.type = "MESH" if isinstance(data, Mesh) else (getattr(data, "kind", "EMPTY"))
        self.parent = None
        self.local_matrix = matrix or Matrix()
        self.constraints = []
        self.modifiers = []
        self.animation_data = None
        self.mode = "OBJECT"
        self.selected = False
        self.hidden = False
        self.rotation_euler = (0, 0, 0)

    # Blender ID objects compare by identity, unlike plain dicts.
    def __eq__(self, other):
        return self is other

    def __bool__(self):
        return True

    @property
    def matrix_world(self):
        return self.parent.matrix_world @ self.local_matrix if self.parent is not None else self.local_matrix

    @matrix_world.setter
    def matrix_world(self, matrix):
        self.local_matrix = self.parent.matrix_world.inverted() @ matrix if self.parent is not None else matrix

    @property
    def location(self):
        return self.local_matrix.translation

    @location.setter
    def location(self, value):
        self.local_matrix.translation = Vector(value)

    @property
    def bound_box(self):
        low = [min(vertex.co[index] for vertex in self.data.vertices) for index in range(3)]
        high = [max(vertex.co[index] for vertex in self.data.vertices) for index in range(3)]
        return [(x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]

    def copy(self):
        obj = Object(self.name, self.data, matrix=self.local_matrix.copy())
        obj.parent = self.parent
        obj.constraints = list(self.constraints)
        obj.modifiers = list(self.modifiers)
        obj.animation_data = self.animation_data
        return obj

    def animation_data_clear(self):
        self.animation_data = None

    def evaluated_get(self, depsgraph):
        return self

    def select_set(self, value):
        self.selected = value

    def hide_set(self, value):
        self.hidden = value


class Objects:
    def __init__(self):
        self.items = {}
        self.active = None

    def get(self, name):
        return self.items.get(name)

    def __iter__(self):
        return iter(self.items.values())

    def link(self, obj):
        prefix = obj.name
        index = 0
        while obj.name in self.items:
            index += 1
            obj.name = f"{prefix}.{index:03}"
        self.items[obj.name] = obj

    def new(self, name, object_data):
        return Object(name, object_data)


class Collections(list):
    def __init__(self, objects):
        super().__init__()
        self.objects = objects

    def new(self, name):
        existing = {coll.name for coll in self}
        prefix, index = name, 0
        while name in existing:
            index += 1
            name = f"{prefix}.{index:03}"
        coll = Collection(name, self.objects)
        self.append(coll)
        return coll


class Collection(dict):
    def __init__(self, name, objects):
        super().__init__()
        self.name = name
        self.objects = SimpleNamespace(link=objects.link)


class Blender:
    def __init__(self, monkeypatch, sources):
        objects = Objects()
        for obj in sources:
            objects.link(obj)
        self.collections = Collections(objects)
        self.scene = SimpleNamespace(
            objects=objects, camera=Object("OldCamera"),
            collection=SimpleNamespace(children=SimpleNamespace(link=lambda coll: None)),
            render=SimpleNamespace(resolution_x=1920, resolution_y=1080, pixel_aspect_x=1, pixel_aspect_y=1),
        )
        self.view_layer = SimpleNamespace(objects=objects, update=lambda: None)
        self.context = SimpleNamespace(scene=self.scene, view_layer=self.view_layer,
                                       selected_objects=[obj for obj in sources if obj.selected],
                                       evaluated_depsgraph_get=lambda: None)
        self.objects = objects
        self.mode_calls = []
        self.fail_uv = False

        def mode_set(mode):
            self.mode_calls.append(mode)
            self.objects.active.mode = mode

        def smart_project(**kwargs):
            if self.fail_uv:
                raise RuntimeError("UV failure")
            self.objects.active.data.uv_layers.new("UVMap")

        bpy = SimpleNamespace(
            context=self.context,
            data=SimpleNamespace(
                collections=self.collections, objects=objects,
                meshes=SimpleNamespace(new_from_object=lambda obj, **kwargs: obj.data.copy()),
                cameras=SimpleNamespace(new=lambda name: SimpleNamespace(kind="CAMERA", name=name)),
                lights=SimpleNamespace(new=lambda name, type: SimpleNamespace(kind="LIGHT", name=name)),
            ),
            ops=SimpleNamespace(
                object=SimpleNamespace(mode_set=mode_set),
                mesh=SimpleNamespace(select_all=lambda **kwargs: None),
                uv=SimpleNamespace(smart_project=smart_project),
            ),
        )
        monkeypatch.setitem(sys.modules, "bpy", bpy)
        monkeypatch.setitem(sys.modules, "mathutils", SimpleNamespace(Vector=Vector, Matrix=Matrix))
        monkeypatch.setitem(sys.modules, "bmesh", SimpleNamespace())

    def run(self, recipe, names, options=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(build_recipe(recipe, names, options), {})
        return parse_result(output.getvalue())


@pytest.mark.parametrize("recipe", RECIPE_NAMES)
def test_missing_targets_do_not_create_partial_results(monkeypatch, recipe):
    blender = Blender(monkeypatch, [])
    result = blender.run(recipe, ["missing"])
    assert "Missing objects" in result["error"]
    assert not blender.collections
    assert not list(blender.objects)


def test_game_preparation_rejects_non_mesh_before_mutation(monkeypatch):
    blender = Blender(monkeypatch, [Object("Empty")])
    result = blender.run("prepare_game_asset", ["Empty"])
    assert "mesh objects" in result["error"]
    assert not blender.collections


def test_game_preparation_bakes_scale_on_independent_mesh_and_adds_uv(monkeypatch):
    source = Object("Cube", Mesh(), matrix=Matrix((7, 8, 9), (2, 3, 4)))
    source.selected = True
    source.mode = "EDIT"
    source_coords = [list(vertex.co) for vertex in source.data.vertices]
    blender = Blender(monkeypatch, [source])
    blender.objects.active = source
    result = blender.run("prepare_game_asset", ["Cube"], {"triangulate": False, "uv_method": "planar"})
    assert "error" not in result
    item = result["objects"][0]
    prepared = blender.objects.get(item["name"])
    assert prepared.data is not source.data
    assert prepared.matrix_world.scale == [1, 1, 1]
    assert item["world_bounding_box"] == [[7, 8, 9], [11, 11, 9]]
    assert item["triangles"] == 1
    assert item["uv_created"] is True
    assert [vertex.co for vertex in source.data.vertices] == source_coords
    assert not source.data.uv_layers
    assert source.selected and source.mode == "EDIT"
    assert not prepared.selected
    assert blender.objects.active is source


def test_context_is_restored_when_a_recipe_fails_after_selection_changes(monkeypatch):
    source = Object("Cube", Mesh())
    source.selected = True
    source.mode = "EDIT"
    blender = Blender(monkeypatch, [source])
    blender.objects.active = source
    blender.fail_uv = True
    result = blender.run("prepare_game_asset", ["Cube"], {"triangulate": False})
    assert result["error"] == "UV failure"
    assert source.selected and source.mode == "EDIT"
    assert blender.objects.active is source
    assert [obj.name for obj in blender.objects if obj.selected] == ["Cube"]


@pytest.mark.parametrize("linked_data", [False, True])
def test_variants_preserve_originals_and_remap_an_unordered_parent_hierarchy(monkeypatch, linked_data):
    parent = Object("Parent", Mesh(), matrix=Matrix((4, 0, 0), (2, 2, 2)))
    child = Object("Child", Mesh(), matrix=Matrix((1, 0, 0)))
    child.parent = parent
    parent.selected, child.selected = True, True
    blender = Blender(monkeypatch, [parent, child])
    blender.objects.active = child
    result = blender.run("create_variants", ["Child", "Parent"],
                         {"count": 2, "spacing": 5, "linked_data": linked_data})
    assert "error" not in result
    assert len(result["objects"]) == 4
    for item in result["objects"]:
        clone = blender.objects.get(item["name"])
        source = parent if item["source"] == "Parent" else child
        assert (clone.data is source.data) is linked_data
        assert clone.matrix_world.translation == [source.matrix_world.translation[0] + 5 * item["variant"], 0, 0]
        if source is child:
            assert clone.parent is blender.objects.get(f"Parent_Variant_{item['variant']:02}")
    assert parent.matrix_world.translation == [4, 0, 0]
    assert child.matrix_world.translation == [6, 0, 0]
    assert child.parent is parent
    assert child.selected and parent.selected
    assert blender.objects.active is child


def test_lighting_can_leave_scene_camera_and_targets_unchanged(monkeypatch):
    source = Object("Cube", Mesh(), matrix=Matrix((3, 4, 5)))
    source.selected = True
    blender = Blender(monkeypatch, [source])
    blender.objects.active = source
    old_camera = blender.scene.camera
    result = blender.run("product_lighting", ["Cube"], {"set_scene_camera": False})
    assert "error" not in result
    assert blender.scene.camera is old_camera
    assert result["world_bounding_box"] == [[3, 4, 5], [5, 5, 5]]
    assert len(result["objects"]) == 4
    assert source.matrix_world.translation == [3, 4, 5]
    assert source.selected and blender.objects.active is source
    assert sum(item["type"] == "LIGHT" for item in result["objects"]) == 3


def test_repeated_lighting_creates_unique_collections_and_names(monkeypatch):
    source = Object("Cube", Mesh())
    blender = Blender(monkeypatch, [source])
    first = blender.run("product_lighting", ["Cube"])
    second = blender.run("product_lighting", ["Cube"])
    assert first["collection"] != second["collection"]
    assert first["camera"] != second["camera"]
    assert blender.scene.camera.name == second["camera"]


def test_non_finite_target_bounds_fail_before_creating_lighting(monkeypatch):
    source = Object("Bad", matrix=Matrix((float("nan"), 0, 0)))
    blender = Blender(monkeypatch, [source])
    result = blender.run("product_lighting", ["Bad"])
    assert "non-finite" in result["error"]
    assert not blender.collections
