"""All tunable settings in one place. No secrets in this file."""
import os

API = "https://api.runpod.io/v2"
NAME_PREFIX = "gdino-pipeline"          # every pod we create starts with this; we only ever touch these

# --- GPU choice (tried in this order) ---------------------------------------------
GPU_PREFERENCE = [
    "NVIDIA RTX A4500",
    "NVIDIA RTX A5000",
    "NVIDIA RTX 4000 Ada Generation",
]
MIN_CUDA_VERSION = "12.1"               # host driver must support at least this

# --- Pod shape ----------------------------------------------------------------------
# Must match requirements.txt of the pipeline (PyTorch 2.5.1 + CUDA 12.1, Python >= 3.10).
# "devel" image = has nvcc, needed to compile GroundingDINO.
IMAGE = os.environ.get("POD_IMAGE", "runpod/pytorch:2.5.1-py3.11-cuda12.1.1-devel-ubuntu22.04")
CONTAINER_DISK_GB = int(os.environ.get("CONTAINER_DISK_GB", "50"))
VOLUME_DISK_GB = int(os.environ.get("VOLUME_DISK_GB", "50"))
MOUNT_PATH = "/workspace"

# --- Launching ----------------------------------------------------------------------
LAUNCH_RETRY_SECONDS = 180              # wait between "is a GPU free?" attempts
LAUNCH_GIVE_UP_MINUTES = int(os.environ.get("LAUNCH_GIVE_UP_MINUTES", "180"))

# --- Watching (all in minutes unless noted) -----------------------------------------
STUCK_QUIET_MINUTES = 90                # no new log line for this long -> alert (never kills)
STUCK_REALERT_MINUTES = 120             # repeat the stuck alert at most this often
DONE_GRACE_MINUTES = 15                 # after a clean finish, wait this long, then delete the pod
FAILED_KEEP_MINUTES = 30                # after a failure, keep the pod this long for inspection
START_TIMEOUT_MINUTES = 30              # pod not RUNNING after this long -> alert
LONG_RUNNING_HOURS = 6                  # reminder only (never kills)
LONG_RUNNING_REALERT_MINUTES = 180
