import bpy
from bpy.types import Armature
from typing import cast


class TransformApplier:
    armature: bpy.types.Object

    def __init__(self) -> None:
        assert bpy.context

        ob: bpy.types.Object | None = None
        for ob in bpy.data.objects:
            if ob.type == "ARMATURE":
                break

        assert ob
        self.armature = ob

    def execute(self):
        matrix_world = self.armature.matrix_world

        _, _, scale = matrix_world.decompose()
        scale_2d = scale.to_2d()

        self.select_and_make_active(self.armature)
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

        action = (
            self.armature.animation_data.action
            if self.armature.animation_data
            else None
        )
        if not action:
            print("no actions found, finishing")
            return

        for fcurve in action.fcurves:
            
            if not fcurve.data_path.startswith('pose.bones['):
                continue

            print(fcurve.data_path)

            if "location" in fcurve.data_path:
                for keyframe in fcurve.keyframe_points:
                    print(f"{keyframe.co=} {scale=}")
                    keyframe.co = scale_2d * keyframe.co

    @staticmethod
    def select_and_make_active(ob: bpy.types.Object):
        for ob_to_deselect in bpy.data.objects:
            if ob_to_deselect == ob:
                continue
            ob_to_deselect.select_set(False)

        assert bpy.context
        bpy.context.view_layer.objects.active = ob
        ob.select_set(True)

        print(f"[ Status ] {ob.name} set to Active Object")


t = TransformApplier()
t.execute()
