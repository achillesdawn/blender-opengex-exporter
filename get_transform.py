import bpy

am = bpy.context.active_object 

bone_name = 'mixamorig:Spine'

bone = am.data.bones[bone_name]
pose = am.pose.bones[bone_name]
bone_parent = bone.parent
pose_bone_parent = pose.parent

print(bone.matrix_local)
print(pose.matrix_basis)

transform = pose_bone_parent.matrix.inverted_safe() @ pose.matrix

print(transform)

print()

