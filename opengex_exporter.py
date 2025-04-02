# =============================================================
#
#  Open Game Engine Exchange
#  http://opengex.org/
#
#  Export plugin for Blender
#  by Eric Lengyel
#    updated for blender 2.80 by Joel Davis
# 	 updated with some fixes by Miguel Cartaxo
#    updated for blender 4.3 by achillesdawn https://github.com/achillesdawn/
#  Version 2.9
#
#  Copyright 2017, Terathon Software LLC
#
#  This software is licensed under the Creative Commons
#  Attribution-ShareAlike 3.0 Unported License:
#
#  http://creativecommons.org/licenses/by-sa/3.0/deed.en_US
#
# =============================================================

bl_info = {
    "name": "OpenGEX (.ogex)",
    "description": "Terathon Software OpenGEX Exporter",
    "author": "Eric Lengyel, Miguel Olivo (achillesdawn)",
    "version": (3, 0, 0, 0),
    "blender": (4, 3, 0),
    "location": "File > Import-Export",
    "wiki_url": "http://opengex.org/",
    "category": "Import-Export",
}


import bpy
from bpy_extras.io_utils import ExportHelper
from bpy.types import Image

from mathutils import Matrix

import struct
import math
import os
import time

from io import BytesIO
from shutil import copyfileobj
from enum import Enum
from pathlib import Path


NODETYPE_NODE = 0
NODETYPE_BONE = 1
NODETYPE_GEO = 2
NODETYPE_LIGHT = 3
NODETYPE_CAMERA = 4

ANIMATION_SAMPLED = 0
ANIMATION_LINEAR = 1
ANIMATION_BEZIER = 2

EPSILON = 1.0e-6

structIdentifier = [
    b"Node $",
    b"BoneNode $",
    b"GeometryNode $",
    b"LightNode $",
    b"CameraNode $",
]


subtranslationName = [b"xpos", b"ypos", b"zpos"]
subrotationName = [b"xrot", b"yrot", b"zrot"]
subscaleName = [b"xscl", b"yscl", b"zscl"]
deltaSubtranslationName = [b"dxpos", b"dypos", b"dzpos"]
deltaSubrotationName = [b"dxrot", b"dyrot", b"dzrot"]
deltaSubscaleName = [b"dxscl", b"dyscl", b"dzscl"]
axisName = [b"x", b"y", b"z"]


VERSION = bpy.app.version


class MaterialPropertyFlags(Enum):
    PropertyColor = 1
    PropertyParam = 2
    PropertySpectrum = 3  # not supported
    PropertyTexture = 4


class ExportVertex:
    __slots__ = (
        "hash",
        "vertexIndex",
        "faceIndex",
        "position",
        "normal",
        "color",
        "texcoord0",
        "texcoord1",
    )

    def __init__(self):
        self.color = [1.0, 1.0, 1.0]
        self.texcoord0 = [0.0, 0.0]
        self.texcoord1 = [0.0, 0.0]

    def __eq__(self, v):
        if self.hash != v.hash:
            return False
        if self.position != v.position:
            return False
        if self.normal != v.normal:
            return False
        if self.color != v.color:
            return False
        if self.texcoord0 != v.texcoord0:
            return False
        if self.texcoord1 != v.texcoord1:
            return False
        return True

    def Hash(self):
        h = hash(self.position[0])
        h = h * 21737 + hash(self.position[1])
        h = h * 21737 + hash(self.position[2])
        h = h * 21737 + hash(self.normal[0])
        h = h * 21737 + hash(self.normal[1])
        h = h * 21737 + hash(self.normal[2])
        h = h * 21737 + hash(self.color[0])
        h = h * 21737 + hash(self.color[1])
        h = h * 21737 + hash(self.color[2])
        h = h * 21737 + hash(self.texcoord0[0])
        h = h * 21737 + hash(self.texcoord0[1])
        h = h * 21737 + hash(self.texcoord1[0])
        h = h * 21737 + hash(self.texcoord1[1])
        self.hash = h


class WriteBuffer:
    def __init__(self) -> None:
        self.buffer = BytesIO()

    def write(self, data: bytes):
        self.buffer.write(data)

    def write_to_file(self, filepath: str):
        self.buffer.seek(0)

        with open(filepath, "wb") as f:
            copyfileobj(self.buffer, f)


class OpenGexPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    def draw(self, context):
        _ = self.layout


class MatrixApplicator:
    armature: bpy.types.Object

    def __init__(self, armature: bpy.types.Object) -> None:
        self.armature = armature

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
            if not fcurve.data_path.startswith("pose.bones["):
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


class OpenGexExporter(bpy.types.Operator, ExportHelper):
    """Export to OpenGEX format"""

    bl_idname = "export_scene.ogex"
    bl_label = "Export OpenGEX"
    filename_ext = ".ogex"

    option_export_selection: bpy.props.BoolProperty(
        name="Export Selection Only",
        description="Export only selected objects",
        default=False,
    )  # type: ignore

    option_sample_animation: bpy.props.BoolProperty(
        name="Force Sampled Animation",
        description="Always export animation as per-frame samples",
        default=False,
    )  # type: ignore

    option_float_as_hex: bpy.props.BoolProperty(
        name="Use Hexadecimals",
        description="Decimal numbers will be exported as hexadecimal numbers",
        default=True,
    )  # type: ignore

    option_export_vertex_colors: bpy.props.BoolProperty(
        name="Export Vertex Colors",
        description="Export the active vertex color layer",
        default=False,
    )  # type: ignore

    option_export_uvs: bpy.props.BoolProperty(
        name="Export UVs", description="Export the active UV layer", default=True
    )  # type: ignore

    option_export_normals: bpy.props.BoolProperty(
        name="Export Normals", description="Export vertex normals", default=True
    )  # type: ignore

    option_export_materials: bpy.props.BoolProperty(
        name="Export Materials",
        description="Export all materials used in the scene",
        default=True,
    )  # type: ignore

    option_apply_transforms: bpy.props.BoolProperty(
        name="Apply Transforms",
        description="Apply all transforms of all objects in the scene",
        default=False,
    )  # type: ignore

    def write(self, text):
        self.file.write(text)

    def indent_write(self, text, extra=0, newline=False):
        if newline:
            self.file.write(b"\n")
        for i in range(self.indentLevel + extra):
            self.file.write(b"\t")
        self.file.write(text)

    def write_int(self, i):
        self.file.write(bytes(str(i), "UTF-8"))

    def write_float_as_is(self, f):
        if (math.isinf(f)) or (math.isnan(f)):
            self.file.write(b"0.0")
        else:
            self.file.write(bytes(str("{:.6f}".format(f)), "UTF-8"))

    def float_to_hex(self, f):
        i = struct.unpack("<I", struct.pack("<f", f))[0]
        return "0x{:08x}".format(i)

    def write_float_as_hex(self, f):
        if (math.isinf(f)) or (math.isnan(f)):
            self.file.write("0x{:08x}".format(0.0))
        else:
            self.file.write(bytes(str(self.float_to_hex(f)), "UTF-8"))

    WriteFloatMap = [write_float_as_is, write_float_as_hex]

    def write_float(self, f):
        self.WriteFloatMap[int(self.option_float_as_hex)](self, f)

    def write_matrix(self, matrix):
        self.indent_write(b"{", 1)
        self.write_float(matrix[0][0])
        self.write(b", ")
        self.write_float(matrix[1][0])
        self.write(b", ")
        self.write_float(matrix[2][0])
        self.write(b", ")
        self.write_float(matrix[3][0])
        self.write(b",\n")

        self.indent_write(b" ", 1)
        self.write_float(matrix[0][1])
        self.write(b", ")
        self.write_float(matrix[1][1])
        self.write(b", ")
        self.write_float(matrix[2][1])
        self.write(b", ")
        self.write_float(matrix[3][1])
        self.write(b",\n")

        self.indent_write(b" ", 1)
        self.write_float(matrix[0][2])
        self.write(b", ")
        self.write_float(matrix[1][2])
        self.write(b", ")
        self.write_float(matrix[2][2])
        self.write(b", ")
        self.write_float(matrix[3][2])
        self.write(b",\n")

        self.indent_write(b" ", 1)
        self.write_float(matrix[0][3])
        self.write(b", ")
        self.write_float(matrix[1][3])
        self.write(b", ")
        self.write_float(matrix[2][3])
        self.write(b", ")
        self.write_float(matrix[3][3])
        self.write(b"}\n")

    def write_matrix_flat(self, matrix):
        self.indent_write(b"{", 1)
        self.write_float(matrix[0][0])
        self.write(b", ")
        self.write_float(matrix[1][0])
        self.write(b", ")
        self.write_float(matrix[2][0])
        self.write(b", ")
        self.write_float(matrix[3][0])
        self.write(b", ")
        self.write_float(matrix[0][1])
        self.write(b", ")
        self.write_float(matrix[1][1])
        self.write(b", ")
        self.write_float(matrix[2][1])
        self.write(b", ")
        self.write_float(matrix[3][1])
        self.write(b", ")
        self.write_float(matrix[0][2])
        self.write(b", ")
        self.write_float(matrix[1][2])
        self.write(b", ")
        self.write_float(matrix[2][2])
        self.write(b", ")
        self.write_float(matrix[3][2])
        self.write(b", ")
        self.write_float(matrix[0][3])
        self.write(b", ")
        self.write_float(matrix[1][3])
        self.write(b", ")
        self.write_float(matrix[2][3])
        self.write(b", ")
        self.write_float(matrix[3][3])
        self.write(b"}")

    def write_color(self, color):
        self.write(b"{")
        self.write_float(color[0])
        self.write(b", ")
        self.write_float(color[1])
        self.write(b", ")
        self.write_float(color[2])
        self.write(b"}")

    def write_file_name(self, filename):
        length = len(filename)
        if length != 0:
            if (length > 2) and (filename[1] == ":"):
                self.write(b"//")
                self.write(bytes(filename[0], "UTF-8"))
                self.write(bytes(filename[2:length].replace("\\", "/"), "UTF-8"))
            else:
                self.write(bytes(filename.replace("\\", "/"), "UTF-8"))

    def write_int_array(self, valueArray):
        count = len(valueArray)
        k = 0

        lineCount = count >> 6
        for i in range(lineCount):
            self.indent_write(b"", 1)
            for j in range(63):
                self.write_int(valueArray[k])
                self.write(b", ")
                k += 1

            self.write_int(valueArray[k])
            k += 1

            if i * 64 < count - 64:
                self.write(b",\n")
            else:
                self.write(b"\n")

        count &= 63
        if count != 0:
            self.indent_write(b"", 1)
            for j in range(count - 1):
                self.write_int(valueArray[k])
                self.write(b", ")
                k += 1

            self.write_int(valueArray[k])
            self.write(b"\n")

    def write_float_array(self, valueArray):
        count = len(valueArray)
        k = 0

        lineCount = count >> 4
        for i in range(lineCount):
            self.indent_write(b"", 1)
            for j in range(15):
                self.write_float(valueArray[k])
                self.write(b", ")
                k += 1

            self.write_float(valueArray[k])
            k += 1

            if i * 16 < count - 16:
                self.write(b",\n")
            else:
                self.write(b"\n")

        count &= 15
        if count != 0:
            self.indent_write(b"", 1)
            for j in range(count - 1):
                self.write_float(valueArray[k])
                self.write(b", ")
                k += 1

            self.write_float(valueArray[k])
            self.write(b"\n")

    def write_vector_2d(self, vector):
        self.write(b"{")
        self.write_float(vector[0])
        self.write(b", ")
        self.write_float(vector[1])
        self.write(b"}")

    def write_vector_3d(self, vector):
        self.write(b"{")
        self.write_float(vector[0])
        self.write(b", ")
        self.write_float(vector[1])
        self.write(b", ")
        self.write_float(vector[2])
        self.write(b"}")

    def write_vector_4d(self, vector):
        self.write(b"{")
        self.write_float(vector[0])
        self.write(b", ")
        self.write_float(vector[1])
        self.write(b", ")
        self.write_float(vector[2])
        self.write(b", ")
        self.write_float(vector[3])
        self.write(b"}")

    def write_quaternion(self, quaternion):
        self.write(b"{")
        self.write_float(quaternion[1])
        self.write(b", ")
        self.write_float(quaternion[2])
        self.write(b", ")
        self.write_float(quaternion[3])
        self.write(b", ")
        self.write_float(quaternion[0])
        self.write(b"}")

    def write_vertex_array_2d(self, vertexArray, attrib):
        count = len(vertexArray)
        k = 0

        lineCount = count >> 3
        for i in range(lineCount):
            self.indent_write(b"", 1)
            for j in range(7):
                self.write_vector_2d(getattr(vertexArray[k], attrib))
                self.write(b", ")
                k += 1

            self.write_vector_2d(getattr(vertexArray[k], attrib))
            k += 1

            if i * 8 < count - 8:
                self.write(b",\n")
            else:
                self.write(b"\n")

        count &= 7
        if count != 0:
            self.indent_write(b"", 1)
            for j in range(count - 1):
                self.write_vector_2d(getattr(vertexArray[k], attrib))
                self.write(b", ")
                k += 1

            self.write_vector_2d(getattr(vertexArray[k], attrib))
            self.write(b"\n")

    def write_vertex_array_3d(self, vertexArray, attrib):
        count = len(vertexArray)
        k = 0

        lineCount = count >> 3
        for i in range(lineCount):
            self.indent_write(b"", 1)
            for j in range(7):
                self.write_vector_3d(getattr(vertexArray[k], attrib))
                self.write(b", ")
                k += 1

            self.write_vector_3d(getattr(vertexArray[k], attrib))
            k += 1

            if i * 8 < count - 8:
                self.write(b",\n")
            else:
                self.write(b"\n")

        count &= 7
        if count != 0:
            self.indent_write(b"", 1)
            for j in range(count - 1):
                self.write_vector_3d(getattr(vertexArray[k], attrib))
                self.write(b", ")
                k += 1

            self.write_vector_3d(getattr(vertexArray[k], attrib))
            self.write(b"\n")

    def write_morph_position_array_3d(self, vertexArray, meshVertexArray):
        count = len(vertexArray)
        k = 0

        lineCount = count >> 3
        for i in range(lineCount):
            self.indent_write(b"", 1)
            for j in range(7):
                self.write_vector_3d(meshVertexArray[vertexArray[k].vertexIndex].co)
                self.write(b", ")
                k += 1

            self.write_vector_3d(meshVertexArray[vertexArray[k].vertexIndex].co)
            k += 1

            if i * 8 < count - 8:
                self.write(b",\n")
            else:
                self.write(b"\n")

        count &= 7
        if count != 0:
            self.indent_write(b"", 1)
            for j in range(count - 1):
                self.write_vector_3d(meshVertexArray[vertexArray[k].vertexIndex].co)
                self.write(b", ")
                k += 1

            self.write_vector_3d(meshVertexArray[vertexArray[k].vertexIndex].co)
            self.write(b"\n")

    def write_morph_normal_array_3d(self, vertexArray, meshVertexArray, tessFaceArray):
        count = len(vertexArray)
        k = 0

        lineCount = count >> 3
        for i in range(lineCount):
            self.indent_write(b"", 1)
            for j in range(7):
                face = tessFaceArray[vertexArray[k].faceIndex]
                self.write_vector_3d(
                    meshVertexArray[vertexArray[k].vertexIndex].normal
                    if (face.use_smooth)
                    else face.normal
                )
                self.write(b", ")
                k += 1

            face = tessFaceArray[vertexArray[k].faceIndex]
            self.write_vector_3d(
                meshVertexArray[vertexArray[k].vertexIndex].normal
                if (face.use_smooth)
                else face.normal
            )
            k += 1

            if i * 8 < count - 8:
                self.write(b",\n")
            else:
                self.write(b"\n")

        count &= 7
        if count != 0:
            self.indent_write(b"", 1)
            for j in range(count - 1):
                face = tessFaceArray[vertexArray[k].faceIndex]
                self.write_vector_3d(
                    meshVertexArray[vertexArray[k].vertexIndex].normal
                    if (face.use_smooth)
                    else face.normal
                )
                self.write(b", ")
                k += 1

            face = tessFaceArray[vertexArray[k].faceIndex]
            self.write_vector_3d(
                meshVertexArray[vertexArray[k].vertexIndex].normal
                if (face.use_smooth)
                else face.normal
            )
            self.write(b"\n")

    def write_triangle(self, triangleIndex, indexTable):
        i = triangleIndex * 3
        self.write(b"{")
        self.write_int(indexTable[i])
        self.write(b", ")
        self.write_int(indexTable[i + 1])
        self.write(b", ")
        self.write_int(indexTable[i + 2])
        self.write(b"}")

    def write_triangle_array(self, count, indexTable):
        triangleIndex = 0

        lineCount = count >> 4
        for i in range(lineCount):
            self.indent_write(b"", 1)
            for j in range(15):
                self.write_triangle(triangleIndex, indexTable)
                self.write(b", ")
                triangleIndex += 1

            self.write_triangle(triangleIndex, indexTable)
            triangleIndex += 1

            if i * 16 < count - 16:
                self.write(b",\n")
            else:
                self.write(b"\n")

        count &= 15
        if count != 0:
            self.indent_write(b"", 1)
            for j in range(count - 1):
                self.write_triangle(triangleIndex, indexTable)
                self.write(b", ")
                triangleIndex += 1

            self.write_triangle(triangleIndex, indexTable)
            self.write(b"\n")

    def write_node_table(self, objectRef):
        first = True
        for node in objectRef[1]["nodeTable"]:
            if first:
                self.write(b"\t\t// ")
            else:
                self.write(b", ")
            self.write(bytes(node.name, "UTF-8"))
            first = False

    @staticmethod
    def get_node_type(node):
        if node.type == "MESH":
            if len(node.data.polygons) != 0:
                return NODETYPE_GEO
        # ***
        # the 'type' attribute for light objects in blender 3.0+
        # is 'LIGHT' instead of 'LAMP'
        elif node.type == "LIGHT":
            # ***
            type = node.data.type
            if (type == "SUN") or (type == "POINT") or (type == "SPOT"):
                return NODETYPE_LIGHT
        elif node.type == "CAMERA":
            return NODETYPE_CAMERA

        return NODETYPE_NODE

    @staticmethod
    def get_shape_keys(mesh):
        shapeKeys = mesh.shape_keys
        if (shapeKeys) and (len(shapeKeys.key_blocks) > 1):
            return shapeKeys

        return None

    def find_node(self, name):
        for nodeRef in self.nodeArray.items():
            if nodeRef[0].name == name:
                return nodeRef
        return None

    @staticmethod
    def deindex_mesh(mesh, materialTable, shouldExportVertexColor=True):
        mesh.calc_loop_triangles()

        # This function deindexes all vertex positions, colors, and texcoords.
        # Three separate ExportVertex structures are created for each triangle.

        vertexArray = mesh.vertices
        exportVertexArray = []
        faceIndex = 0

        for face in mesh.loop_triangles:
            k1 = face.vertices[0]
            k2 = face.vertices[1]
            k3 = face.vertices[2]

            v1 = vertexArray[k1]
            v2 = vertexArray[k2]
            v3 = vertexArray[k3]

            exportVertex = ExportVertex()
            exportVertex.vertexIndex = k1
            exportVertex.faceIndex = faceIndex
            exportVertex.position = v1.co
            exportVertex.normal = v1.normal if (face.use_smooth) else face.normal
            exportVertexArray.append(exportVertex)

            exportVertex = ExportVertex()
            exportVertex.vertexIndex = k2
            exportVertex.faceIndex = faceIndex
            exportVertex.position = v2.co
            exportVertex.normal = v2.normal if (face.use_smooth) else face.normal
            exportVertexArray.append(exportVertex)

            exportVertex = ExportVertex()
            exportVertex.vertexIndex = k3
            exportVertex.faceIndex = faceIndex
            exportVertex.position = v3.co
            exportVertex.normal = v3.normal if (face.use_smooth) else face.normal
            exportVertexArray.append(exportVertex)

            materialTable.append(face.material_index)

            faceIndex += 1

        colorCount = len(mesh.vertex_colors)
        if colorCount > 0 and shouldExportVertexColor:
            colorFace = mesh.vertex_colors[0].data
            vertexIndex = 0
            faceIndex = 0

            for face in mesh.loop_triangles:
                cf = colorFace[faceIndex]
                exportVertexArray[vertexIndex].color[0] = cf.color[0]
                vertexIndex += 1
                exportVertexArray[vertexIndex].color[1] = cf.color[1]
                vertexIndex += 1
                exportVertexArray[vertexIndex].color[2] = cf.color[2]
                vertexIndex += 1

                if len(face.vertices) == 4:
                    exportVertexArray[vertexIndex].color[0] = cf.color[1]
                    vertexIndex += 1
                    exportVertexArray[vertexIndex].color[1] = cf.color[2]
                    vertexIndex += 1
                    exportVertexArray[vertexIndex].color[2] = cf.color[3]
                    vertexIndex += 1

                faceIndex += 1

        uv_layer0 = mesh.uv_layers[0] if len(mesh.uv_layers) > 0 else None
        uv_layer1 = mesh.uv_layers[1] if len(mesh.uv_layers) > 1 else None

        vertexIndex = 0
        for tri in mesh.loop_triangles:
            for loop_index in tri.loops:
                if uv_layer0:
                    uv0 = uv_layer0.data[loop_index].uv
                    exportVertexArray[vertexIndex].texcoord0 = uv0

                if uv_layer1:
                    uv1 = uv_layer1.data[loop_index].uv
                    exportVertexArray[vertexIndex].texcoord1 = uv1

                vertexIndex += 1

        for ev in exportVertexArray:
            ev.Hash()

        return exportVertexArray

    @staticmethod
    def find_export_vertex(bucket, exportVertexArray, vertex):
        for index in bucket:
            if exportVertexArray[index] == vertex:
                return index

        return -1

    @staticmethod
    def unify_vertices(exportVertexArray, indexTable):
        # This function looks for identical vertices having exactly the same position, normal,
        # color, and texcoords. Duplicate vertices are unified, and a new index table is returned.

        bucketCount = len(exportVertexArray) >> 3
        if bucketCount > 1:
            # Round down to nearest power of two.

            while True:
                count = bucketCount & (bucketCount - 1)
                if count == 0:
                    break
                bucketCount = count
        else:
            bucketCount = 1

        hashTable = [[] for i in range(bucketCount)]
        unifiedVertexArray = []

        for i in range(len(exportVertexArray)):
            ev = exportVertexArray[i]
            bucket = ev.hash & (bucketCount - 1)
            index = OpenGexExporter.find_export_vertex(
                hashTable[bucket], exportVertexArray, ev
            )
            if index < 0:
                indexTable.append(len(unifiedVertexArray))
                unifiedVertexArray.append(ev)
                hashTable[bucket].append(i)
            else:
                indexTable.append(indexTable[index])

        return unifiedVertexArray

    def process_bone(self, bone):
        if self.exportAllFlag or bone.select:
            self.nodeArray[bone] = {
                "nodeType": NODETYPE_BONE,
                "structName": bytes("node" + str(len(self.nodeArray) + 1), "UTF-8"),
            }

        for child in bone.children:
            self.process_bone(child)

    def process_node(self, node):
        if self.exportAllFlag or node.select_get():
            node_type = OpenGexExporter.get_node_type(node)

            self.nodeArray[node] = {
                "nodeType": node_type,
                "structName": bytes("node" + str(len(self.nodeArray) + 1), "UTF-8"),
            }

            if node.parent_type == "BONE":
                boneSubnodeArray = self.boneParentArray.get(node.parent_bone)
                if boneSubnodeArray:
                    boneSubnodeArray.append(node)
                else:
                    self.boneParentArray[node.parent_bone] = [node]

            if node.type == "ARMATURE":
                skeleton = node.data
                if skeleton:
                    for bone in skeleton.bones:
                        if not bone.parent:
                            self.process_bone(bone)

        for child in node.children:
            self.process_node(child)

    def process_skinned_meshes(self):
        for node_ref in self.nodeArray.items():
            if node_ref[1]["nodeType"] == NODETYPE_GEO:
                armature = node_ref[0].find_armature()
                if armature:
                    for bone in armature.data.bones:
                        bone_ref = self.find_node(bone.name)
                        if bone_ref:
                            # If a node is used as a bone, then we force its type to be a bone.

                            bone_ref[1]["nodeType"] = NODETYPE_BONE

    @staticmethod
    def ClassifyAnimationCurve(fcurve):
        linearCount = 0
        bezierCount = 0

        for key in fcurve.keyframe_points:
            interp = key.interpolation
            if interp == "LINEAR":
                linearCount += 1
            elif interp == "BEZIER":
                bezierCount += 1
            else:
                return ANIMATION_SAMPLED

        if bezierCount == 0:
            return ANIMATION_LINEAR
        elif linearCount == 0:
            return ANIMATION_BEZIER

        return ANIMATION_SAMPLED

    @staticmethod
    def AnimationKeysDifferent(fcurve):
        keyCount = len(fcurve.keyframe_points)
        if keyCount > 0:
            key1 = fcurve.keyframe_points[0].co[1]

            for i in range(1, keyCount):
                key2 = fcurve.keyframe_points[i].co[1]
                if math.fabs(key2 - key1) > EPSILON:
                    return True

        return False

    @staticmethod
    def AnimationTangentsNonzero(fcurve):
        keyCount = len(fcurve.keyframe_points)
        if keyCount > 0:
            key = fcurve.keyframe_points[0].co[1]
            left = fcurve.keyframe_points[0].handle_left[1]
            right = fcurve.keyframe_points[0].handle_right[1]
            if (math.fabs(key - left) > EPSILON) or (math.fabs(right - key) > EPSILON):
                return True

            for i in range(1, keyCount):
                key = fcurve.keyframe_points[i].co[1]
                left = fcurve.keyframe_points[i].handle_left[1]
                right = fcurve.keyframe_points[i].handle_right[1]
                if (math.fabs(key - left) > EPSILON) or (
                    math.fabs(right - key) > EPSILON
                ):
                    return True

        return False

    @staticmethod
    def AnimationPresent(fcurve, kind):
        if kind != ANIMATION_BEZIER:
            return OpenGexExporter.AnimationKeysDifferent(fcurve)

        return (OpenGexExporter.AnimationKeysDifferent(fcurve)) or (
            OpenGexExporter.AnimationTangentsNonzero(fcurve)
        )

    @staticmethod
    def MatricesDifferent(m1, m2):
        for i in range(4):
            for j in range(4):
                if math.fabs(m1[i][j] - m2[i][j]) > EPSILON:
                    return True

        return False

    @staticmethod
    def CollectBoneAnimation(armature, name):
        path = 'pose.bones["' + name + '"].'
        curveArray = []

        if armature.animation_data:
            action = armature.animation_data.action
            if action:
                for fcurve in action.fcurves:
                    if fcurve.data_path.startswith(path):
                        # if "location" in fcurve.data_path:
                        #     action.fcurves.remove(fcurve)
                        curveArray.append(fcurve)

        return curveArray

    def ExportKeyTimes(self, fcurve):
        self.indent_write(b"Key {float {")

        keyCount = len(fcurve.keyframe_points)
        for i in range(keyCount):
            if i > 0:
                self.write(b", ")

            time = fcurve.keyframe_points[i].co[0] - self.beginFrame
            self.write_float(time * self.frameTime)

        self.write(b"}}\n")

    def ExportKeyTimeControlPoints(self, fcurve):
        self.indent_write(b'Key (kind = "-control") {float {')

        keyCount = len(fcurve.keyframe_points)
        for i in range(keyCount):
            if i > 0:
                self.write(b", ")

            ctrl = fcurve.keyframe_points[i].handle_left[0] - self.beginFrame
            self.write_float(ctrl * self.frameTime)

        self.write(b"}}\n")
        self.indent_write(b'Key (kind = "+control") {float {')

        for i in range(keyCount):
            if i > 0:
                self.write(b", ")

            ctrl = fcurve.keyframe_points[i].handle_right[0] - self.beginFrame
            self.write_float(ctrl * self.frameTime)

        self.write(b"}}\n")

    def ExportKeyValues(self, fcurve):
        self.indent_write(b"Key {float {")

        keyCount = len(fcurve.keyframe_points)
        for i in range(keyCount):
            if i > 0:
                self.write(b", ")

            value = fcurve.keyframe_points[i].co[1]
            self.write_float(value)

        self.write(b"}}\n")

    def ExportKeyValueControlPoints(self, fcurve):
        self.indent_write(b'Key (kind = "-control") {float {')

        keyCount = len(fcurve.keyframe_points)
        for i in range(keyCount):
            if i > 0:
                self.write(b", ")

            ctrl = fcurve.keyframe_points[i].handle_left[1]
            self.write_float(ctrl)

        self.write(b"}}\n")
        self.indent_write(b'Key (kind = "+control") {float {')

        for i in range(keyCount):
            if i > 0:
                self.write(b", ")

            ctrl = fcurve.keyframe_points[i].handle_right[1]
            self.write_float(ctrl)

        self.write(b"}}\n")

    def ExportAnimationTrack(self, fcurve, kind, target, newline):
        # This function exports a single animation track. The curve types for the
        # Time and Value structures are given by the kind parameter.

        self.indent_write(b"Track (target = %", 0, newline)
        self.write(target)
        self.write(b")\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        if kind != ANIMATION_BEZIER:
            self.indent_write(b"Time\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.ExportKeyTimes(fcurve)

            self.indent_write(b"}\n\n", -1)
            self.indent_write(b"Value\n", -1)
            self.indent_write(b"{\n", -1)

            self.ExportKeyValues(fcurve)

            self.indentLevel -= 1
            self.indent_write(b"}\n")

        else:
            self.indent_write(b'Time (curve = "bezier")\n')
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.ExportKeyTimes(fcurve)
            self.ExportKeyTimeControlPoints(fcurve)

            self.indent_write(b"}\n\n", -1)
            self.indent_write(b'Value (curve = "bezier")\n', -1)
            self.indent_write(b"{\n", -1)

            self.ExportKeyValues(fcurve)
            self.ExportKeyValueControlPoints(fcurve)

            self.indentLevel -= 1
            self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n")

    def ExportNodeSampledAnimation(self, node, scene):
        # This function exports animation as full 4x4 matrices for each frame.

        currentFrame = scene.frame_current
        currentSubframe = scene.frame_subframe

        animationFlag = False
        m1 = node.matrix_local.copy()

        for i in range(self.beginFrame, self.endFrame):
            scene.frame_set(i)
            m2 = node.matrix_local
            if OpenGexExporter.MatricesDifferent(m1, m2):
                animationFlag = True
                break

        if animationFlag:
            self.indent_write(b"Animation\n", 0, True)
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"Track (target = %transform)\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"Time\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"Key {float {")

            for i in range(self.beginFrame, self.endFrame):
                self.write(b", ")

            self.write_float(self.endFrame * self.frameTime)
            self.write(b"}}\n")

            self.indent_write(b"}\n\n", -1)
            self.indent_write(b"Value\n", -1)
            self.indent_write(b"{\n", -1)

            self.indent_write(b"Key\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"float[16]\n")
            self.indent_write(b"{\n")

            for i in range(self.beginFrame, self.endFrame):
                scene.frame_set(i)
                self.write_matrix_flat(node.matrix_local)
                self.write(b",\n")

            scene.frame_set(self.endFrame)
            self.write_matrix_flat(node.matrix_local)
            self.indent_write(b"}\n", 0, True)

            self.indentLevel -= 1
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

        scene.frame_set(currentFrame, subframe=currentSubframe)

    def ExportBoneSampledAnimation(self, poseBone, scene):
        # This function exports bone animation as full 4x4 matrices for each frame.

        currentFrame = scene.frame_current
        currentSubframe = scene.frame_subframe

        animationFlag = False
        m1 = poseBone.matrix.copy()

        for i in range(self.beginFrame, self.endFrame):
            scene.frame_set(i)
            m2 = poseBone.matrix
            if OpenGexExporter.MatricesDifferent(m1, m2):
                animationFlag = True
                break

        if animationFlag:
            self.indent_write(b"Animation\n", 0, True)
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"Track (target = %transform)\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"Time\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"Key {float {")

            for i in range(self.beginFrame, self.endFrame):
                self.write_float((i - self.beginFrame) * self.frameTime)
                self.write(b", ")

            self.write_float(self.endFrame * self.frameTime)
            self.write(b"}}\n")

            self.indent_write(b"}\n\n", -1)
            self.indent_write(b"Value\n", -1)
            self.indent_write(b"{\n", -1)

            self.indent_write(b"Key\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"float[16]\n")
            self.indent_write(b"{\n")

            parent = poseBone.parent
            if parent:
                for i in range(self.beginFrame, self.endFrame):
                    scene.frame_set(i)
                    if math.fabs(parent.matrix.determinant()) > EPSILON:
                        # replaced the matrix multiplication operator '*' with '@',
                        # because it no longer works for blender 3.0+

                        # ***
                        self.write_matrix_flat(
                            parent.matrix.inverted() @ poseBone.matrix
                        )
                        # ***
                    else:
                        self.write_matrix_flat(poseBone.matrix)

                    self.write(b",\n")

                scene.frame_set(self.endFrame)
                if math.fabs(parent.matrix.determinant()) > EPSILON:
                    self.write_matrix_flat(parent.matrix.inverted() @ poseBone.matrix)
                else:
                    self.write_matrix_flat(poseBone.matrix)

                self.indent_write(b"}\n", 0, True)

            else:
                for i in range(self.beginFrame, self.endFrame):
                    scene.frame_set(i)
                    self.write_matrix_flat(poseBone.matrix)
                    self.write(b",\n")

                scene.frame_set(self.endFrame)
                self.write_matrix_flat(poseBone.matrix)
                self.indent_write(b"}\n", 0, True)

            self.indentLevel -= 1
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

        scene.frame_set(currentFrame, subframe=currentSubframe)

    def ExportMorphWeightSampledAnimationTrack(self, block, target, scene, newline):
        currentFrame = scene.frame_current
        currentSubframe = scene.frame_subframe

        self.indent_write(b"Track (target = %", 0, newline)
        self.write(target)
        self.write(b")\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b"Time\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b"Key {float {")

        for i in range(self.beginFrame, self.endFrame):
            self.write_float((i - self.beginFrame) * self.frameTime)
            self.write(b", ")

        self.write_float(self.endFrame * self.frameTime)
        self.write(b"}}\n")

        self.indent_write(b"}\n\n", -1)
        self.indent_write(b"Value\n", -1)
        self.indent_write(b"{\n", -1)

        self.indent_write(b"Key {float {")

        for i in range(self.beginFrame, self.endFrame):
            scene.frame_set(i)
            self.write_float(block.value)
            self.write(b", ")

        scene.frame_set(self.endFrame)
        self.write_float(block.value)
        self.write(b"}}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n")

        scene.frame_set(currentFrame, subframe=currentSubframe)

    def ExportNodeTransform(self, node, scene):
        posAnimCurve = [None, None, None]
        rotAnimCurve = [None, None, None]
        sclAnimCurve = [None, None, None]
        posAnimKind = [0, 0, 0]
        rotAnimKind = [0, 0, 0]
        sclAnimKind = [0, 0, 0]

        deltaPosAnimCurve = [None, None, None]
        deltaRotAnimCurve = [None, None, None]
        deltaSclAnimCurve = [None, None, None]
        deltaPosAnimKind = [0, 0, 0]
        deltaRotAnimKind = [0, 0, 0]
        deltaSclAnimKind = [0, 0, 0]

        positionAnimated = False
        rotationAnimated = False
        scaleAnimated = False
        posAnimated = [False, False, False]
        rotAnimated = [False, False, False]
        sclAnimated = [False, False, False]

        deltaPositionAnimated = False
        deltaRotationAnimated = False
        deltaScaleAnimated = False
        deltaPosAnimated = [False, False, False]
        deltaRotAnimated = [False, False, False]
        deltaSclAnimated = [False, False, False]

        mode = node.rotation_mode
        sampledAnimation = (
            (self.sampleAnimationFlag)
            or (mode == "QUATERNION")
            or (mode == "AXIS_ANGLE")
        )

        if (not sampledAnimation) and (node.animation_data):
            action = node.animation_data.action
            if action:
                for fcurve in action.fcurves:
                    kind = OpenGexExporter.ClassifyAnimationCurve(fcurve)
                    if kind != ANIMATION_SAMPLED:
                        if fcurve.data_path == "location":
                            for i in range(3):
                                if (fcurve.array_index == i) and (not posAnimCurve[i]):
                                    posAnimCurve[i] = fcurve
                                    posAnimKind[i] = kind
                                    if OpenGexExporter.AnimationPresent(fcurve, kind):
                                        posAnimated[i] = True
                        elif fcurve.data_path == "delta_location":
                            for i in range(3):
                                if (fcurve.array_index == i) and (
                                    not deltaPosAnimCurve[i]
                                ):
                                    deltaPosAnimCurve[i] = fcurve
                                    deltaPosAnimKind[i] = kind
                                    if OpenGexExporter.AnimationPresent(fcurve, kind):
                                        deltaPosAnimated[i] = True
                        elif fcurve.data_path == "rotation_euler":
                            for i in range(3):
                                if (fcurve.array_index == i) and (not rotAnimCurve[i]):
                                    rotAnimCurve[i] = fcurve
                                    rotAnimKind[i] = kind
                                    if OpenGexExporter.AnimationPresent(fcurve, kind):
                                        rotAnimated[i] = True
                        elif fcurve.data_path == "delta_rotation_euler":
                            for i in range(3):
                                if (fcurve.array_index == i) and (
                                    not deltaRotAnimCurve[i]
                                ):
                                    deltaRotAnimCurve[i] = fcurve
                                    deltaRotAnimKind[i] = kind
                                    if OpenGexExporter.AnimationPresent(fcurve, kind):
                                        deltaRotAnimated[i] = True
                        elif fcurve.data_path == "scale":
                            for i in range(3):
                                if (fcurve.array_index == i) and (not sclAnimCurve[i]):
                                    sclAnimCurve[i] = fcurve
                                    sclAnimKind[i] = kind
                                    if OpenGexExporter.AnimationPresent(fcurve, kind):
                                        sclAnimated[i] = True
                        elif fcurve.data_path == "delta_scale":
                            for i in range(3):
                                if (fcurve.array_index == i) and (
                                    not deltaSclAnimCurve[i]
                                ):
                                    deltaSclAnimCurve[i] = fcurve
                                    deltaSclAnimKind[i] = kind
                                    if OpenGexExporter.AnimationPresent(fcurve, kind):
                                        deltaSclAnimated[i] = True
                        elif (
                            (fcurve.data_path == "rotation_axis_angle")
                            or (fcurve.data_path == "rotation_quaternion")
                            or (fcurve.data_path == "delta_rotation_quaternion")
                        ):
                            sampledAnimation = True
                            break
                    else:
                        sampledAnimation = True
                        break

        positionAnimated = posAnimated[0] | posAnimated[1] | posAnimated[2]
        rotationAnimated = rotAnimated[0] | rotAnimated[1] | rotAnimated[2]
        scaleAnimated = sclAnimated[0] | sclAnimated[1] | sclAnimated[2]

        deltaPositionAnimated = (
            deltaPosAnimated[0] | deltaPosAnimated[1] | deltaPosAnimated[2]
        )
        deltaRotationAnimated = (
            deltaRotAnimated[0] | deltaRotAnimated[1] | deltaRotAnimated[2]
        )
        deltaScaleAnimated = (
            deltaSclAnimated[0] | deltaSclAnimated[1] | deltaSclAnimated[2]
        )

        if (sampledAnimation) or (
            (not positionAnimated)
            and (not rotationAnimated)
            and (not scaleAnimated)
            and (not deltaPositionAnimated)
            and (not deltaRotationAnimated)
            and (not deltaScaleAnimated)
        ):
            # If there's no keyframe animation at all, then write the node transform as a single 4x4 matrix.
            # We might still be exporting sampled animation below.

            self.indent_write(b"Transform")

            if sampledAnimation:
                self.write(b" %transform")

            self.indent_write(b"{\n", 0, True)
            self.indentLevel += 1

            self.indent_write(b"float[16]\n")
            self.indent_write(b"{\n")
            self.write_matrix(node.matrix_local)
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

            if sampledAnimation:
                self.ExportNodeSampledAnimation(node, scene)

        else:
            structFlag = False

            deltaTranslation = node.delta_location
            if deltaPositionAnimated:
                # When the delta location is animated, write the x, y, and z components separately
                # so they can be targeted by different tracks having different sets of keys.

                for i in range(3):
                    pos = deltaTranslation[i]
                    if (deltaPosAnimated[i]) or (math.fabs(pos) > EPSILON):
                        self.indent_write(b"Translation %", 0, structFlag)
                        self.write(deltaSubtranslationName[i])
                        self.write(b' (kind = "')
                        self.write(axisName[i])
                        self.write(b'")\n')
                        self.indent_write(b"{\n")
                        self.indent_write(b"float {", 1)
                        self.write_float(pos)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

            elif (
                (math.fabs(deltaTranslation[0]) > EPSILON)
                or (math.fabs(deltaTranslation[1]) > EPSILON)
                or (math.fabs(deltaTranslation[2]) > EPSILON)
            ):
                self.indent_write(b"Translation\n")
                self.indent_write(b"{\n")
                self.indent_write(b"float[3] {", 1)
                self.write_vector_3d(deltaTranslation)
                self.write(b"}")
                self.indent_write(b"}\n", 0, True)

                structFlag = True

            translation = node.location
            if positionAnimated:
                # When the location is animated, write the x, y, and z components separately
                # so they can be targeted by different tracks having different sets of keys.

                for i in range(3):
                    pos = translation[i]
                    if (posAnimated[i]) or (math.fabs(pos) > EPSILON):
                        self.indent_write(b"Translation %", 0, structFlag)
                        self.write(subtranslationName[i])
                        self.write(b' (kind = "')
                        self.write(axisName[i])
                        self.write(b'")\n')
                        self.indent_write(b"{\n")
                        self.indent_write(b"float {", 1)
                        self.write_float(pos)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

            elif (
                (math.fabs(translation[0]) > EPSILON)
                or (math.fabs(translation[1]) > EPSILON)
                or (math.fabs(translation[2]) > EPSILON)
            ):
                self.indent_write(b"Translation\n")
                self.indent_write(b"{\n")
                self.indent_write(b"float[3] {", 1)
                self.write_vector_3d(translation)
                self.write(b"}")
                self.indent_write(b"}\n", 0, True)

                structFlag = True

            if deltaRotationAnimated:
                # When the delta rotation is animated, write three separate Euler angle rotations
                # so they can be targeted by different tracks having different sets of keys.

                for i in range(3):
                    axis = ord(mode[2 - i]) - 0x58
                    angle = node.delta_rotation_euler[axis]
                    if (deltaRotAnimated[axis]) or (math.fabs(angle) > EPSILON):
                        self.indent_write(b"Rotation %", 0, structFlag)
                        self.write(deltaSubrotationName[axis])
                        self.write(b' (kind = "')
                        self.write(axisName[axis])
                        self.write(b'")\n')
                        self.indent_write(b"{\n")
                        self.indent_write(b"float {", 1)
                        self.write_float(angle)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

            else:
                # When the delta rotation is not animated, write it in the representation given by
                # the node's current rotation mode. (There is no axis-angle delta rotation.)

                if mode == "QUATERNION":
                    quaternion = node.delta_rotation_quaternion
                    if (
                        (math.fabs(quaternion[0] - 1.0) > EPSILON)
                        or (math.fabs(quaternion[1]) > EPSILON)
                        or (math.fabs(quaternion[2]) > EPSILON)
                        or (math.fabs(quaternion[3]) > EPSILON)
                    ):
                        self.indent_write(
                            b'Rotation (kind = "quaternion")\n', 0, structFlag
                        )
                        self.indent_write(b"{\n")
                        self.indent_write(b"float[4] {", 1)
                        self.write_quaternion(quaternion)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

                else:
                    for i in range(3):
                        axis = ord(mode[2 - i]) - 0x58
                        angle = node.delta_rotation_euler[axis]
                        if math.fabs(angle) > EPSILON:
                            self.indent_write(b'Rotation (kind = "', 0, structFlag)
                            self.write(axisName[axis])
                            self.write(b'")\n')
                            self.indent_write(b"{\n")
                            self.indent_write(b"float {", 1)
                            self.write_float(angle)
                            self.write(b"}")
                            self.indent_write(b"}\n", 0, True)

                            structFlag = True

            if rotationAnimated:
                # When the rotation is animated, write three separate Euler angle rotations
                # so they can be targeted by different tracks having different sets of keys.

                for i in range(3):
                    axis = ord(mode[2 - i]) - 0x58
                    angle = node.rotation_euler[axis]
                    if (rotAnimated[axis]) or (math.fabs(angle) > EPSILON):
                        self.indent_write(b"Rotation %", 0, structFlag)
                        self.write(subrotationName[axis])
                        self.write(b' (kind = "')
                        self.write(axisName[axis])
                        self.write(b'")\n')
                        self.indent_write(b"{\n")
                        self.indent_write(b"float {", 1)
                        self.write_float(angle)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

            else:
                # When the rotation is not animated, write it in the representation given by
                # the node's current rotation mode.

                if mode == "QUATERNION":
                    quaternion = node.rotation_quaternion
                    if (
                        (math.fabs(quaternion[0] - 1.0) > EPSILON)
                        or (math.fabs(quaternion[1]) > EPSILON)
                        or (math.fabs(quaternion[2]) > EPSILON)
                        or (math.fabs(quaternion[3]) > EPSILON)
                    ):
                        self.indent_write(
                            b'Rotation (kind = "quaternion")\n', 0, structFlag
                        )
                        self.indent_write(b"{\n")
                        self.indent_write(b"float[4] {", 1)
                        self.write_quaternion(quaternion)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

                elif mode == "AXIS_ANGLE":
                    if math.fabs(node.rotation_axis_angle[0]) > EPSILON:
                        self.indent_write(b'Rotation (kind = "axis")\n', 0, structFlag)
                        self.indent_write(b"{\n")
                        self.indent_write(b"float[4] {", 1)
                        self.write_vector_4d(node.rotation_axis_angle)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

                else:
                    for i in range(3):
                        axis = ord(mode[2 - i]) - 0x58
                        angle = node.rotation_euler[axis]
                        if math.fabs(angle) > EPSILON:
                            self.indent_write(b'Rotation (kind = "', 0, structFlag)
                            self.write(axisName[axis])
                            self.write(b'")\n')
                            self.indent_write(b"{\n")
                            self.indent_write(b"float {", 1)
                            self.write_float(angle)
                            self.write(b"}")
                            self.indent_write(b"}\n", 0, True)

                            structFlag = True

            deltaScale = node.delta_scale
            if deltaScaleAnimated:
                # When the delta scale is animated, write the x, y, and z components separately
                # so they can be targeted by different tracks having different sets of keys.

                for i in range(3):
                    scl = deltaScale[i]
                    if (deltaSclAnimated[i]) or (math.fabs(scl) > EPSILON):
                        self.indent_write(b"Scale %", 0, structFlag)
                        self.write(deltaSubscaleName[i])
                        self.write(b' (kind = "')
                        self.write(axisName[i])
                        self.write(b'")\n')
                        self.indent_write(b"{\n")
                        self.indent_write(b"float {", 1)
                        self.write_float(scl)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

            elif (
                (math.fabs(deltaScale[0] - 1.0) > EPSILON)
                or (math.fabs(deltaScale[1] - 1.0) > EPSILON)
                or (math.fabs(deltaScale[2] - 1.0) > EPSILON)
            ):
                self.indent_write(b"Scale\n", 0, structFlag)
                self.indent_write(b"{\n")
                self.indent_write(b"float[3] {", 1)
                self.write_vector_3d(deltaScale)
                self.write(b"}")
                self.indent_write(b"}\n", 0, True)

                structFlag = True

            scale = node.scale
            if scaleAnimated:
                # When the scale is animated, write the x, y, and z components separately
                # so they can be targeted by different tracks having different sets of keys.

                for i in range(3):
                    scl = scale[i]
                    if (sclAnimated[i]) or (math.fabs(scl) > EPSILON):
                        self.indent_write(b"Scale %", 0, structFlag)
                        self.write(subscaleName[i])
                        self.write(b' (kind = "')
                        self.write(axisName[i])
                        self.write(b'")\n')
                        self.indent_write(b"{\n")
                        self.indent_write(b"float {", 1)
                        self.write_float(scl)
                        self.write(b"}")
                        self.indent_write(b"}\n", 0, True)

                        structFlag = True

            elif (
                (math.fabs(scale[0] - 1.0) > EPSILON)
                or (math.fabs(scale[1] - 1.0) > EPSILON)
                or (math.fabs(scale[2] - 1.0) > EPSILON)
            ):
                self.indent_write(b"Scale\n", 0, structFlag)
                self.indent_write(b"{\n")
                self.indent_write(b"float[3] {", 1)
                self.write_vector_3d(scale)
                self.write(b"}")
                self.indent_write(b"}\n", 0, True)

                structFlag = True

            # Export the animation tracks.

            self.indent_write(b"Animation (begin = ", 0, True)
            self.write_float((action.frame_range[0] - self.beginFrame) * self.frameTime)
            self.write(b", end = ")
            self.write_float((action.frame_range[1] - self.beginFrame) * self.frameTime)
            self.write(b")\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            structFlag = False

            if positionAnimated:
                for i in range(3):
                    if posAnimated[i]:
                        self.ExportAnimationTrack(
                            posAnimCurve[i],
                            posAnimKind[i],
                            subtranslationName[i],
                            structFlag,
                        )
                        structFlag = True

            if rotationAnimated:
                for i in range(3):
                    if rotAnimated[i]:
                        self.ExportAnimationTrack(
                            rotAnimCurve[i],
                            rotAnimKind[i],
                            subrotationName[i],
                            structFlag,
                        )
                        structFlag = True

            if scaleAnimated:
                for i in range(3):
                    if sclAnimated[i]:
                        self.ExportAnimationTrack(
                            sclAnimCurve[i], sclAnimKind[i], subscaleName[i], structFlag
                        )
                        structFlag = True

            if deltaPositionAnimated:
                for i in range(3):
                    if deltaPosAnimated[i]:
                        self.ExportAnimationTrack(
                            deltaPosAnimCurve[i],
                            deltaPosAnimKind[i],
                            deltaSubtranslationName[i],
                            structFlag,
                        )
                        structFlag = True

            if deltaRotationAnimated:
                for i in range(3):
                    if deltaRotAnimated[i]:
                        self.ExportAnimationTrack(
                            deltaRotAnimCurve[i],
                            deltaRotAnimKind[i],
                            deltaSubrotationName[i],
                            structFlag,
                        )
                        structFlag = True

            if deltaScaleAnimated:
                for i in range(3):
                    if deltaSclAnimated[i]:
                        self.ExportAnimationTrack(
                            deltaSclAnimCurve[i],
                            deltaSclAnimKind[i],
                            deltaSubscaleName[i],
                            structFlag,
                        )
                        structFlag = True

            self.indentLevel -= 1
            self.indent_write(b"}\n")

    # Replacement for
    #   node.to_mesh(scene, applyModifiers, "RENDER", True, False)
    # TODO: handle other params
    def GetMesh(self, node, scene, applyModifiers):
        if applyModifiers:
            depsgraph = self.ctx.evaluated_depsgraph_get()
            node = node.evaluated_get(depsgraph)

        return node.to_mesh()

    def export_bone_transform(
        self, armature: bpy.types.Object, bone: bpy.types.Bone, scene: bpy.types.Scene
    ):
        curveArray = self.CollectBoneAnimation(armature, bone.name)
        animation = (len(curveArray) != 0) or (self.sampleAnimationFlag)

        transform = bone.matrix_local.copy()
        parentBone = bone.parent

        if parentBone:
            transform = parentBone.matrix_local.inverted_safe() @ transform

        pose_bone = armature.pose.bones.get(bone.name)

        if pose_bone:
            print(pose_bone)
            transform = pose_bone.matrix.copy()
            pose_bone_parent = pose_bone.parent

            if pose_bone_parent:
                transform = pose_bone_parent.matrix.inverted_safe() @ transform

        # transform bone matrix to include parent object tranforms
        # transform = armature.matrix_world @ transform

        self.indent_write(b"Transform")

        if animation:
            self.write(b" %transform")

        self.indent_write(b"{\n", 0, True)
        self.indentLevel += 1

        self.indent_write(b"float[16]\n")
        self.indent_write(b"{\n")
        self.write_matrix(transform)
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n")

        if animation and pose_bone:
            self.ExportBoneSampledAnimation(pose_bone, scene)

    def ExportMaterialRef(self, material, index):
        if not material in self.materialArray:
            self.materialArray[material] = {
                "structName": bytes(
                    "material" + str(len(self.materialArray) + 1), "UTF-8"
                )
            }

        self.indent_write(b"MaterialRef (index = ")
        self.write_int(index)
        self.write(b") {ref {$")
        self.write(self.materialArray[material]["structName"])
        self.write(b"}}\n")

    def ExportMorphWeights(self, node, shapeKeys, scene):
        action = None
        curveArray = []
        indexArray = []

        if shapeKeys.animation_data:
            action = shapeKeys.animation_data.action
            if action:
                for fcurve in action.fcurves:
                    if (fcurve.data_path.startswith("key_blocks[")) and (
                        fcurve.data_path.endswith("].value")
                    ):
                        keyName = fcurve.data_path.strip("abcdehklopstuvy[]_.")
                        if (keyName[0] == '"') or (keyName[0] == "'"):
                            index = shapeKeys.key_blocks.find(keyName.strip("\"'"))
                            if index >= 0:
                                curveArray.append(fcurve)
                                indexArray.append(index)
                        else:
                            curveArray.append(fcurve)
                            indexArray.append(int(keyName))

        if (not action) and (node.animation_data):
            action = node.animation_data.action
            if action:
                for fcurve in action.fcurves:
                    if (
                        fcurve.data_path.startswith("data.shape_keys.key_blocks[")
                    ) and (fcurve.data_path.endswith("].value")):
                        keyName = fcurve.data_path.strip("abcdehklopstuvy[]_.")
                        if (keyName[0] == '"') or (keyName[0] == "'"):
                            index = shapeKeys.key_blocks.find(keyName.strip("\"'"))
                            if index >= 0:
                                curveArray.append(fcurve)
                                indexArray.append(index)
                        else:
                            curveArray.append(fcurve)
                            indexArray.append(int(keyName))

        animated = len(curveArray) != 0
        referenceName = shapeKeys.reference_key.name if (shapeKeys.use_relative) else ""

        for k in range(len(shapeKeys.key_blocks)):
            self.indent_write(b"MorphWeight", 0, (k == 0))

            if animated:
                self.write(b" %mw")
                self.write_int(k)

            self.write(b" (index = ")
            self.write_int(k)
            self.write(b") {float {")

            block = shapeKeys.key_blocks[k]
            self.write_float(block.value if (block.name != referenceName) else 1.0)

            self.write(b"}}\n")

        if animated:
            self.indent_write(b"Animation (begin = ", 0, True)
            self.write_float((action.frame_range[0] - self.beginFrame) * self.frameTime)
            self.write(b", end = ")
            self.write_float((action.frame_range[1] - self.beginFrame) * self.frameTime)
            self.write(b")\n")
            self.indent_write(b"{\n")
            self.indentLevel += 1

            structFlag = False

            for a in range(len(curveArray)):
                k = indexArray[a]
                target = bytes("mw" + str(k), "UTF-8")

                fcurve = curveArray[a]
                kind = OpenGexExporter.ClassifyAnimationCurve(fcurve)
                if (kind != ANIMATION_SAMPLED) and (not self.sampleAnimationFlag):
                    self.ExportAnimationTrack(fcurve, kind, target, structFlag)
                else:
                    self.ExportMorphWeightSampledAnimationTrack(
                        shapeKeys.key_blocks[k], target, scene, structFlag
                    )

                structFlag = True

            self.indentLevel -= 1
            self.indent_write(b"}\n")

    def export_bone(self, armature, bone, scene):
        node_ref = self.nodeArray.get(bone)

        if node_ref:
            self.indent_write(structIdentifier[node_ref["nodeType"]], 0, True)
            self.write(node_ref["structName"])

            self.indent_write(b"{\n", 0, True)
            self.indentLevel += 1

            name = bone.name
            if name != "":
                self.indent_write(b'Name {string {"')
                self.write(bytes(name, "UTF-8"))
                self.write(b'"}}\n\n')

            self.export_bone_transform(armature, bone, scene)

        for subnode in bone.children:
            self.export_bone(armature, subnode, scene)

        # Export any ordinary nodes that are parented to this bone.

        boneSubnodeArray = self.boneParentArray.get(bone.name)
        if boneSubnodeArray:
            poseBone = None
            if not bone.use_relative_parent:
                poseBone = armature.pose.bones.get(bone.name)

            for subnode in boneSubnodeArray:
                self.export_node(subnode, scene, poseBone)

        if node_ref:
            self.indentLevel -= 1
            self.indent_write(b"}\n")

    def export_node(self, node, scene, poseBone=None):
        # This function exports a single node in the scene and includes its name,
        # object reference, material references (for geometries), and transform.
        # Subnodes are then exported recursively.

        node_ref = self.nodeArray.get(node)

        if node_ref:
            node_type = node_ref["nodeType"]
            self.indent_write(structIdentifier[node_type], 0, True)
            self.write(node_ref["structName"])

            if node_type == NODETYPE_GEO:
                if node.hide_render:
                    self.write(b" (visible = false)")

            self.indent_write(b"{\n", 0, True)
            self.indentLevel += 1

            structFlag = False

            # Export the node's name if it has one.

            name = node.name
            if name != "":
                self.indent_write(b'Name {string {"')
                self.write(bytes(name, "UTF-8"))
                self.write(b'"}}\n')
                structFlag = True

            # Export the object reference and material references.

            object = node.data

            if node_type == NODETYPE_GEO:
                print(node_ref)

                if object not in self.geometryArray:
                    # Attempt to sanitize name
                    geomName = object.name.replace(" ", "_")
                    geomName = geomName.replace(".", "_").lower()

                    self.geometryArray[object] = {
                        "structName": bytes(geomName, "UTF-8"),
                        "nodeTable": [node],
                    }
                else:
                    self.geometryArray[object]["nodeTable"].append(node)

                self.indent_write(b"ObjectRef {ref {$")
                self.write(self.geometryArray[object]["structName"])
                self.write(b"}}\n")

                if self.option_export_materials:
                    for i in range(len(node.material_slots)):
                        self.ExportMaterialRef(node.material_slots[i].material, i)

                shapeKeys = OpenGexExporter.get_shape_keys(object)
                if shapeKeys:
                    self.ExportMorphWeights(node, shapeKeys, scene)

                structFlag = True

            elif node_type == NODETYPE_LIGHT:
                if not object in self.lightArray:
                    self.lightArray[object] = {
                        "structName": bytes(
                            "light" + str(len(self.lightArray) + 1), "UTF-8"
                        ),
                        "nodeTable": [node],
                    }
                else:
                    self.lightArray[object]["nodeTable"].append(node)

                self.indent_write(b"ObjectRef {ref {$")
                self.write(self.lightArray[object]["structName"])
                self.write(b"}}\n")
                structFlag = True

            elif node_type == NODETYPE_CAMERA:
                if not object in self.cameraArray:
                    self.cameraArray[object] = {
                        "structName": bytes(
                            "camera" + str(len(self.cameraArray) + 1), "UTF-8"
                        ),
                        "nodeTable": [node],
                    }
                else:
                    self.cameraArray[object]["nodeTable"].append(node)

                self.indent_write(b"ObjectRef {ref {$")
                self.write(self.cameraArray[object]["structName"])
                self.write(b"}}\n")
                structFlag = True

            if structFlag:
                self.write(b"\n")

            if poseBone:
                # If the node is parented to a bone and is not relative, then undo the bone's transform.

                if math.fabs(poseBone.matrix.determinant()) > EPSILON:
                    self.indent_write(b"Transform\n")
                    self.indent_write(b"{\n")
                    self.indentLevel += 1

                    self.indent_write(b"float[16]\n")
                    self.indent_write(b"{\n")
                    self.write_matrix(poseBone.matrix.inverted())
                    self.indent_write(b"}\n")

                    self.indentLevel -= 1
                    self.indent_write(b"}\n\n")

            # Export the transform. If the node is animated, then animation tracks are exported here.

            self.ExportNodeTransform(node, scene)

            if node.type == "ARMATURE":
                skeleton = node.data
                if skeleton:
                    for bone in skeleton.bones:
                        if not bone.parent:
                            self.export_bone(node, bone, scene)

        for subnode in node.children:
            if subnode.parent_type != "BONE":
                self.export_node(subnode, scene)

        if node_ref:
            self.indentLevel -= 1
            self.indent_write(b"}\n")

    def ExportSkin(self, node, armature, exportVertexArray):
        # This function exports all skinning data, which includes the skeleton
        # and per-vertex bone influence data.

        self.indent_write(b"Skin\n", 0, True)
        self.indent_write(b"{\n")
        self.indentLevel += 1

        # Write the skin bind pose transform.

        self.indent_write(b"Transform\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b"float[16]\n")
        self.indent_write(b"{\n")

        # An identity matrix is used here because it causes
        # problems in some engines otherwise.
        self.write_matrix(Matrix())
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n\n")

        # Export the skeleton, which includes an array of bone node references
        # and and array of per-bone bind pose transforms.

        self.indent_write(b"Skeleton\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        # Write the bone node reference array.

        self.indent_write(b"BoneRefArray\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        boneArray = armature.data.bones
        boneCount = len(boneArray)

        self.indent_write(b"ref\t\t\t// ")
        self.write_int(boneCount)
        self.indent_write(b"{\n", 0, True)
        self.indent_write(b"", 1)

        for i in range(boneCount):
            boneRef = self.find_node(boneArray[i].name)
            if boneRef:
                self.write(b"$")
                self.write(boneRef[1]["structName"])
            else:
                self.write(b"null")

            if i < boneCount - 1:
                self.write(b", ")
            else:
                self.write(b"\n")

        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n\n")

        # Write the bind pose transform array.

        self.indent_write(b"Transform\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b"float[16]\t// ")
        self.write_int(boneCount)
        self.indent_write(b"{\n", 0, True)

        for i in range(boneCount):
            self.write_matrix_flat(armature.matrix_world @ boneArray[i].matrix_local)
            if i < boneCount - 1:
                self.write(b",\n")

        self.indent_write(b"}\n", 0, True)

        self.indentLevel -= 1
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n\n")

        # Export the per-vertex bone influence data.

        groupRemap = []

        for group in node.vertex_groups:
            groupName = group.name
            for i in range(boneCount):
                if boneArray[i].name == groupName:
                    groupRemap.append(i)
                    break
            else:
                groupRemap.append(-1)

        boneCountArray = []
        boneIndexArray = []
        boneWeightArray = []

        meshVertexArray = node.data.vertices
        for ev in exportVertexArray:
            boneCount = 0
            totalWeight = 0.0
            for element in meshVertexArray[ev.vertexIndex].groups:
                boneIndex = groupRemap[element.group]
                boneWeight = element.weight
                if (boneIndex >= 0) and (boneWeight != 0.0):
                    boneCount += 1
                    totalWeight += boneWeight
                    boneIndexArray.append(boneIndex)
                    boneWeightArray.append(boneWeight)
            boneCountArray.append(boneCount)

            if totalWeight != 0.0:
                normalizer = 1.0 / totalWeight
                for i in range(-boneCount, 0):
                    boneWeightArray[i] *= normalizer

        # Write the bone count array. There is one entry per vertex.

        self.indent_write(b"BoneCountArray\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b"unsigned_int16\t\t// ")
        self.write_int(len(boneCountArray))
        self.indent_write(b"{\n", 0, True)
        self.write_int_array(boneCountArray)
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n\n")

        # Write the bone index array. The number of entries is the sum of the bone counts for all vertices.

        self.indent_write(b"BoneIndexArray\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b"unsigned_int16\t\t// ")
        self.write_int(len(boneIndexArray))
        self.indent_write(b"{\n", 0, True)
        self.write_int_array(boneIndexArray)
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n\n")

        # Write the bone weight array. The number of entries is the sum of the bone counts for all vertices.

        self.indent_write(b"BoneWeightArray\n")
        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b"float\t\t// ")
        self.write_int(len(boneWeightArray))
        self.indent_write(b"{\n", 0, True)
        self.write_float_array(boneWeightArray)
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n")

    def ExportGeometry(self, objectRef, scene):
        # This function exports a single geometry object.

        self.write(b"\nGeometryObject $")
        self.write(objectRef[1]["structName"])
        self.write_node_table(objectRef)

        self.write(b"\n{\n")
        self.indentLevel += 1

        node = objectRef[1]["nodeTable"][0]
        mesh = objectRef[0]

        structFlag = False
        # Save the morph state if necessary.

        activeShapeKeyIndex = node.active_shape_key_index
        showOnlyShapeKey = node.show_only_shape_key
        currentMorphValue = []

        shapeKeys = OpenGexExporter.get_shape_keys(mesh)
        if shapeKeys:
            node.active_shape_key_index = 0
            node.show_only_shape_key = True

            baseIndex = 0
            relative = shapeKeys.use_relative
            if relative:
                morphCount = 0
                baseName = shapeKeys.reference_key.name
                for block in shapeKeys.key_blocks:
                    if block.name == baseName:
                        baseIndex = morphCount
                        break
                    morphCount += 1

            morphCount = 0
            for block in shapeKeys.key_blocks:
                currentMorphValue.append(block.value)
                block.value = 0.0

                if block.name != "":
                    self.indent_write(b"Morph (index = ", 0, structFlag)
                    self.write_int(morphCount)

                    if (relative) and (morphCount != baseIndex):
                        self.write(b", base = ")
                        self.write_int(baseIndex)

                    self.write(b")\n")
                    self.indent_write(b"{\n")
                    self.indent_write(b'Name {string {"', 1)
                    self.write(bytes(block.name, "UTF-8"))
                    self.write(b'"}}\n')
                    self.indent_write(b"}\n")
                    structFlag = True

                morphCount += 1

            shapeKeys.key_blocks[0].value = 1.0
            mesh.update()

        self.indent_write(b'Mesh (primitive = "triangles")\n', 0, structFlag)
        self.indent_write(b"{\n")
        self.indentLevel += 1

        armature = node.find_armature()
        applyModifiers = not armature

        # Apply all modifiers to create a new mesh with tessfaces.

        # We don't apply modifiers for a skinned mesh because we need the vertex positions
        # before they are deformed by the armature modifier in order to export the proper
        # bind pose. This does mean that modifiers preceding the armature modifier are ignored,
        # but the Blender API does not provide a reasonable way to retrieve the mesh at an
        # arbitrary stage in the modifier stack.

        exportMesh = self.GetMesh(node, scene, applyModifiers)

        # Triangulate mesh and remap vertices to eliminate duplicates.

        materialTable = []
        exportVertexArray = OpenGexExporter.deindex_mesh(
            exportMesh, materialTable, self.option_export_vertex_colors
        )
        triangleCount = len(materialTable)

        indexTable = []
        unifiedVertexArray = OpenGexExporter.unify_vertices(
            exportVertexArray, indexTable
        )
        vertexCount = len(unifiedVertexArray)

        # Write the position array.

        self.indent_write(b'VertexArray (attrib = "position")\n')
        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b"float[3]\t\t// ")
        self.write_int(vertexCount)
        self.indent_write(b"{\n", 0, True)
        self.write_vertex_array_3d(unifiedVertexArray, "position")
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.indent_write(b"}\n\n")

        # Write the normal array.
        if self.option_export_normals:
            self.indent_write(b'VertexArray (attrib = "normal")\n')
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"float[3]\t\t// ")
            self.write_int(vertexCount)
            self.indent_write(b"{\n", 0, True)
            self.write_vertex_array_3d(unifiedVertexArray, "normal")
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

        # Write the color array if it exists.
        colorCount = len(exportMesh.vertex_colors)
        if colorCount > 0 and self.option_export_vertex_colors:
            self.indent_write(b'VertexArray (attrib = "color")\n', 0, True)
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"float[3]\t\t// ")
            self.write_int(vertexCount)
            self.indent_write(b"{\n", 0, True)
            self.write_vertex_array_3d(unifiedVertexArray, "color")
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

        # Write the texcoord arrays.
        if self.option_export_uvs:
            for uv_layer_index in range(len(mesh.uv_layers)):
                if uv_layer_index > 1:
                    break

                if uv_layer_index > 0:
                    attribSuffix = bytes(f"[{uv_layer_index}]", "UTF-8")
                else:
                    attribSuffix = b""

                self.indent_write(
                    b'VertexArray (attrib = "texcoord' + attribSuffix + b'")\n', 0, True
                )
                self.indent_write(b"{\n")
                self.indentLevel += 1

                self.indent_write(b"float[2]\t\t// ")
                self.write_int(vertexCount)
                self.indent_write(b"{\n", 0, True)
                self.write_vertex_array_2d(
                    unifiedVertexArray, "texcoord" + str(uv_layer_index)
                )
                self.indent_write(b"}\n")

                self.indentLevel -= 1
                self.indent_write(b"}\n")

        # Write morph targets.
        if shapeKeys:
            shapeKeys.key_blocks[0].value = 0.0
            for m in range(1, len(currentMorphValue)):
                shapeKeys.key_blocks[m].value = 1.0
                mesh.update()

                node.active_shape_key_index = m
                # morphMesh = node.to_mesh(scene, applyModifiers, "RENDER", True, False)
                morphMesh = self.GetMesh(node, scene, applyModifiers)
                morphMesh.calc_loop_triangles()

                # Write the morph target position array.

                self.indent_write(
                    b'VertexArray (attrib = "position", morph = ', 0, True
                )
                self.write_int(m)
                self.write(b")\n")
                self.indent_write(b"{\n")
                self.indentLevel += 1

                self.indent_write(b"float[3]\t\t// ")
                self.write_int(vertexCount)
                self.indent_write(b"{\n", 0, True)
                self.write_morph_position_array_3d(
                    unifiedVertexArray, morphMesh.vertices
                )
                self.indent_write(b"}\n")

                self.indentLevel -= 1
                self.indent_write(b"}\n\n")

                # Write the morph target normal array.

                self.indent_write(b'VertexArray (attrib = "normal", morph = ')
                self.write_int(m)
                self.write(b")\n")
                self.indent_write(b"{\n")
                self.indentLevel += 1

                self.indent_write(b"float[3]\t\t// ")
                self.write_int(vertexCount)
                self.indent_write(b"{\n", 0, True)
                self.write_morph_normal_array_3d(
                    unifiedVertexArray, morphMesh.vertices, morphMesh.loop_triangles
                )
                self.indent_write(b"}\n")

                self.indentLevel -= 1
                self.indent_write(b"}\n")

                # Delete morphMesh
                node.to_mesh_clear()

        # Write the index arrays.

        maxMaterialIndex = 0
        for i in range(len(materialTable)):
            index = materialTable[i]
            if index > maxMaterialIndex:
                maxMaterialIndex = index

        if maxMaterialIndex == 0:
            # There is only one material, so write a single index array.

            self.indent_write(b"IndexArray\n", 0, True)
            self.indent_write(b"{\n")
            self.indentLevel += 1

            self.indent_write(b"unsigned_int32[3]\t\t// ")
            self.write_int(triangleCount)
            self.indent_write(b"{\n", 0, True)
            self.write_triangle_array(triangleCount, indexTable)
            self.indent_write(b"}\n")

            self.indentLevel -= 1
            self.indent_write(b"}\n")

        else:
            # If there are multiple material indexes, then write a separate index array for each one.

            materialTriangleCount = [0 for i in range(maxMaterialIndex + 1)]
            for i in range(len(materialTable)):
                materialTriangleCount[materialTable[i]] += 1

            for m in range(maxMaterialIndex + 1):
                if materialTriangleCount[m] != 0:
                    materialIndexTable = []
                    for i in range(len(materialTable)):
                        if materialTable[i] == m:
                            k = i * 3
                            materialIndexTable.append(indexTable[k])
                            materialIndexTable.append(indexTable[k + 1])
                            materialIndexTable.append(indexTable[k + 2])

                    self.indent_write(b"IndexArray (material = ", 0, True)
                    self.write_int(m)
                    self.write(b")\n")
                    self.indent_write(b"{\n")
                    self.indentLevel += 1

                    self.indent_write(b"unsigned_int32[3]\t\t// ")
                    self.write_int(materialTriangleCount[m])
                    self.indent_write(b"{\n", 0, True)
                    self.write_triangle_array(
                        materialTriangleCount[m], materialIndexTable
                    )
                    self.indent_write(b"}\n")

                    self.indentLevel -= 1
                    self.indent_write(b"}\n")

        # If the mesh is skinned, export the skinning data here.

        if armature:
            self.ExportSkin(node, armature, unifiedVertexArray)

        # Restore the morph state.

        if shapeKeys:
            node.active_shape_key_index = activeShapeKeyIndex
            node.show_only_shape_key = showOnlyShapeKey

            for m in range(len(currentMorphValue)):
                shapeKeys.key_blocks[m].value = currentMorphValue[m]

            mesh.update()

        # Delete the new mesh that we made earlier.
        node.to_mesh_clear()

        self.indentLevel -= 1
        self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.write(b"}\n")

    def ExportLight(self, objectRef):
        # This function exports a single light object.
        self.write(b"\nLightObject $")
        self.write(objectRef[1]["structName"])

        object = objectRef[0]
        type = object.type

        self.write(b" (type = ")
        pointFlag = False
        spotFlag = False

        if type == "SUN":
            self.write(b'"infinite"')
        elif type == "POINT":
            self.write(b'"point"')
            pointFlag = True
        else:
            self.write(b'"spot"')
            pointFlag = True
            spotFlag = True

        if not object.use_shadow:
            self.write(b", shadow = false")

        self.write(b")")
        self.write_node_table(objectRef)

        self.write(b"\n{\n")
        self.indentLevel += 1

        # Export the light's color, and include a separate intensity if necessary.

        self.indent_write(b'Color (attrib = "light") {float[3] {')
        self.write_color(object.color)
        self.write(b"}}\n")

        intensity = object.energy
        if intensity != 1.0:
            self.indent_write(b'Param (attrib = "intensity") {float {')
            self.write_float(intensity)
            self.write(b"}}\n")

        if pointFlag:
            # Export a separate attenuation function for each type that's in use.

            falloff = object.falloff_type

            if falloff == "INVERSE_LINEAR":
                self.indent_write(b'Atten (curve = "inverse")\n', 0, True)
                self.indent_write(b"{\n")

                self.indent_write(b'Param (attrib = "scale") {float {', 1)
                self.write_float(object.distance)
                self.write(b"}}\n")

                self.indent_write(b"}\n")

            elif falloff == "INVERSE_SQUARE":
                self.indent_write(b'Atten (curve = "inverse_square")\n', 0, True)
                self.indent_write(b"{\n")

                self.indent_write(b'Param (attrib = "scale") {float {', 1)
                self.write_float(math.sqrt(object.distance))
                self.write(b"}}\n")

                self.indent_write(b"}\n")

            elif falloff == "LINEAR_QUADRATIC_WEIGHTED":
                if object.linear_attenuation != 0.0:
                    self.indent_write(b'Atten (curve = "inverse")\n', 0, True)
                    self.indent_write(b"{\n")

                    self.indent_write(b'Param (attrib = "scale") {float {', 1)
                    self.write_float(object.distance)
                    self.write(b"}}\n")

                    self.indent_write(b'Param (attrib = "constant") {float {', 1)
                    self.write_float(1.0)
                    self.write(b"}}\n")

                    self.indent_write(b'Param (attrib = "linear") {float {', 1)
                    self.write_float(object.linear_attenuation)
                    self.write(b"}}\n")

                    self.indent_write(b"}\n\n")

                if object.quadratic_attenuation != 0.0:
                    self.indent_write(b'Atten (curve = "inverse_square")\n')
                    self.indent_write(b"{\n")

                    self.indent_write(b'Param (attrib = "scale") {float {', 1)
                    self.write_float(object.distance)
                    self.write(b"}}\n")

                    self.indent_write(b'Param (attrib = "constant") {float {', 1)
                    self.write_float(1.0)
                    self.write(b"}}\n")

                    self.indent_write(b'Param (attrib = "quadratic") {float {', 1)
                    self.write_float(object.quadratic_attenuation)
                    self.write(b"}}\n")

                    self.indent_write(b"}\n")

            if VERSION[0] < 3 and (object.use_sphere):
                self.indent_write(b'Atten (curve = "linear")\n', 0, True)
                self.indent_write(b"{\n")

                self.indent_write(b'Param (attrib = "end") {float {', 1)
                self.write_float(object.distance)
                self.write(b"}}\n")

                self.indent_write(b"}\n")

            if spotFlag:
                # Export additional angular attenuation for spot lights.

                self.indent_write(
                    b'Atten (kind = "angle", curve = "linear")\n', 0, True
                )
                self.indent_write(b"{\n")

                endAngle = object.spot_size * 0.5
                beginAngle = endAngle * (1.0 - object.spot_blend)

                self.indent_write(b'Param (attrib = "begin") {float {', 1)
                self.write_float(beginAngle)
                self.write(b"}}\n")

                self.indent_write(b'Param (attrib = "end") {float {', 1)
                self.write_float(endAngle)
                self.write(b"}}\n")

                self.indent_write(b"}\n")

        self.indentLevel -= 1
        self.write(b"}\n")

    def ExportCamera(self, objectRef):
        # This function exports a single camera object.

        self.write(b"\nCameraObject $")
        self.write(objectRef[1]["structName"])
        self.write_node_table(objectRef)

        self.write(b"\n{\n")
        self.indentLevel += 1

        object = objectRef[0]

        self.indent_write(b'Param (attrib = "fov") {float {')
        self.write_float(object.angle_x)
        self.write(b"}}\n")

        self.indent_write(b'Param (attrib = "near") {float {')
        self.write_float(object.clip_start)
        self.write(b"}}\n")

        self.indent_write(b'Param (attrib = "far") {float {')
        self.write_float(object.clip_end)
        self.write(b"}}\n")

        self.indentLevel -= 1
        self.write(b"}\n")

    def ExportObjects(self, scene):
        for objectRef in self.geometryArray.items():
            self.ExportGeometry(objectRef, scene)
        for objectRef in self.lightArray.items():
            self.ExportLight(objectRef)
        for objectRef in self.cameraArray.items():
            self.ExportCamera(objectRef)

    def FindTextureInNodeTree(self, bsdf, channel):
        curr = bsdf.inputs[channel]

        while curr and curr.is_linked:
            node = curr.links[0].from_socket.node

            if node.type == "TEX_IMAGE":
                return node.image

            # Wasn't an image name, walk back links for now..
            curr = None
            backLinks = ["Color"]
            for backlinkName in backLinks:
                curr = node.inputs.get(backlinkName, None)
                break

        return None

    def FindNormalMapInNodeTree(self, bsdf, channel):
        curr = bsdf.inputs[channel]

        while curr and curr.is_linked:
            node = curr.links[0].from_socket.node

            if node.type == "TEX_IMAGE":
                return node.image

            # Wasn't an image name, walk back links for now..
            curr = None
            backLinks = ["Color", "Normal"]
            for backlinkName in backLinks:
                curr = node.inputs.get(backlinkName, None)
                break

        return None

    def export_image_node_texture(self, image: Image, attrib):
        
        filepath: str = self.filepath # type: ignore

        self.indent_write(b'Texture (attrib = "', 0, False)
        self.write(attrib)
        self.write(b'")\n')

        self.indent_write(b"{\n")
        self.indentLevel += 1

        self.indent_write(b'string {"')

        texture_path = Path(filepath).parent / "textures"

        if not texture_path.exists:
            print("creating texture export dir", texture_path.as_posix())
            texture_path.mkdir(exist_ok=True)

        image_name = Path(image.filepath).name

        image_path = texture_path / image_name

        print(f"saving image {image_path.as_posix()}")

        image.save(filepath=image_path.as_posix())

        if self.export_materials:
            prefix = "/Import/textures/"
        else:
            prefix = ""

        self.write_file_name(prefix + os.path.basename(image.filepath))

        self.write(b'"}\n')

        # TODO: look for a vector transform node and convert the scale/offsets

        self.indentLevel -= 1
        self.indent_write(b"}\n")

    def ExportMaterialParam(
        self, bsdf, blenderParamName, ogexParamName, propertyFlags, defaultValue=0.0
    ):
        channel = bsdf.inputs[blenderParamName]
        if not channel:
            return

        didWriteValue = False

        # Color and Param are exclusive, only should be present
        if MaterialPropertyFlags.PropertyColor in propertyFlags:
            if type(channel) == bpy.types.NodeSocketColor:
                color = tuple(channel.default_value)
            elif type(channel) == bpy.types.NodeSocketFloatFactor:
                value = channel.default_value
                color = (value, value, value)

            if (
                (color[0] != defaultValue)
                and (color[1] != defaultValue)
                and (color[2] != defaultValue)
            ):
                didWriteValue = True
                self.indent_write(
                    b'Color (attrib = "' + ogexParamName + b'") {float[3] {'
                )
                self.write_color(color)
                self.write(b"}}\n")

        elif MaterialPropertyFlags.PropertyParam in propertyFlags:
            if type(channel) == bpy.types.NodeSocketColor:
                value = channel.default_value[0]
            elif type(channel) == bpy.types.NodeSocketFloatFactor:
                value = channel.default_value

            if value != defaultValue:
                didWriteValue = True
                self.indent_write(b'Param (attrib = "' + ogexParamName + b'") {float {')
                self.write_float(value)
                self.write(b"}}\n")

        if MaterialPropertyFlags.PropertyTexture in propertyFlags:
            textureNode = self.FindTextureInNodeTree(bsdf, blenderParamName)
            if textureNode:
                self.export_image_node_texture(textureNode, ogexParamName)
                didWriteValue = True

        if didWriteValue:
            self.write(b"\n")

    def ExportNormalMap(self, bsdf):
        normalMap = self.FindNormalMapInNodeTree(bsdf, "Normal")
        if normalMap:
            self.export_image_node_texture(normalMap, b"normal")
            self.write(b"\n")

    def export_materials(self):
        # This function exports all of the materials used in the scene.
        if not self.option_export_materials:
            return

        for materialRef in self.materialArray.items():
            material = materialRef[0]

            self.write(b"\nMaterial $")
            self.write(materialRef[1]["structName"])
            self.write(b"\n{\n")
            self.indentLevel += 1

            if material.name != "":
                self.indent_write(b'Name {string {"')
                self.write(bytes(material.name, "UTF-8"))
                self.write(b'"}}\n\n')

            bsdf = None
            if material.node_tree:
                nodes = material.node_tree.nodes

                # This exporter requires you are using Principled BSDF. Might want to add a case to support Mix Shader
                # with an emission or transparency shader since that is a common setup.
                bsdf = nodes.get("Principled BSDF", None)

            if bsdf:
                # Shortcuts for common types of flags
                flagsColorOrTexture = (
                    MaterialPropertyFlags.PropertyColor,
                    MaterialPropertyFlags.PropertyTexture,
                )
                flagsParamOrTexture = (
                    MaterialPropertyFlags.PropertyParam,
                    MaterialPropertyFlags.PropertyTexture,
                )

                # See chart on Table 2.1 of OGEX spec for details of how these map
                self.ExportMaterialParam(
                    bsdf, "Base Color", b"diffuse", flagsColorOrTexture
                )

                # ***
                if VERSION[0] < 4:
                    specular_name = "Specular"
                    emission_name = "Emission"
                else:
                    specular_name = "Specular IOR Level"
                    emission_name = "Emission Color"

                self.ExportMaterialParam(
                    bsdf, specular_name, b"specular", flagsColorOrTexture
                )
                self.ExportMaterialParam(
                    bsdf, "Roughness", b"roughness", flagsParamOrTexture
                )
                self.ExportMaterialParam(
                    bsdf, "Metallic", b"metalness", flagsParamOrTexture
                )
                self.ExportMaterialParam(
                    bsdf, emission_name, b"emission", flagsColorOrTexture
                )
                self.ExportMaterialParam(
                    bsdf, "Alpha", b"opacity", flagsParamOrTexture, 1.0
                )
                self.ExportNormalMap(bsdf)

            self.indentLevel -= 1
            self.write(b"}\n")

    def ExportMetrics(self, scene):
        scale = scene.unit_settings.scale_length

        if scene.unit_settings.system == "IMPERIAL":
            scale *= 0.3048

        self.write(b'Metric (key = "distance") {float {')
        self.write_float(scale)
        self.write(b"}}\n")

        self.write(b'Metric (key = "angle") {float {1.0}}\n')
        self.write(b'Metric (key = "time") {float {1.0}}\n')
        self.write(b'Metric (key = "up") {string {"z"}}\n')

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

    def execute(self, context):
        self.file = WriteBuffer()

        self.indentLevel = 0

        self.nodeArray = {}
        self.geometryArray = {}
        self.lightArray = {}
        self.cameraArray = {}
        self.materialArray = {}
        self.boneParentArray = {}

        print("\nOpenGex export starting... %r" % self.filepath)  # type: ignore
        start_time = time.perf_counter()

        assert context
        self.ctx = context

        scene = self.ctx.scene
        self.ExportMetrics(scene)

        originalFrame = scene.frame_current
        originalSubframe = scene.frame_subframe
        self.restoreFrame = False

        self.beginFrame = scene.frame_start
        self.endFrame = scene.frame_end
        self.frameTime = 1.0 / (scene.render.fps_base * scene.render.fps)

        self.exportAllFlag = not self.option_export_selection
        self.sampleAnimationFlag = self.option_sample_animation

        if self.option_apply_transforms:
            for ob in scene.objects:
                if ob.type == "ARMATURE":
                    t = MatrixApplicator(ob)
                    t.execute()
                else:
                    self.select_and_make_active(ob)

                    # apply transforms
                    bpy.ops.object.transform_apply(
                        location=True, scale=True, rotation=True
                    )

        for object in scene.objects:
            if not object.parent:
                self.process_node(object)

        self.process_skinned_meshes()

        for object in scene.objects:
            if not object.parent:
                self.export_node(object, scene)

        self.ExportObjects(scene)
        self.export_materials()

        if self.restoreFrame:
            scene.frame_set(originalFrame, originalSubframe)

        self.file.write_to_file(self.filepath)  # type: ignore

        print("Export finished in %.4f sec." % (time.perf_counter() - start_time))
        return {"FINISHED"}


classes = (OpenGexPreferences, OpenGexExporter)


def menu_func(self, context):
    self.layout.operator(OpenGexExporter.bl_idname, text="OpenGEX (.ogex)")


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_export.append(menu_func)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func)


if __name__ == "__main__":
    register()
