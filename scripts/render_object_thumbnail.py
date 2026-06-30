#!/usr/bin/env python3
"""Render a square object thumbnail from an OBJ mesh with Blender."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object", required=True, help="Object directory/name under mesh root.")
    parser.add_argument(
        "--mesh-root",
        default="/home/capture15/shared_data/mesh_blender",
        help="Directory containing per-object mesh folders.",
    )
    parser.add_argument("--mesh", default=None, help="Optional explicit OBJ path.")
    parser.add_argument(
        "--source-x-up",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Legacy shortcut for --up-axis +x.",
    )
    parser.add_argument(
        "--up-axis",
        choices=("+x", "-x", "+y", "-y", "+z", "-z"),
        default="+x",
        help="Source mesh axis to place along world vertical.",
    )
    parser.add_argument("--output", required=True, help="Output PNG path.")
    parser.add_argument("--resolution", type=int, default=768, help="Square render size in pixels.")
    parser.add_argument("--margin", type=float, default=1.75, help="Camera framing multiplier.")
    parser.add_argument(
        "--background-color",
        default="0.04,0.05,0.07",
        help="Comma-separated RGB values in 0..1 used to flatten the transparent render.",
    )
    parser.add_argument("--exposure", type=float, default=-1.35, help="Render exposure for bright/white meshes.")
    parser.add_argument("--foreground-gain", type=float, default=0.55, help="RGB multiplier applied before compositing.")
    parser.add_argument(
        "--camera-direction",
        nargs=3,
        type=float,
        default=(1.2, -1.7, 0.95),
        metavar=("X", "Y", "Z"),
        help="Orthographic camera direction vector.",
    )
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    return parser.parse_args(argv)


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def resolve_mesh(args: argparse.Namespace) -> Path:
    if args.mesh:
        mesh = Path(args.mesh)
    else:
        object_dir = Path(args.mesh_root) / args.object
        mesh = object_dir / f"{args.object}_viser.obj"
        if not mesh.exists():
            mesh = object_dir / f"{args.object}.obj"
    if not mesh.exists():
        raise FileNotFoundError(f"Mesh not found: {mesh}")
    return mesh


def import_obj(mesh_path: Path) -> list[bpy.types.Object]:
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=str(mesh_path))
    imported = [obj for obj in bpy.data.objects if obj not in before and obj.type == "MESH"]
    if not imported:
        raise RuntimeError(f"No mesh objects imported from {mesh_path}")
    return imported


def normalize_materials(meshes: list[bpy.types.Object]) -> None:
    for obj in meshes:
        for slot in obj.material_slots:
            material = slot.material
            if not material:
                continue
            material.use_nodes = True
            material.diffuse_color = (1.0, 1.0, 1.0, 1.0)
            bsdf = material.node_tree.nodes.get("Principled BSDF")
            if bsdf:
                bsdf.inputs["Roughness"].default_value = 0.58
                bsdf.inputs["Metallic"].default_value = 0.0


def scene_bounds(meshes: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    mins = Vector((min(point.x for point in points), min(point.y for point in points), min(point.z for point in points)))
    maxs = Vector((max(point.x for point in points), max(point.y for point in points), max(point.z for point in points)))
    return mins, maxs


def center_meshes(meshes: list[bpy.types.Object]) -> float:
    mins, maxs = scene_bounds(meshes)
    center = (mins + maxs) * 0.5
    for obj in meshes:
        obj.location -= center
    mins, maxs = scene_bounds(meshes)
    dimensions = maxs - mins
    return max(dimensions.x, dimensions.y, dimensions.z)


def source_axis_vector(axis: str) -> Vector:
    sign = -1.0 if axis.startswith("-") else 1.0
    name = axis[-1]
    if name == "x":
        return Vector((sign, 0.0, 0.0))
    if name == "y":
        return Vector((0.0, sign, 0.0))
    return Vector((0.0, 0.0, sign))


def orient_source_axis_up(meshes: list[bpy.types.Object], axis: str) -> None:
    rotation = source_axis_vector(axis).rotation_difference(Vector((0.0, 0.0, 1.0)))
    for obj in meshes:
        obj.rotation_euler.rotate(rotation)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        obj.select_set(False)


def place_on_floor(meshes: list[bpy.types.Object]) -> None:
    mins, _ = scene_bounds(meshes)
    for obj in meshes:
        obj.location.z -= mins.z


def look_at(camera: bpy.types.Object, target: Vector) -> None:
    direction = target - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def setup_camera(size: float, target: Vector, direction_values: tuple[float, float, float]) -> bpy.types.Object:
    direction = Vector(direction_values).normalized()
    camera_data = bpy.data.cameras.new("thumbnail_camera")
    camera = bpy.data.objects.new("thumbnail_camera", camera_data)
    bpy.context.collection.objects.link(camera)
    camera.location = target + direction * max(size * 4.0, 1.0)
    look_at(camera, target)
    camera_data.type = "ORTHO"
    bpy.context.scene.camera = camera
    return camera


def frame_camera(camera: bpy.types.Object, meshes: list[bpy.types.Object], margin: float) -> None:
    bpy.context.view_layer.update()
    camera_inverse = camera.matrix_world.inverted()
    points = [
        camera_inverse @ (obj.matrix_world @ Vector(corner))
        for obj in meshes
        for corner in obj.bound_box
    ]
    width = max(point.x for point in points) - min(point.x for point in points)
    height = max(point.y for point in points) - min(point.y for point in points)
    camera.data.ortho_scale = max(width, height) * margin


def setup_lighting(size: float) -> None:
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.color = (0.04, 0.05, 0.07)
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background:
        background.inputs["Color"].default_value = (0.04, 0.05, 0.07, 1.0)
        background.inputs["Strength"].default_value = 0.8

    key_data = bpy.data.lights.new("key_area", type="AREA")
    key_data.energy = 85
    key_data.size = max(size * 4.0, 1.0)
    key = bpy.data.objects.new("key_area", key_data)
    bpy.context.collection.objects.link(key)
    key.location = (size * 1.8, -size * 2.2, size * 2.4)

    fill_data = bpy.data.lights.new("fill_area", type="AREA")
    fill_data.energy = 18
    fill_data.size = max(size * 5.0, 1.0)
    fill = bpy.data.objects.new("fill_area", fill_data)
    bpy.context.collection.objects.link(fill)
    fill.location = (-size * 2.0, size * 1.8, size * 1.6)


def setup_render(output_path: Path, resolution: int, exposure: float) -> None:
    scene = bpy.context.scene
    engines = {item.identifier for item in scene.render.bl_rna.properties["engine"].enum_items}
    scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = 64
        if hasattr(scene.eevee, "use_gtao"):
            scene.eevee.use_gtao = True
        if hasattr(scene.eevee, "gtao_distance"):
            scene.eevee.gtao_distance = 3
        if hasattr(scene.eevee, "gtao_factor"):
            scene.eevee.gtao_factor = 1.5
    scene.render.resolution_x = resolution
    scene.render.resolution_y = resolution
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.filepath = str(output_path)
    scene.view_settings.view_transform = "Filmic" if "Filmic" in {item.identifier for item in scene.view_settings.bl_rna.properties["view_transform"].enum_items} else "Standard"
    scene.view_settings.look = "Medium High Contrast"
    scene.view_settings.exposure = exposure
    scene.view_settings.gamma = 1.0


def parse_background_color(value: str) -> tuple[float, float, float]:
    parts = [float(part.strip()) for part in value.split(",")]
    if len(parts) != 3:
        raise ValueError("--background-color must contain exactly three comma-separated RGB values")
    return tuple(max(0.0, min(1.0, part)) for part in parts)


def flatten_render(output_path: Path, background: tuple[float, float, float], foreground_gain: float) -> None:
    image = bpy.data.images.load(str(output_path))
    width, height = image.size
    pixels = list(image.pixels[:])
    flattened = bpy.data.images.new("thumbnail_flattened", width=width, height=height, alpha=True)

    alpha_pixels = [
        (index // 4) for index in range(0, len(pixels), 4) if pixels[index + 3] > 0.01
    ]
    if alpha_pixels:
        xs = [pixel_index % width for pixel_index in alpha_pixels]
        ys = [pixel_index // width for pixel_index in alpha_pixels]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        offset_x = (width - (max_x - min_x + 1)) // 2 - min_x
        offset_y = (height - (max_y - min_y + 1)) // 2 - min_y
    else:
        offset_x = 0
        offset_y = 0

    out = [0.0] * len(pixels)
    for y in range(height):
        for x in range(width):
            out_index = (y * width + x) * 4
            out[out_index : out_index + 4] = (background[0], background[1], background[2], 1.0)

    for src_y in range(height):
        for src_x in range(width):
            src_index = (src_y * width + src_x) * 4
            alpha = pixels[src_index + 3]
            if alpha <= 0.0:
                continue
            dst_x = src_x + offset_x
            dst_y = src_y + offset_y
            if not (0 <= dst_x < width and 0 <= dst_y < height):
                continue
            dst_index = (dst_y * width + dst_x) * 4
            out[dst_index : dst_index + 4] = (
                min(1.0, pixels[src_index] * foreground_gain) * alpha + background[0] * (1.0 - alpha),
                min(1.0, pixels[src_index + 1] * foreground_gain) * alpha + background[1] * (1.0 - alpha),
                min(1.0, pixels[src_index + 2] * foreground_gain) * alpha + background[2] * (1.0 - alpha),
                1.0,
            )
    flattened.pixels.foreach_set(out)
    flattened.filepath_raw = str(output_path)
    flattened.file_format = "PNG"
    flattened.save()


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    clear_scene()
    meshes = import_obj(resolve_mesh(args))
    normalize_materials(meshes)
    if args.source_x_up:
        orient_source_axis_up(meshes, args.up_axis)
    size = center_meshes(meshes)
    place_on_floor(meshes)
    mins, maxs = scene_bounds(meshes)
    target = (mins + maxs) * 0.5
    camera = setup_camera(size, target, args.camera_direction)
    frame_camera(camera, meshes, args.margin)
    setup_lighting(size)
    setup_render(output_path, args.resolution, args.exposure)
    bpy.ops.render.render(write_still=True)
    flatten_render(output_path, parse_background_color(args.background_color), args.foreground_gain)


if __name__ == "__main__":
    main()
