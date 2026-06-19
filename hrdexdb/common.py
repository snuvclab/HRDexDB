from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import trimesh
from scipy.spatial.transform import Rotation as R, Slerp


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_ROOT = PROJECT_ROOT / "v0"
DEFAULT_MESH_ROOT = DEFAULT_DATASET_ROOT / "assets" / "mesh"
ASSET_ROOT = PROJECT_ROOT / "assets"
ROBOT_ASSET_ROOT = ASSET_ROOT / "robots"

HAND_ALIASES = {
    "hand": "human",
    "human": "human",
    "inspire_f1": "inspire_f1",
}

ROBOT_URDFS = {
    "allegro": ROBOT_ASSET_ROOT / "xarm_allegro.urdf",
    "allegro_v5": ROBOT_ASSET_ROOT / "allegro_v5" / "xarm_allegro_v5.urdf",
    "inspire": ROBOT_ASSET_ROOT / "xarm_inspire_DFTP.urdf",
    "inspire_f1": ROBOT_ASSET_ROOT / "xarm_inspire_f1_right.urdf",
}


@dataclass(frozen=True)
class EpisodePaths:
    dataset_root: Path
    hand: str
    hand_dir: str
    object_name: str
    scene: str
    episode_root: Path
    object_mesh: Path
    robot_urdf: Optional[Path]


def normalize_hand(hand: str) -> str:
    key = hand.strip()
    return HAND_ALIASES.get(key, key)


def resolve_hand_dir(dataset_root: Path, hand: str) -> Tuple[str, str]:
    hand_type = normalize_hand(hand)
    if hand == "hand":
        if (dataset_root / "hand").is_dir():
            return "human", "hand"
        if (dataset_root / "human").is_dir():
            return "human", "human"
    return hand_type, hand_type


def resolve_episode(
    dataset_root: str | Path,
    hand: str,
    *,
    object_name: str,
    scene: str | int,
    mesh_root: str | Path | None = None,
    object_mesh: str | Path | None = None,
    robot_urdf: str | Path | None = None,
) -> EpisodePaths:
    dataset_root = Path(dataset_root).expanduser().resolve()
    mesh_root_path = (
        Path(mesh_root).expanduser().resolve()
        if mesh_root is not None
        else dataset_root / "assets" / "mesh"
    )
    hand_type, hand_dir = resolve_hand_dir(dataset_root, hand)
    scene_name = str(scene)
    episode_root = dataset_root / hand_dir / object_name / scene_name
    if not episode_root.is_dir():
        raise FileNotFoundError(
            f"Scene directory not found: {episode_root}. "
            f"Expected layout: <dataset-root>/<hand>/<object>/<scene>."
        )

    mesh_path = (
        Path(object_mesh).expanduser().resolve()
        if object_mesh
        else mesh_root_path / object_name / f"{object_name}.obj"
    )
    if not mesh_path.exists():
        raise FileNotFoundError(f"Object mesh not found: {mesh_path}")

    urdf_path: Optional[Path]
    if robot_urdf:
        urdf_path = Path(robot_urdf).expanduser().resolve()
    elif hand_type == "human":
        urdf_path = None
    else:
        urdf_path = ROBOT_URDFS.get(hand_type)
    if urdf_path is not None and not urdf_path.exists():
        raise FileNotFoundError(f"Robot URDF not found: {urdf_path}")

    return EpisodePaths(
        dataset_root=dataset_root,
        hand=hand_type,
        hand_dir=hand_dir,
        object_name=object_name,
        scene=scene_name,
        episode_root=episode_root,
        object_mesh=mesh_path,
        robot_urdf=urdf_path,
    )


def load_json(path: str | Path) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def load_array(path: str | Path) -> np.ndarray:
    return np.asarray(np.load(path, allow_pickle=True), dtype=float)


def load_series(data_dir: str | Path, candidates: Sequence[str]) -> Tuple[np.ndarray, np.ndarray]:
    data_dir = Path(data_dir)
    for name in candidates:
        data_path = data_dir / name
        if not data_path.exists():
            continue
        data = load_array(data_path)
        time_candidates = [
            data_path.with_name(data_path.stem + "_time.npy"),
            data_dir / "time.npy",
        ]
        for time_path in time_candidates:
            if time_path.exists():
                times = load_array(time_path).reshape(-1)
                n = min(len(times), len(data))
                return data[:n], times[:n]
        return data, np.arange(len(data), dtype=float)
    raise FileNotFoundError(f"None of {tuple(candidates)} found in {data_dir}")


def resample_to(times_src: np.ndarray, data_src: np.ndarray, times_dst: np.ndarray) -> np.ndarray:
    times_src = np.asarray(times_src, dtype=float).reshape(-1)
    data_src = np.asarray(data_src, dtype=float)
    times_dst = np.asarray(times_dst, dtype=float).reshape(-1)
    if len(times_src) != len(data_src):
        n = min(len(times_src), len(data_src))
        times_src = times_src[:n]
        data_src = data_src[:n]
    if len(times_src) == 0:
        raise ValueError("Cannot resample an empty time series.")
    if len(times_src) == 1:
        return np.repeat(data_src[:1], len(times_dst), axis=0)

    order = np.argsort(times_src)
    times_src = times_src[order]
    data_src = data_src[order]
    flat = data_src.reshape(len(data_src), -1)
    out = np.stack(
        [np.interp(times_dst, times_src, flat[:, i]) for i in range(flat.shape[1])],
        axis=1,
    )
    return out.reshape((len(times_dst),) + data_src.shape[1:])


def resample_poses(poses: np.ndarray, target_len: int) -> np.ndarray:
    poses = np.asarray(poses, dtype=float)
    if len(poses) == target_len:
        return poses
    if len(poses) == 0:
        raise ValueError("Cannot resample an empty pose sequence.")
    if len(poses) == 1:
        return np.repeat(poses, target_len, axis=0)

    src_t = np.linspace(0.0, 1.0, len(poses))
    dst_t = np.linspace(0.0, 1.0, target_len)
    trans = np.stack([np.interp(dst_t, src_t, poses[:, i, 3]) for i in range(3)], axis=1)
    rots = RotationSafe.from_matrix_sequence(poses[:, :3, :3])
    slerp = Slerp(src_t, rots)
    out = np.tile(np.eye(4, dtype=float), (target_len, 1, 1))
    out[:, :3, :3] = slerp(dst_t).as_matrix()
    out[:, :3, 3] = trans
    return out


class RotationSafe:
    @staticmethod
    def from_matrix_sequence(mats: np.ndarray) -> R:
        fixed = []
        for mat in mats:
            u, _, vt = np.linalg.svd(mat)
            rot = u @ vt
            if np.linalg.det(rot) < 0:
                u[:, -1] *= -1.0
                rot = u @ vt
            fixed.append(rot)
        return R.from_matrix(np.stack(fixed, axis=0))


def load_camera_params(episode_root: str | Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, np.ndarray]]:
    cam_dir = Path(episode_root) / "cam_param"
    intr_raw = load_json(cam_dir / "intrinsics.json")
    extr_raw = load_json(cam_dir / "extrinsics.json")
    intrinsics: Dict[str, Dict[str, Any]] = {}
    extrinsics: Dict[str, np.ndarray] = {}
    for cam_id in sorted(set(intr_raw) & set(extr_raw)):
        payload = intr_raw[cam_id]
        K = np.asarray(payload.get("intrinsics_undistort", payload.get("original_intrinsics")), dtype=float)
        if K.shape != (3, 3):
            raise ValueError(f"{cam_id}: bad intrinsic shape {K.shape}")
        intrinsics[cam_id] = {
            **payload,
            "intrinsics_undistort": K,
            "width": int(payload["width"]),
            "height": int(payload["height"]),
        }
        ext = np.asarray(extr_raw[cam_id], dtype=float)
        if ext.shape == (3, 4):
            E = np.eye(4, dtype=float)
            E[:3, :] = ext
        elif ext.shape == (4, 4):
            E = ext
        else:
            raise ValueError(f"{cam_id}: bad extrinsic shape {ext.shape}")
        extrinsics[cam_id] = E
    return intrinsics, extrinsics


def load_ego_camera_params(episode_root: str | Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[int, np.ndarray]]]:
    cam_dir = Path(episode_root) / "cam_param"
    calib_path = None
    for name in ("ego_calib.json", "ego_calibration.json"):
        candidate = cam_dir / name
        if candidate.exists():
            calib_path = candidate
            break
    if calib_path is None:
        return {}, {}

    payload = load_json(calib_path)
    intr_raw = payload.get("intrinsics", {})
    extr_raw = payload.get("extrinsics", {})
    intrinsics: Dict[str, Dict[str, Any]] = {}
    extrinsics: Dict[str, Dict[int, np.ndarray]] = {}
    for cam_id in sorted(set(intr_raw) & set(extr_raw)):
        intr_payload = intr_raw[cam_id]
        K = np.asarray(
            intr_payload.get("intrinsics_undistort", intr_payload.get("original_intrinsics")),
            dtype=float,
        )
        if K.shape != (3, 3):
            raise ValueError(f"{cam_id}: bad ego intrinsic shape {K.shape}")
        intrinsics[cam_id] = {
            **intr_payload,
            "intrinsics_undistort": K,
            "width": int(intr_payload["width"]),
            "height": int(intr_payload["height"]),
        }

        cam_frames: Dict[int, np.ndarray] = {}
        for frame_key, raw_ext in extr_raw[cam_id].items():
            ext = np.asarray(raw_ext, dtype=float)
            if ext.shape == (3, 4):
                E = np.eye(4, dtype=float)
                E[:3, :] = ext
            elif ext.shape == (4, 4):
                E = ext
            else:
                raise ValueError(f"{cam_id}/{frame_key}: bad ego extrinsic shape {ext.shape}")
            cam_frames[int(frame_key)] = E
        if cam_frames:
            extrinsics[cam_id] = cam_frames
    return intrinsics, extrinsics


def load_c2r(episode_root: str | Path) -> np.ndarray:
    path = Path(episode_root) / "C2R.npy"
    if not path.exists():
        return np.eye(4, dtype=float)
    return load_array(path)


def load_video_timeline(episode_root: str | Path, fallback_len: int) -> Tuple[np.ndarray, np.ndarray]:
    ts_dir = Path(episode_root) / "raw" / "timestamps"
    ts_path = ts_dir / "timestamp.npy"
    frame_id_path = ts_dir / "frame_id.npy"
    if ts_path.exists() and frame_id_path.exists():
        ts = load_array(ts_path).reshape(-1)
        frame_ids = np.asarray(np.load(frame_id_path), dtype=int).reshape(-1)
        n = min(len(ts), len(frame_ids))
        return ts[:n], frame_ids[:n]
    return np.arange(fallback_len, dtype=float), np.arange(1, fallback_len + 1, dtype=int)


def object_pose_dir(episode_root: str | Path) -> Optional[Path]:
    episode_root = Path(episode_root)
    for name in ("object_6d", "object_tracking"):
        candidate = episode_root / name
        if candidate.is_dir():
            return candidate
    return None


def load_object_poses_robot(episode_root: str | Path, target_len: int) -> Optional[np.ndarray]:
    pose_dir = object_pose_dir(episode_root)
    if pose_dir is None:
        return None
    pose_paths = sorted(pose_dir.glob("pose_*.txt"))
    if not pose_paths:
        return None

    poses = []
    for path in pose_paths:
        arr = np.loadtxt(path, dtype=float)
        if arr.shape == (16,):
            arr = arr.reshape(4, 4)
        if arr.shape == (4, 4):
            poses.append(arr)
    if not poses:
        return None

    poses_world = resample_poses(np.stack(poses, axis=0), target_len)
    robot_from_world = np.linalg.inv(load_c2r(episode_root))
    return np.einsum("ij,tjk->tik", robot_from_world, poses_world)


def load_robot_qpos(episode_root: str | Path, hand: str) -> Tuple[np.ndarray, np.ndarray]:
    episode_root = Path(episode_root)
    arm_qpos, arm_time = load_series(
        episode_root / "raw" / "arm",
        ("position.npy", "action_qpos.npy", "action.npy"),
    )
    hand_dir = episode_root / "raw" / "hand"
    if hand == "inspire_f1":
        hand_raw, hand_time = load_series(hand_dir, ("right_joint_states.npy", "right_commands.npy"))
        hand_qpos = inspire_f1_action_to_qpos_dof6(hand_raw)
    elif hand == "inspire":
        hand_raw, hand_time = load_series(hand_dir, ("position.npy", "action.npy"))
        hand_qpos = inspire_action_to_qpos_dof6(hand_raw)
    elif hand in {"allegro", "allegro_v5"}:
        hand_qpos, hand_time = load_series(hand_dir, ("position.npy", "action.npy"))
    else:
        raise ValueError(f"Unsupported robot hand: {hand}")

    hand_qpos = resample_to(hand_time, hand_qpos, arm_time)
    n = min(len(arm_qpos), len(hand_qpos), len(arm_time))
    return np.concatenate([arm_qpos[:n], hand_qpos[:n]], axis=1), arm_time[:n]


def load_robot_qpos_on_video_timeline(
    episode_root: str | Path,
    hand: str,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    full_qpos, robot_time = load_robot_qpos(episode_root, hand)
    video_time, frame_ids = load_video_timeline(episode_root, len(full_qpos))
    qpos_video = resample_to(robot_time, full_qpos, video_time)
    return qpos_video, video_time, frame_ids


def load_human_mano_sequence(episode_root: str | Path) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    episode_root = Path(episode_root)
    candidates = (
        episode_root / "mano" / "mano",
        episode_root / "mano",
        episode_root / "hand" / "mano",
        episode_root / "hand" / "mano" / "mano",
    )
    mano_paths = []
    for candidate in candidates:
        if candidate.is_dir():
            mano_paths = sorted(candidate.glob("*.obj"))
            if mano_paths:
                break
    if not mano_paths:
        joined = ", ".join(str(path) for path in candidates)
        raise FileNotFoundError(f"No MANO OBJ files found. Checked: {joined}")

    vertices = []
    faces = None
    frame_ids = []
    for path in mano_paths:
        mesh = load_mesh(path)
        mesh_faces = np.asarray(mesh.faces, dtype=np.int32)
        if faces is None:
            faces = mesh_faces
        elif mesh_faces.shape != faces.shape or not np.array_equal(mesh_faces, faces):
            raise ValueError(f"MANO topology differs at {path}")
        vertices.append(np.asarray(mesh.vertices, dtype=float))
        frame_ids.append(int(path.stem))

    return (
        np.stack(vertices, axis=0),
        np.asarray(faces, dtype=np.int32),
        np.asarray(frame_ids, dtype=int),
        np.asarray(frame_ids, dtype=float),
    )


def inspire_action_to_qpos_dof6(action: np.ndarray) -> np.ndarray:
    action = np.asarray(action, dtype=float)
    qpos = np.zeros((action.shape[0], 6), dtype=float)
    qpos[:, 0] = 1.40 * (1.0 - action[:, 5] / 1000.0)
    qpos[:, 1] = 0.60 * (1.0 - action[:, 4] / 1000.0)
    qpos[:, 2] = (-4e-8 * action[:, 3] ** 3 + 3e-5 * action[:, 3] ** 2 - 0.0704 * action[:, 3] + 83.572) * np.pi / 180.0
    qpos[:, 3] = (-4e-8 * action[:, 2] ** 3 + 3e-5 * action[:, 2] ** 2 - 0.0704 * action[:, 2] + 83.572) * np.pi / 180.0
    qpos[:, 4] = (-4e-8 * action[:, 1] ** 3 + 3e-5 * action[:, 1] ** 2 - 0.0704 * action[:, 1] + 83.572) * np.pi / 180.0
    qpos[:, 5] = (-4e-8 * action[:, 0] ** 3 + 3e-5 * action[:, 0] ** 2 - 0.0704 * action[:, 0] + 83.572) * np.pi / 180.0
    return qpos


def inspire_f1_action_to_qpos_dof6(action: np.ndarray) -> np.ndarray:
    action = np.asarray(action, dtype=float)
    qpos = np.zeros((action.shape[0], 6), dtype=float)
    qpos[:, 0] = (1800.0 - action[:, 0]) * np.pi / 1800.0
    qpos[:, 1] = (1350.0 - action[:, 1]) * np.pi / 1800.0
    qpos[:, 2] = (1740.0 - action[:, 2]) * np.pi / 1800.0
    qpos[:, 3] = (1740.0 - action[:, 3]) * np.pi / 1800.0
    qpos[:, 4] = (1740.0 - action[:, 4]) * np.pi / 1800.0
    qpos[:, 5] = (1740.0 - action[:, 5]) * np.pi / 1800.0
    return qpos


def load_mesh(path: str | Path) -> trimesh.Trimesh:
    mesh = trimesh.load(path, force="mesh", process=False)
    if isinstance(mesh, trimesh.Scene):
        geoms = [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geoms:
            raise ValueError(f"No mesh geometry in {path}")
        mesh = trimesh.util.concatenate(geoms)
    if not isinstance(mesh, trimesh.Trimesh):
        raise TypeError(f"Unsupported mesh type from {path}: {type(mesh)}")
    return mesh


def find_video(episode_root_or_video_dir: str | Path, cam_id: str) -> Path:
    root = Path(episode_root_or_video_dir)
    if root.name in {"vid", "videos", "video_reduced"}:
        video_dirs = [root]
    else:
        video_dirs = [root / "vid", root / "videos", root / "video_reduced"]

    candidates = []
    for video_dir in video_dirs:
        if not video_dir.exists():
            continue
        for ext in ("avi", "mp4", "mov", "mkv"):
            candidates.extend(video_dir.glob(f"{cam_id}.{ext}"))
            candidates.extend(video_dir.glob(f"{cam_id}_*.{ext}"))
    if candidates:
        return sorted(candidates)[0]
    checked = ", ".join(str(path) for path in video_dirs)
    raise FileNotFoundError(f"No video found for camera {cam_id}. Checked: {checked}")


def load_video_frame(video_path: str | Path, frame_id: int, frame_offset: int = 0) -> Optional[np.ndarray]:
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return None
        frame_pos = max(0, int(frame_id) + int(frame_offset) - 1)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def apply_transform(points: np.ndarray, transform: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=float)
    return points @ transform[:3, :3].T + transform[:3, 3]


def parse_urdf_mesh_references(urdf_path: str | Path) -> List[Path]:
    urdf_path = Path(urdf_path)
    root = ET.parse(urdf_path).getroot()
    refs: List[Path] = []
    for mesh in root.findall(".//mesh"):
        filename = mesh.attrib.get("filename")
        if not filename:
            continue
        filename = re.sub(r"^package://[^/]+/", "", filename)
        refs.append((urdf_path.parent / filename).resolve())
    return refs
