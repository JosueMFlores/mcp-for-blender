---
title: Roblox
summary: R15 characters, rigid accessories and animations for Roblox Studio: scale and axes, part and bone names, attachments, cages, limits, and FBX export with export_scene(preset="roblox").
---

# Roblox

## Space and scale

- export_scene(preset="roblox") writes 1 Blender unit as 1 stud, whatever the scene's unit scale.
- Z is up and a character faces -Y (Blender's front view sees its face). A character that doesn't
  face -Y walks sideways in Roblox.
- Freeze transforms before exporting: `bpy.ops.object.transform_apply(location=False, rotation=True,
  scale=True)` on the selection. For a character, the armature, HumanoidRootNode and LowerTorso
  also sit at the origin.
- Export in rest pose: a pose set in Blender doesn't reach Roblox.

## R15 character

- The armature object is `Root`. Bones: `HumanoidRootNode` → `LowerTorso` → `UpperTorso` → `Head`;
  `UpperTorso` → `LeftUpperArm` → `LeftLowerArm` → `LeftHand` (same for Right);
  `LowerTorso` → `LeftUpperLeg` → `LeftLowerLeg` → `LeftFoot` (same for Right). 16 bones.
- 15 meshes named `<Part>_Geo` (`Head_Geo`, `LeftUpperArm_Geo`...), each skinned to its own bone,
  one texture each.
- At most 4 bones per vertex. With the mesh active in object mode:
  `bpy.ops.object.vertex_group_limit_total(group_select_mode="BONE_DEFORM", limit=4)`, then
  `bpy.ops.object.vertex_group_normalize_all(group_select_mode="BONE_DEFORM", lock_active=False)`.
- Names must be unique. Exporting a second set (say the template's own attachments) brings in
  `_Att_1`, `_Att_2` duplicates.

## Attachments

19 small meshes named `<Name>_Att`, each parented to its bone (not to an empty, or the player's
hats and hair fall to the floor):

- Head: FaceCenter, FaceFront, Hat, Hair. UpperTorso: LeftCollar, RightCollar, Neck, BodyBack,
  BodyFront. LowerTorso: WaistFront, WaistBack, WaistCenter.
- Left/RightUpperArm: Left/RightShoulder. Left/RightHand: Left/RightGrip. Left/RightFoot:
  Left/RightFoot. `Root_Att` at the origin, on the armature.

```python
w = att.matrix_world.copy()
att.parent, att.parent_type, att.parent_bone = rig, "BONE", "Head"
att.matrix_parent_inverse.identity()
bpy.context.view_layer.update()
att.matrix_world = w
```

## Layered clothing cages

One `<Part>_OuterCage` per body part (15). Start from Roblox's template cage and only move
vertices: never add or delete them or touch the UVs. Keep them just outside the body, with no
weights. Studio turns them into WrapTargets; without them the player's layered clothing floats or
twists. Export the 15 fitted cages only, never the template's own armature, attachments or cages.

## Limits

- 20,000 triangles per mesh (`len(me.loops) - 2 * len(me.polygons)`). Avatar items have smaller
  per-part budgets: check current Roblox docs.
- Textures: 1024x1024 is Roblox's standard size; avatar Marketplace items take up to 2048x2048 and
  larger images get downscaled. Plug the Image Texture straight into Principled Base Color (a Mix
  in between loses the texture in FBX), and keep each image saved as a PNG file.
- A packed image you repaint keeps its old packed bytes: after `image.save()`, `image.unpack(method="REMOVE")`,
  `image.reload()`, then `image.pack()`, or the FBX carries the old texture.

## Rigid accessories (hats, hair)

- One mesh, at most 4,000 triangles, one texture. Model it on the character, in its space.
- No attachments in the FBX: Studio's Accessory Fitting Tool (or a script) adds them. Hats,
  helmets and crowns use HatAttachment; hair uses HairAttachment.
- Keep it off the chest and back: it moves rigidly with the head and clips the torso when the head
  turns. Size limits per type change: check current Roblox docs.

## Animation

- Animate bone rotations; only `LowerTorso` gets location keys, and `HumanoidRootNode` none. Keep
  it in place (the game's scripts move the character) and make a loop's last frame equal its first.
- Export the rig with its meshes (the importer fails on a bare rig), the action assigned, and the
  scene frame range set to the action's. The preset bakes that action only.
- In Studio: Animation Editor on an R15 rig, ⋯ > Import > From FBX Animation, set loop and
  priority, then publish.

## Export and import

1. `export_scene(filepath="//export/Character.fbx", preset="roblox", object_names=["Character"])`,
   naming the character's top object so its children come along.
2. Fix every warning it returns, then export again.
3. Studio: File > Import 3D, pick the .fbx, Rig Type R15 for characters. Check that each body part
   has a WrapTarget and Head has HatAttachment and HairAttachment.

No gore, text, logos or copyrighted designs: moderation removes them.
