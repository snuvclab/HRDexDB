from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Optional, Sequence

import numpy as np
import trimesh
from scipy.spatial.transform import Rotation as R

import viser
from viser.extras import ViserUrdf

from .common import (
    DEFAULT_DATASET_ROOT,
    load_c2r,
    load_camera_params,
    load_ego_camera_params,
    load_human_mano_sequence,
    load_mesh,
    load_object_poses_robot,
    load_robot_qpos_on_video_timeline,
    resolve_episode,
)


def rotation_wxyz(matrix: np.ndarray) -> np.ndarray:
    return R.from_matrix(np.array(matrix, dtype=float, copy=True)).as_quat()[[3, 0, 1, 2]]


class SimpleViserUrdf:
    def __init__(
        self,
        server: viser.ViserServer,
        urdf_path: Path,
        root: str = "/robot",
        root_transform: Optional[np.ndarray] = None,
    ):
        self.server = server
        if root_transform is None:
            root_transform = np.eye(4, dtype=float)
        server.scene.add_frame(
            root,
            position=root_transform[:3, 3],
            wxyz=rotation_wxyz(root_transform[:3, :3]),
            show_axes=False,
        )
        self.urdf = ViserUrdf(
            server,
            urdf_path,
            root_node_name=root,
            mesh_color_override=(180, 180, 180),
            load_meshes=True,
            load_collision_meshes=False,
        )
        self.joint_names = list(self.urdf.get_actuated_joint_limits())
        self.update(np.zeros(len(self.joint_names), dtype=float))

    def update(self, qpos: np.ndarray) -> None:
        qpos = np.asarray(qpos, dtype=float).reshape(-1)
        if len(qpos) < len(self.joint_names):
            qpos = np.pad(qpos, (0, len(self.joint_names) - len(qpos)))
        self.urdf.update_cfg(qpos[: len(self.joint_names)])


class SimpleManoSequence:
    def __init__(
        self,
        server: viser.ViserServer,
        vertices: np.ndarray,
        faces: np.ndarray,
        root: str = "/human/mano",
        root_transform: Optional[np.ndarray] = None,
    ):
        self.server = server
        self.vertices = np.asarray(vertices, dtype=float)
        self.faces = np.asarray(faces, dtype=np.int32)
        self.handle = None
        self.root = root
        parent = root.rsplit("/", 1)[0]
        if parent:
            if root_transform is None:
                root_transform = np.eye(4, dtype=float)
            server.scene.add_frame(
                parent,
                position=root_transform[:3, 3],
                wxyz=rotation_wxyz(root_transform[:3, :3]),
                show_axes=False,
            )
        self.update(0)

    def update(self, t: int) -> None:
        t = int(np.clip(t, 0, len(self.vertices) - 1))
        if self.handle is not None:
            self.handle.remove()
        self.handle = self.server.scene.add_mesh_simple(
            name=self.root,
            vertices=self.vertices[t],
            faces=self.faces,
            color=(150, 205, 230),
        )


def add_object(
    server: viser.ViserServer,
    name: str,
    mesh: trimesh.Trimesh,
    pose: np.ndarray,
    color_quantization: int = 32,
    root: str = "/objects",
):
    frame = server.scene.add_frame(
        f"{root}/{name}",
        position=pose[:3, 3],
        wxyz=rotation_wxyz(pose[:3, :3]),
        show_axes=False,
    )
    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    vertex_colors = getattr(mesh.visual, "vertex_colors", None)
    if vertex_colors is None or len(vertex_colors) != len(vertices):
        server.scene.add_mesh_simple(
            name=f"{root}/{name}/mesh",
            vertices=vertices,
            faces=faces.astype(np.uint32),
            color=(180, 90, 70),
            side="double",
        )
        return frame

    face_colors = np.asarray(vertex_colors[:, :3], dtype=np.float32)[faces].mean(axis=1)
    step = max(1, int(color_quantization))
    quantized = np.clip((face_colors // step) * step + step // 2, 0, 255).astype(np.uint8)
    palette, group_ids = np.unique(quantized, axis=0, return_inverse=True)
    for group_id, color in enumerate(palette):
        group_faces = faces[group_ids == group_id]
        if len(group_faces) == 0:
            continue
        used_vertices, inverse = np.unique(group_faces.reshape(-1), return_inverse=True)
        server.scene.add_mesh_simple(
            name=f"{root}/{name}/mesh/color_{group_id:03d}",
            vertices=vertices[used_vertices],
            faces=inverse.reshape(-1, 3).astype(np.uint32),
            color=tuple(int(x) for x in color),
            side="double",
        )
    return frame


def add_cameras(
    server: viser.ViserServer,
    episode_root: Path,
    c2r: np.ndarray,
    camera_ids: Optional[Sequence[str]],
    frustum_size: float,
    root: str = "/cameras",
) -> Dict[str, object]:
    intrinsics, extrinsics = load_camera_params(episode_root)
    selected = sorted(camera_ids) if camera_ids else sorted(intrinsics)
    out = {}
    for cam_id in selected:
        if cam_id not in intrinsics or cam_id not in extrinsics:
            continue
        cam_from_robot = extrinsics[cam_id] @ c2r
        robot_from_cam = np.linalg.inv(cam_from_robot)
        K = np.asarray(intrinsics[cam_id]["intrinsics_undistort"], dtype=float)
        h = int(intrinsics[cam_id]["height"])
        w = int(intrinsics[cam_id]["width"])
        fov = float(2.0 * np.arctan2(h * 0.5, K[1, 1]))
        frame_handle = server.scene.add_frame(
            f"{root}/{cam_id}",
            position=robot_from_cam[:3, 3],
            wxyz=rotation_wxyz(robot_from_cam[:3, :3]),
            show_axes=True,
            axes_length=frustum_size * 0.5,
            axes_radius=frustum_size * 0.015,
        )
        frustum = server.scene.add_camera_frustum(
            name=f"{root}/{cam_id}/frustum",
            fov=fov,
            aspect=float(w) / float(h),
            scale=frustum_size,
            color=(90, 90, 90),
            line_width=1.5,
        )
        out[cam_id] = (frame_handle, frustum)
    return out


class EgoCameraSequence:
    def __init__(
        self,
        server: viser.ViserServer,
        intrinsics: Dict[str, Dict[str, object]],
        extrinsics: Dict[str, Dict[int, np.ndarray]],
        c2r: np.ndarray,
        frame_ids: np.ndarray,
        camera_ids: Optional[Sequence[str]],
        frustum_size: float,
        root: str = "/ego_cameras",
    ):
        selected = sorted(camera_ids) if camera_ids else sorted(intrinsics)
        self.c2r = np.asarray(c2r, dtype=float)
        self.frame_ids = np.asarray(frame_ids, dtype=int).reshape(-1)
        self.extrinsics = {
            cam_id: frames
            for cam_id, frames in extrinsics.items()
            if cam_id in intrinsics and cam_id in selected
        }
        self.sorted_frame_ids = {
            cam_id: np.asarray(sorted(frames), dtype=int)
            for cam_id, frames in self.extrinsics.items()
        }
        self.handles = {}
        server.scene.add_frame(root, show_axes=False)

        for cam_id in sorted(self.extrinsics):
            pose = self._robot_from_cam(cam_id, int(self.frame_ids[0]))
            K = np.asarray(intrinsics[cam_id]["intrinsics_undistort"], dtype=float)
            h = int(intrinsics[cam_id]["height"])
            w = int(intrinsics[cam_id]["width"])
            fov = float(2.0 * np.arctan2(h * 0.5, K[1, 1]))
            frame_handle = server.scene.add_frame(
                f"{root}/{cam_id}",
                position=pose[:3, 3],
                wxyz=rotation_wxyz(pose[:3, :3]),
                show_axes=True,
                axes_length=frustum_size * 0.5,
                axes_radius=frustum_size * 0.015,
            )
            frustum = server.scene.add_camera_frustum(
                name=f"{root}/{cam_id}/frustum",
                fov=fov,
                aspect=float(w) / float(h),
                scale=frustum_size,
                color=(240, 185, 90),
                line_width=1.5,
            )
            self.handles[cam_id] = (frame_handle, frustum)

    def _nearest_frame_id(self, cam_id: str, frame_id: int) -> int:
        frames = self.sorted_frame_ids[cam_id]
        if frame_id in self.extrinsics[cam_id]:
            return frame_id
        idx = int(np.searchsorted(frames, frame_id))
        if idx <= 0:
            return int(frames[0])
        if idx >= len(frames):
            return int(frames[-1])
        before = int(frames[idx - 1])
        after = int(frames[idx])
        return before if abs(frame_id - before) <= abs(after - frame_id) else after

    def _robot_from_cam(self, cam_id: str, frame_id: int) -> np.ndarray:
        nearest = self._nearest_frame_id(cam_id, frame_id)
        cam_from_robot = self.extrinsics[cam_id][nearest] @ self.c2r
        return np.linalg.inv(cam_from_robot)

    def update(self, t: int) -> None:
        if not self.handles:
            return
        t = int(np.clip(t, 0, len(self.frame_ids) - 1))
        frame_id = int(self.frame_ids[t])
        for cam_id, (frame_handle, _frustum) in self.handles.items():
            pose = self._robot_from_cam(cam_id, frame_id)
            frame_handle.position = pose[:3, 3]
            frame_handle.wxyz = rotation_wxyz(pose[:3, :3])


def parse_camera_ids(value: str) -> Optional[list[str]]:
    if value.strip().lower() in {"", "all"}:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Visualize HRDexDB robot/human object trajectories in viser.")
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument(
        "--mesh-root",
        type=Path,
        default=None,
        help="Object mesh root. Defaults to <dataset-root>/assets/mesh.",
    )
    parser.add_argument("--hand", default="inspire_f1", help="Dataset hand split, e.g. inspire_f1, human, or hand.")
    parser.add_argument("--object", default="apple", dest="object_name")
    parser.add_argument("--scene", default="0")
    parser.add_argument("--object-mesh", type=Path, default=None)
    parser.add_argument("--robot-urdf", type=Path, default=None)
    parser.add_argument("--camera-ids", default="all", help="Comma-separated IDs, or 'all'.")
    parser.add_argument("--no-cameras", action="store_true")
    parser.add_argument("--no-object", action="store_true")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    ep = resolve_episode(
        args.dataset_root,
        args.hand,
        object_name=args.object_name,
        scene=args.scene,
        mesh_root=args.mesh_root,
        object_mesh=args.object_mesh,
        robot_urdf=args.robot_urdf,
    )
    c2r = load_c2r(ep.episode_root)
    robot_from_world = np.linalg.inv(c2r)

    mano = None
    robot = None
    ego_cameras = None
    if ep.hand == "human":
        mano_vertices, mano_faces, frame_ids, _video_time = load_human_mano_sequence(ep.episode_root)
        timeline_len = len(mano_vertices)
        qpos = None
    else:
        qpos, _video_time, frame_ids = load_robot_qpos_on_video_timeline(ep.episode_root, ep.hand)
        timeline_len = len(qpos)
    if timeline_len <= 0:
        raise ValueError(f"Empty trajectory: {ep.episode_root}")

    obj_poses = None if args.no_object else load_object_poses_robot(ep.episode_root, timeline_len)
    obj_mesh = None if args.no_object else load_mesh(ep.object_mesh)

    server_kwargs = {}
    if args.port is not None:
        server_kwargs["port"] = args.port
    server = viser.ViserServer(**server_kwargs)
    server.gui.configure_theme(dark_mode=True)
    server.scene.set_up_direction("+z")
    server.scene.add_grid("/floor", width=1.6, height=1.6, cell_size=0.1, plane="xy")

    if ep.hand == "human":
        mano = SimpleManoSequence(server, mano_vertices, mano_faces, root_transform=robot_from_world)
    else:
        if ep.robot_urdf is None:
            raise ValueError(f"No URDF configured for hand type: {ep.hand}")
        robot = SimpleViserUrdf(server, ep.robot_urdf)

    obj_frame = None
    if obj_poses is not None and obj_mesh is not None:
        obj_frame = add_object(server, ep.object_name, obj_mesh, obj_poses[0])

    if not args.no_cameras:
        selected_camera_ids = parse_camera_ids(args.camera_ids)
        try:
            add_cameras(
                server,
                ep.episode_root,
                c2r,
                selected_camera_ids,
                0.02,
            )
        except FileNotFoundError as exc:
            print(f"[warn] cameras skipped: {exc}")
        if ep.hand == "human":
            try:
                ego_intrinsics, ego_extrinsics = load_ego_camera_params(ep.episode_root)
                if ego_intrinsics and ego_extrinsics:
                    ego_cameras = EgoCameraSequence(
                        server,
                        ego_intrinsics,
                        ego_extrinsics,
                        c2r,
                        frame_ids,
                        selected_camera_ids,
                        0.02,
                    )
            except Exception as exc:
                print(f"[warn] ego cameras skipped: {exc}")

    with server.gui.add_folder("Playback"):
        timestep = server.gui.add_slider("Frame", min=0, max=timeline_len - 1, step=1, initial_value=0)
        playing = server.gui.add_checkbox("Playing", True)
        fps = server.gui.add_slider("FPS", min=1.0, max=60.0, step=1.0, initial_value=float(args.fps))

    def update_scene(t: int) -> None:
        t = int(np.clip(t, 0, timeline_len - 1))
        with server.atomic():
            if robot is not None and qpos is not None:
                robot.update(qpos[t])
            if mano is not None:
                mano.update(t)
            if ego_cameras is not None:
                ego_cameras.update(t)
            if obj_frame is not None and obj_poses is not None:
                obj_frame.position = obj_poses[t, :3, 3]
                obj_frame.wxyz = rotation_wxyz(obj_poses[t, :3, :3])

    @timestep.on_update
    def _(_) -> None:
        update_scene(timestep.value)

    update_scene(0)
    print(
        "viser server running "
        f"hand={ep.hand_dir} object={ep.object_name} scene={ep.scene} frames={timeline_len}. "
        "Open the URL printed by viser in a browser."
    )
    try:
        while True:
            if playing.value:
                timestep.value = (int(timestep.value) + 1) % timeline_len
            time.sleep(1.0 / max(float(fps.value), 1.0))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
