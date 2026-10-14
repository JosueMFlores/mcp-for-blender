"""export_scene: write the whole scene, the current selection, or named objects to GLB/FBX."""
import asyncio
import importlib.util
import json
import os
import sys
import types

from conftest import ROOT_ADDON as ADDON


class _Matrix:
    """matrix_world with only what decompose() reports: rotation w and scale."""

    def __init__(self, w=1.0, scale=(1.0, 1.0, 1.0)):
        self.w, self.scale = w, scale

    def decompose(self):
        return (0.0, 0.0, 0.0), types.SimpleNamespace(w=self.w), self.scale


def _mesh(triangles=12, uv=True, vertices=()):
    return types.SimpleNamespace(loops=[None] * (3 * triangles), polygons=[None] * triangles,
                                 uv_layers=["UVMap"] if uv else [], vertices=list(vertices))


def _armature_data(bones):
    return types.SimpleNamespace(bones=[types.SimpleNamespace(name=b) for b in bones])


class _Object:
    def __init__(self, name, children=(), type="MESH", data=None, matrix=None, mode="OBJECT", materials=(),
                 modifiers=(), vertex_groups=(), action=None, selectable=True, in_view_layer=True):
        self.name = name
        self.children_recursive = list(children)
        self.selected = False
        self.type = type
        self.data = data if data is not None else (_mesh() if type == "MESH" else None)
        self.matrix_world = matrix or _Matrix()
        self.mode = mode
        self.material_slots = [types.SimpleNamespace(material=m) for m in materials]
        self.modifiers = list(modifiers)
        self.vertex_groups = list(vertex_groups)
        self.animation_data = types.SimpleNamespace(action=action) if action else None
        self.selectable = selectable
        self.in_view_layer = in_view_layer

    def select_set(self, value):
        if not self.in_view_layer:
            raise RuntimeError(f"Object '{self.name}' can't be selected because it is not in View Layer 'ViewLayer'!")
        # Like Blender, hidden objects silently stay unselected.
        if self.selectable or not value:
            self.selected = value

    def select_get(self):
        return self.selected


class _Objects:
    def __init__(self, *objects):
        self._all = list(objects)
        self._by_name = {o.name: o for o in objects}

    def get(self, name):
        return self._by_name.get(name)

    def __getitem__(self, name):
        return self._by_name[name]

    def __iter__(self):
        return iter(self._all)


class _LayerObjects:
    """view_layer.objects: the scene's objects minus those in excluded collections."""

    def __init__(self, objects, active):
        self._objects, self._active = objects, active

    def __iter__(self):
        return (o for o in self._objects if o.in_view_layer)

    @property
    def active(self):
        return self._active

    @active.setter
    def active(self, obj):
        if obj is not None and not obj.in_view_layer:
            raise ValueError(f"ViewLayer 'ViewLayer' does not contain object '{obj.name}'")
        self._active = obj


def _load_addon(monkeypatch, objects, selected, exports, active=None, blend="", mode_calls=None):
    scene = types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_hyper3d=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_polypizza=False,
        blendermcp_use_hunyuan3d=False,
        objects=objects,
    )
    bpy = types.ModuleType("bpy")
    bpy.context = types.SimpleNamespace(
        scene=scene,
        object=None,
        selected_objects=selected,
        view_layer=types.SimpleNamespace(objects=_LayerObjects(objects, active)),
    )
    bpy.data = types.SimpleNamespace(objects=objects, filepath=blend)
    bpy.path = types.SimpleNamespace(abspath=lambda p: os.path.join(os.path.dirname(blend), p[2:]))
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )

    def export(kind):
        def _op(filepath, **kwargs):
            if kind == "gltf" and os.path.splitext(filepath)[1].lower() not in (".glb", ".gltf"):
                filepath += ".glb"  # as Blender's glTF exporter does
            selection = [o.name for o in objects if o.selected]
            exports.append({"kind": kind, "filepath": filepath, "selected": selection, **kwargs})
            with open(filepath, "wb") as fh:
                fh.write(b"EXPORTED")
        return _op

    def select_all(action):
        if action == "DESELECT":
            for o in bpy.context.view_layer.objects:
                o.select_set(False)

    def mode_set(mode):
        (mode_calls if mode_calls is not None else []).append(mode)
        if bpy.context.view_layer.objects.active is not None:
            bpy.context.view_layer.objects.active.mode = mode

    bpy.ops = types.SimpleNamespace(
        object=types.SimpleNamespace(select_all=select_all, mode_set=mode_set),
        export_scene=types.SimpleNamespace(gltf=export("gltf"), fbx=export("fbx")),
    )

    props = types.ModuleType("bpy.props")
    for name in ("BoolProperty", "EnumProperty", "FloatProperty", "IntProperty", "StringProperty"):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []

    app = types.ModuleType("bpy.app")
    app.version = (4, 2, 0)
    app.version_string = "4.2.0"
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    monkeypatch.setitem(sys.modules, "mathutils", types.ModuleType("mathutils"))

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    spec = importlib.util.spec_from_file_location("blender_mcp_addon_test", ADDON)
    addon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(addon)
    return addon


def test_named_objects_export_with_their_children_as_glb(monkeypatch, tmp_path):
    leg = _Object("Leg")
    chair = _Object("Chair", children=[leg])
    lamp = _Object("Lamp")
    exports = []
    addon = _load_addon(monkeypatch, _Objects(chair, leg, lamp), [], exports)
    out = str(tmp_path / "chair.glb")

    result = addon.BlenderMCPServer().export_scene(out, object_names=["Chair"])

    assert result == {"path": out, "bytes": len(b"EXPORTED"), "selection_only": True, "exported": ["Chair", "Leg"]}
    assert exports[0]["selected"] == ["Chair", "Leg"]
    assert not (chair.selected or leg.selected or lamp.selected), "the user's (empty) selection is back"
    assert exports[0]["kind"] == "gltf"
    assert exports[0]["use_selection"] is True
    assert exports[0]["use_active_scene"] is True
    assert exports[0]["export_apply"] is True
    assert exports[0]["export_format"] == "GLB"


def test_selection_only_exports_the_current_selection_as_fbx(monkeypatch, tmp_path):
    cube = _Object("Cube")
    exports = []
    addon = _load_addon(monkeypatch, _Objects(cube, _Object("Other")), [cube], exports)
    out = str(tmp_path / "sel.fbx")

    result = addon.BlenderMCPServer().export_scene(out, format="fbx", selection_only=True, apply_modifiers=False)

    assert result["exported"] == ["Cube"]
    assert result["selection_only"] is True
    assert exports[0]["kind"] == "fbx"
    assert exports[0]["use_selection"] is True
    assert exports[0]["use_mesh_modifiers"] is False
    assert exports[0]["bake_space_transform"] is False


def test_whole_scene_when_nothing_is_named_or_selected(monkeypatch, tmp_path):
    objs = _Objects(_Object("A"), _Object("B"))
    exports = []
    addon = _load_addon(monkeypatch, objs, [], exports)
    out = str(tmp_path / "nested" / "scene.glb")

    result = addon.BlenderMCPServer().export_scene(out)

    assert result["selection_only"] is False
    assert result["exported"] == ["A", "B"]
    assert exports[0]["use_selection"] is False
    assert os.path.isdir(str(tmp_path / "nested")), "parent folders are created"


def test_missing_objects_and_bad_input_are_rejected_before_exporting(monkeypatch, tmp_path):
    exports = []
    addon = _load_addon(monkeypatch, _Objects(_Object("A")), [], exports)
    server = addon.BlenderMCPServer()
    out = str(tmp_path / "x.glb")

    assert "not found" in server.export_scene(out, object_names=["A", "Ghost"])["error"]
    assert "glb" in server.export_scene(out, format="obj")["error"]
    assert "filepath" in server.export_scene("")["error"]
    assert "Nothing is selected" in server.export_scene(out, selection_only=True)["error"]
    assert exports == []


def test_command_is_always_routed(monkeypatch, tmp_path):
    exports = []
    addon = _load_addon(monkeypatch, _Objects(_Object("A")), [], exports)
    out = str(tmp_path / "scene.glb")

    command = addon.BlenderMCPServer()._execute_command_internal({"type": "export_scene", "params": {"filepath": out}})

    assert command["status"] == "success"
    assert command["result"]["exported"] == ["A"]


R15 = ["HumanoidRootNode", "LowerTorso", "UpperTorso", "Head",
       "LeftUpperArm", "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand",
       "LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "RightUpperLeg", "RightLowerLeg", "RightFoot"]


def _image(name, size=(1024, 1024), filepath="//textures/x.png"):
    return types.SimpleNamespace(name=name, size=size, filepath=filepath)


def _material(name, image=None, base_color_from=None):
    nodes = []
    if image is not None:
        nodes.append(types.SimpleNamespace(type="TEX_IMAGE", image=image))
    link = [types.SimpleNamespace(from_node=types.SimpleNamespace(type=base_color_from))] if base_color_from else []
    nodes.append(types.SimpleNamespace(type="BSDF_PRINCIPLED", inputs={
        "Base Color": types.SimpleNamespace(is_linked=bool(link), links=link)}))
    return types.SimpleNamespace(name=name, node_tree=types.SimpleNamespace(nodes=nodes))


def test_roblox_preset_forces_fbx_with_studio_settings(monkeypatch, tmp_path):
    rig = _Object("Root", type="ARMATURE", data=_armature_data(R15))
    body = _Object("Head_Geo", materials=[_material("Skin", _image("skin.png", size=(2048, 2048)), "TEX_IMAGE")])
    character = _Object("Character", type="EMPTY", data=None, children=[rig, body])
    camera = _Object("Camera", type="CAMERA")
    exports = []
    addon = _load_addon(monkeypatch, _Objects(character, rig, body, camera), [], exports)

    result = addon.BlenderMCPServer().export_scene(str(tmp_path / "char.glb"), object_names=["Character"],
                                                   preset="Roblox")

    assert result["path"] == str(tmp_path / "char.fbx"), "the preset's format decides the extension"
    assert result["preset"] == "roblox"
    assert result["warnings"] == [], "2048x2048 is within even the avatar Marketplace limit"
    assert result["exported"] == ["Character", "Root", "Head_Geo"], "cameras and lights aren't exported"
    kwargs = exports[0]
    assert kwargs["kind"] == "fbx"
    assert kwargs["global_scale"] == 0.01 and kwargs["apply_unit_scale"] is False
    assert (kwargs["axis_forward"], kwargs["axis_up"]) == ("-Z", "Y")
    assert kwargs["object_types"] == {"EMPTY", "ARMATURE", "MESH"}
    assert kwargs["add_leaf_bones"] is False and kwargs["bake_space_transform"] is False
    assert kwargs["path_mode"] == "COPY" and kwargs["embed_textures"] is True
    assert kwargs["use_custom_props"] is True and kwargs["use_mesh_modifiers"] is True
    assert kwargs["bake_anim"] is False, "nothing is animated"
    assert not kwargs["bake_anim_use_nla_strips"] and not kwargs["bake_anim_use_all_actions"]
    assert not kwargs["bake_anim_force_startend_keying"] and kwargs["bake_anim_simplify_factor"] == 0.0


def test_roblox_preset_bakes_animation_only_when_there_is_an_action(monkeypatch, tmp_path):
    rig = _Object("Root", type="ARMATURE", data=_armature_data(R15), action=object())
    exports = []
    addon = _load_addon(monkeypatch, _Objects(rig), [], exports)

    addon.BlenderMCPServer().export_scene(str(tmp_path / "walk.fbx"), preset="roblox")

    assert exports[0]["bake_anim"] is True


def test_roblox_warnings_name_what_studio_would_reject(monkeypatch, tmp_path):
    rig = _Object("Rig", type="ARMATURE", data=_armature_data(["HumanoidRootNode", "LowerTorso", "Spine"]))
    groups = [types.SimpleNamespace(index=i, name=b) for i, b in enumerate(R15[:6])]
    five_bones = types.SimpleNamespace(groups=[types.SimpleNamespace(group=i, weight=0.2) for i in range(5)])
    heavy_rig = _Object("BodyRig", type="ARMATURE", data=_armature_data(R15))
    skinned = _Object("Body", data=_mesh(vertices=[five_bones]), vertex_groups=groups,
                      modifiers=[types.SimpleNamespace(type="ARMATURE", object=heavy_rig)])
    dense = _Object("Statue", data=_mesh(triangles=20001))
    scaled = _Object("Crate", matrix=_Matrix(scale=(2.0, 2.0, 2.0)),
                     materials=[_material("Wood", _image("wood.png", size=(4096, 4096)))])
    flat = _Object("Sign", data=_mesh(uv=False),
                   materials=[_material("Paint", _image("Generated", filepath=""), "MIX")])
    attachment = _Object("Hat_Att", data=_mesh(triangles=0, uv=False), matrix=_Matrix(w=0.7))
    hidden = _Object("Cage", selectable=False)
    top = _Object("Character", type="EMPTY", data=None,
                  children=[rig, heavy_rig, skinned, dense, scaled, flat, attachment, hidden])
    exports = []
    addon = _load_addon(monkeypatch, _Objects(top, rig, heavy_rig, skinned, dense, scaled, flat, attachment,
                                              hidden), [], exports)

    result = addon.BlenderMCPServer().export_scene(str(tmp_path / "c.fbx"), object_names=["Character"],
                                                   preset="roblox")
    text = "\n".join(result["warnings"])

    assert "'Statue' has 20,001 triangles" in text and "20,000" in text
    assert "'Body' has 1 vertices weighted to more than 4 bones" in text
    assert "'Rig' is missing R15 bones" in text and "UpperTorso" in text
    assert "rotation or scale on Crate" in text
    assert "No UV map on Sign" in text and "Hat_Att" not in text
    assert "'wood.png' is 4096x4096" in text and "can't exceed 2048x2048" in text
    assert "general max is 4096x4096" in text
    assert "Base Color isn't an Image Texture plugged straight in on Paint" in text
    assert "no file on disk: Generated" in text
    assert "hidden or unselectable: Cage" in text and "Cage" not in result["exported"]


def test_unknown_preset_lists_the_valid_ones(monkeypatch, tmp_path):
    exports = []
    addon = _load_addon(monkeypatch, _Objects(_Object("A")), [], exports)

    result = addon.BlenderMCPServer().export_scene(str(tmp_path / "a.fbx"), preset="unreal")

    assert "unreal" in result["error"] and "roblox" in result["error"]
    assert exports == []


def test_paths_are_absolute_or_relative_to_the_blend(monkeypatch, tmp_path):
    exports = []
    addon = _load_addon(monkeypatch, _Objects(_Object("A")), [], exports, blend=str(tmp_path / "proj.blend"))
    server = addon.BlenderMCPServer()

    assert "absolute" in server.export_scene("out/a.glb")["error"]
    result = server.export_scene("//export/a.glb")
    assert result["path"] == str(tmp_path / "export" / "a.glb")

    unsaved = _load_addon(monkeypatch, _Objects(_Object("A")), [], [])
    assert "isn't saved" in unsaved.BlenderMCPServer().export_scene("//a.glb")["error"]


def test_selection_active_object_and_mode_are_put_back(monkeypatch, tmp_path):
    chair = _Object("Chair")
    lamp = _Object("Lamp", mode="EDIT")
    lamp.selected = True
    exports, modes = [], []
    addon = _load_addon(monkeypatch, _Objects(chair, lamp), [lamp], exports, active=lamp, mode_calls=modes)

    addon.BlenderMCPServer().export_scene(str(tmp_path / "chair.fbx"), object_names=["Chair"], preset="roblox")

    assert exports[0]["selected"] == ["Chair"]
    assert lamp.selected and not chair.selected
    assert addon.bpy.context.view_layer.objects.active is lamp
    assert modes == ["OBJECT", "EDIT"], "left edit mode to export, then went back"


def test_selection_is_put_back_when_the_export_fails(monkeypatch, tmp_path):
    chair = _Object("Chair")
    lamp = _Object("Lamp")
    lamp.selected = True
    addon = _load_addon(monkeypatch, _Objects(chair, lamp), [lamp], [], active=lamp)

    def broken(**_kwargs):
        raise RuntimeError("disk full")

    addon.bpy.ops.export_scene.gltf = broken
    try:
        addon.BlenderMCPServer().export_scene(str(tmp_path / "chair.glb"), object_names=["Chair"])
    except RuntimeError:
        pass
    assert lamp.selected and not chair.selected
    assert addon.bpy.context.view_layer.objects.active is lamp


def test_texture_over_the_general_max_says_so(monkeypatch, tmp_path):
    poster = _Object("Poster", materials=[_material("Print", _image("poster.png", size=(8192, 4096)), "TEX_IMAGE")])
    exports = []
    addon = _load_addon(monkeypatch, _Objects(poster), [], exports)

    result = addon.BlenderMCPServer().export_scene(str(tmp_path / "p.fbx"), preset="roblox")

    assert result["warnings"] == ["Image 'poster.png' is 8192x4096. Avatar Marketplace items (bodies, rigid "
                                  "accessories) can't exceed 2048x2048; it's also over Roblox's general max of "
                                  "4096x4096."]


def test_a_dot_in_the_name_is_not_taken_for_an_extension(monkeypatch, tmp_path):
    exports = []
    addon = _load_addon(monkeypatch, _Objects(_Object("A")), [], exports)
    server = addon.BlenderMCPServer()

    result = server.export_scene(str(tmp_path / "Chair.001"))
    assert result["path"] == str(tmp_path / "Chair.001.glb")
    assert exports[-1]["filepath"] == result["path"], "the glTF exporter had nothing to append"
    assert server.export_scene(str(tmp_path / "sword_v1.5"), format="fbx")["path"] == str(tmp_path / "sword_v1.5.fbx")
    assert server.export_scene(str(tmp_path / "a.gltf"))["path"] == str(tmp_path / "a.glb")
    assert server.export_scene(str(tmp_path / "b.glb"), format="fbx")["path"] == str(tmp_path / "b.fbx")
    assert server.export_scene(str(tmp_path / "c.GLB"))["path"] == str(tmp_path / "c.GLB")


def test_objects_excluded_from_the_view_layer_are_left_out_and_named(monkeypatch, tmp_path):
    leg = _Object("Leg", in_view_layer=False)
    chair = _Object("Chair", children=[leg])
    ref = _Object("Reference", in_view_layer=False)
    lamp = _Object("Lamp")
    lamp.selected = True
    exports = []
    addon = _load_addon(monkeypatch, _Objects(chair, leg, ref, lamp), [lamp], exports, active=lamp)
    server = addon.BlenderMCPServer()

    # The first named object is excluded, so another one becomes active instead.
    result = server.export_scene(str(tmp_path / "chair.glb"), object_names=["Reference", "Chair"])

    assert result["exported"] == ["Chair"]
    assert exports[-1]["selected"] == ["Chair"]
    assert result["warnings"] == ["Left out because their collection is excluded from the view layer: Reference, "
                                  "Leg. Include it (its checkbox in the Outliner) to export them."]
    assert lamp.selected and not chair.selected
    assert addon.bpy.context.view_layer.objects.active is lamp

    whole = server.export_scene(str(tmp_path / "scene.glb"))
    assert whole["exported"] == ["Chair", "Lamp"] and "Leg, Reference" in whole["warnings"][0]


def test_roblox_checks_skip_objects_excluded_from_the_view_layer(monkeypatch, tmp_path):
    dense = _Object("Statue", data=_mesh(triangles=20001), in_view_layer=False)
    crate = _Object("Crate")
    exports = []
    addon = _load_addon(monkeypatch, _Objects(dense, crate), [], exports)

    result = addon.BlenderMCPServer().export_scene(str(tmp_path / "c.fbx"), preset="roblox")

    assert result["exported"] == ["Crate"]
    assert len(result["warnings"]) == 1 and "excluded from the view layer: Statue" in result["warnings"][0]


def test_only_excluded_objects_named_is_an_error_not_a_crash(monkeypatch, tmp_path):
    ref = _Object("Reference", in_view_layer=False)
    lamp = _Object("Lamp", mode="EDIT")
    exports, modes = [], []
    addon = _load_addon(monkeypatch, _Objects(ref, lamp), [], exports, active=lamp, mode_calls=modes)

    result = addon.BlenderMCPServer().export_scene(str(tmp_path / "ref.glb"), object_names=["Reference"])

    assert "Nothing to export: Reference" in result["error"] and "view layer" in result["error"]
    assert exports == [] and modes == [], "rejected before touching the mode or selection"
