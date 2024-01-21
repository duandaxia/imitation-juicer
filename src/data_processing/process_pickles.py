import argparse
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List

import numpy as np
import zarr
from furniture_bench.robot.robot_state import filter_and_concat_robot_state
from numcodecs import Blosc, blosc
from tqdm import tqdm
from src.common.types import Trajectory
from src.common.files import get_processed_path, get_raw_paths
from src.visualization.render_mp4 import unpickle_data
from src.common.geometry import np_isaac_quat_to_rot_6d


# === Modified Function to Initialize Zarr Store with Full Dimensions ===
def initialize_zarr_store(out_path, full_data_shapes, chunksize=32):
    """
    Initialize the Zarr store with full dimensions for each dataset.
    """
    z = zarr.open(str(out_path), mode="w")
    z.attrs["time_created"] = datetime.now().astimezone().isoformat()

    # Define the compressor
    compressor = Blosc(cname="zstd", clevel=9, shuffle=Blosc.BITSHUFFLE)

    # Initialize datasets with full shapes
    for name, shape, dtype in full_data_shapes:
        if "color_image" in name:  # Apply compression to image data
            z.create_dataset(
                name,
                shape=shape,
                dtype=dtype,
                chunks=(chunksize,) + shape[1:],
                compressor=compressor,
            )
        else:
            z.create_dataset(
                name, shape=shape, dtype=dtype, chunks=(chunksize,) + shape[1:]
            )

    return z


def action_to_6d_rotation(action: np.ndarray) -> np.ndarray:
    """
    Convert the 8D action space to 10D action space.

    Parts:
        - 3D position
        - 4D quaternion rotation
        - 1D gripper

    Rotation 4D quaternion -> 6D vector represention
    """
    assert action.shape[1] == 8, "Action must be 8D"

    # Get each part of the action
    delta_pos = action[:, :3]
    delta_quat = action[:, 3:7]
    delta_gripper = action[:, 7:]

    # Convert quaternion to 6D rotation
    delta_rot = np_isaac_quat_to_rot_6d(delta_quat)

    # Concatenate all parts
    action_6d = np.concatenate([delta_pos, delta_rot, delta_gripper], axis=1)

    return action_6d


def proprioceptive_to_6d_rotation(robot_state: np.ndarray) -> np.ndarray:
    """
    Convert the 14D proprioceptive state space to 16D state space.

    Parts:
        - 3D position
        - 4D quaternion rotation
        - 3D linear velocity
        - 3D angular velocity
        - 1D gripper width

    Rotation 4D quaternion -> 6D vector represention
    """
    assert robot_state.shape[1] == 14, "Robot state must be 14D"

    # Get each part of the robot state
    pos = robot_state[:, :3]
    ori_quat = robot_state[:, 3:7]
    pos_vel = robot_state[:, 7:10]
    ori_vel = robot_state[:, 10:13]
    gripper = robot_state[:, 13:]

    # Convert quaternion to 6D rotation
    ori_6d = np_isaac_quat_to_rot_6d(ori_quat)

    # Concatenate all parts
    robot_state_6d = np.concatenate([pos, ori_6d, pos_vel, ori_vel, gripper], axis=1)

    return robot_state_6d


def extract_ee_pose_6d(robot_state: np.ndarray) -> np.ndarray:
    """
    Extract the end effector pose from the 6D robot state.
    """
    assert robot_state.shape[1] == 16, "Robot state must be 16D"

    # Get each part of the robot state
    pos = robot_state[:, :3]
    ori_6d = robot_state[:, 3:9]

    # Concatenate all parts
    ee_pose_6d = np.concatenate([pos, ori_6d], axis=1)

    return ee_pose_6d


def process_pickle_file(pickle_path: Path, noop_threshold: float):
    """
    Process a single pickle file and return processed data.
    """
    data: Trajectory = unpickle_data(pickle_path)
    obs = data["observations"]

    # Extract the observations from the pickle file and convert to 6D rotation
    color_image1 = np.array([o["color_image1"] for o in obs], dtype=np.uint8)[:-1]
    color_image2 = np.array([o["color_image2"] for o in obs], dtype=np.uint8)[:-1]
    all_robot_state = np.array(
        [filter_and_concat_robot_state(o["robot_state"]) for o in obs], dtype=np.float32
    )
    all_robot_state = proprioceptive_to_6d_rotation(all_robot_state)
    robot_state = all_robot_state[:-1]

    # Extract the delta actions from the pickle file and convert to 6D rotation
    action_delta = np.array(data["actions"], dtype=np.float32)
    action_delta = action_to_6d_rotation(action_delta)

    # Extract the position control actions from the pickle file
    action_pos = extract_ee_pose_6d(all_robot_state[1:])

    # Extract the rewards and skills from the pickle file
    reward = np.array(data["rewards"], dtype=np.float32)
    skill = np.array(data["skills"], dtype=np.float32)

    # Sanity check that all arrays are the same length
    assert len(robot_state) == len(
        action_delta
    ), f"Mismatch in {pickle_path}, lengths differ by {len(robot_state) - len(action_delta)}"

    # Extract the pickle file name as the path after `raw` in the path
    pickle_file = "/".join(pickle_path.parts[pickle_path.parts.index("raw") + 1 :])

    processed_data = {
        "robot_state": robot_state,
        "color_image1": color_image1,
        "color_image2": color_image2,
        "action/delta": action_delta,
        "action/pos": action_pos,
        "reward": reward,
        "skill": skill,
        "episode_length": len(action_delta),
        "furniture": data["furniture"],
        "success": data["success"],
        "pickle_file": pickle_file,
    }

    return processed_data


def parallel_process_pickle_files(pickle_paths, noop_threshold, num_threads):
    """
    Process all pickle files in parallel and aggregate results.
    """
    # Initialize empty data structures to hold aggregated data
    aggregated_data = {
        "robot_state": [],
        "color_image1": [],
        "color_image2": [],
        "action/delta": [],
        "action/pos": [],
        "reward": [],
        "skill": [],
        "episode_ends": [],
        "furniture": [],
        "success": [],
        "pickle_file": [],
    }

    # Process files in parallel
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [
            executor.submit(process_pickle_file, path, noop_threshold)
            for path in pickle_paths
        ]
        for future in tqdm(
            as_completed(futures), total=len(futures), desc="Processing files"
        ):
            data = future.result()
            # Aggregate data from each file
            for key in data:
                if key == "episode_length":
                    # Calculate and append to episode_ends
                    last_end = (
                        aggregated_data["episode_ends"][-1]
                        if len(aggregated_data["episode_ends"]) > 0
                        else 0
                    )
                    aggregated_data["episode_ends"].append(last_end + data[key])
                else:
                    aggregated_data[key].append(data[key])

    # Convert lists to numpy arrays for numerical data
    for key in [
        "robot_state",
        "color_image1",
        "color_image2",
        "action/delta",
        "action/pos",
        "reward",
        "skill",
    ]:
        aggregated_data[key] = np.concatenate(aggregated_data[key])

    return aggregated_data


def write_to_zarr_store(z, key, value):
    """
    Function to write data to a Zarr store.
    """
    z[key][:] = value


def parallel_write_to_zarr(z, aggregated_data, num_threads):
    """
    Write aggregated data to the Zarr store in parallel.
    """
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = []
        for key, value in aggregated_data.items():
            # Schedule the writing of each dataset
            futures.append(executor.submit(write_to_zarr_store, z, key, value))

        # Wait for all futures to complete and track progress
        for future in tqdm(
            as_completed(futures), total=len(futures), desc="Writing to Zarr store"
        ):
            future.result()


# === Entry Point of the Script ===
# ... (Your argument parsing code remains the same) ...

if __name__ == "__main__":
    # ... (Your argument parsing code remains the same) ...
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", "-e", type=str, nargs="+", default=None)
    parser.add_argument("--furniture", "-f", type=str, default=None, nargs="+")
    parser.add_argument(
        "--source",
        "-s",
        type=str,
        choices=["scripted", "rollout", "teleop"],
        default=None,
        nargs="+",
    )
    parser.add_argument(
        "--randomness",
        "-r",
        type=str,
        default=None,
        nargs="+",
    )
    parser.add_argument(
        "--demo-outcome",
        "-d",
        type=str,
        choices=["success", "failure"],
        default=None,
        nargs="+",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    pickle_paths: List[Path] = get_raw_paths(
        environment=args.env,
        task=args.furniture,
        demo_source=args.source,
        randomness=args.randomness,
        demo_outcome=args.demo_outcome,
    )

    print(f"Found {len(pickle_paths)} pickle files")

    output_path = get_processed_path(
        obs_type="image",
        environment=args.env,
        task=args.furniture,
        demo_source=args.source,
        randomness=args.randomness,
        demo_outcome=args.demo_outcome,
    )

    print(f"Output path: {output_path}")

    if output_path.exists() and not args.overwrite:
        raise ValueError(
            f"Output path already exists: {output_path}. Use --overwrite to overwrite."
        )

    # Process all pickle files
    chunksize = 1_000
    noop_threshold = 0.0
    n_cpus = os.cpu_count()

    all_data = parallel_process_pickle_files(pickle_paths, noop_threshold, n_cpus)

    # Define the full shapes for each dataset
    full_data_shapes = [
        # These are of length: number of timesteps
        ("robot_state", all_data["robot_state"].shape, np.float32),
        ("color_image1", all_data["color_image1"].shape, np.uint8),
        ("color_image2", all_data["color_image2"].shape, np.uint8),
        ("action/delta", all_data["action/delta"].shape, np.float32),
        ("action/pos", all_data["action/pos"].shape, np.float32),
        ("skill", all_data["skill"].shape, np.float32),
        ("reward", all_data["reward"].shape, np.float32),
        # These are of length: number of episodes
        ("episode_ends", (len(all_data["episode_ends"]),), np.uint32),
        ("furniture", (len(all_data["furniture"]),), str),
        ("success", (len(all_data["success"]),), np.uint8),
        ("pickle_file", (len(all_data["pickle_file"]),), str),
    ]

    # Initialize Zarr store with full dimensions
    z = initialize_zarr_store(output_path, full_data_shapes, chunksize=chunksize)

    blosc.use_threads = True
    blosc.set_nthreads(n_cpus)

    # Write the data to the Zarr store
    it = tqdm(all_data)
    for name in it:
        it.set_description(f"Writing data to zarr: {name}")
        z[name][:] = all_data[name]

    # Update final metadata
    z.attrs["time_finished"] = datetime.now().astimezone().isoformat()
    z.attrs["noop_threshold"] = noop_threshold
    z.attrs["chunksize"] = chunksize
