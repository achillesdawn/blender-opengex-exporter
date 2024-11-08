import bpy
from typing import cast

assert bpy.context
ob = bpy.context.active_object
assert ob

matrix_world = ob.matrix_world

print(matrix_world)

data = cast(bpy.types.Armature, ob.data)

# bone: bpy.types.Bone
# for bone in data.bones:
#     parent = bone.parent
#     bone.matrix_local = matrix_world @ bone.matrix_local

for pose_bone in ob.pose.bones:
    data = cast(bpy.types.Armature, ob.data)

    bone = data.bones[pose_bone.name]
    parent = bone.parent

    # if parent:
    #     parent_local = parent.matrix_local.inverted_safe() @ bone.matrix_local
    #     pose_bone.matrix_basis = matrix_world @ parent_local

    # else:
    pose_bone.matrix_basis = matrix_world @ bone.matrix_local

ob.matrix_world.identity()
