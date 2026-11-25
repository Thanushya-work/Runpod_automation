#!/bin/bash
# Runs INSIDE the pod as its start command. Everything printed here is read by the watcher
# through the Runpod log API, so every line gets a UTC timestamp and the markers below matter:
#   SETUP_FAILED step=<name>   -> setup broke; pod is kept for inspection, you get an alert
#   PIPELINE_DONE exit=<code>  -> main.py ended; watcher deletes the pod after the grace period
# The container never exits by itself; the watcher decides when to delete the pod.

set -uo pipefail
mkdir -p /workspace
exec > >(while IFS= read -r line; do printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line"; done | tee -a /workspace/bootstrap.log) 2>&1

# Short diagnostics FIRST and the marker LAST, so the watcher's 15-line tail shows the actual error.
fail() {
  nvidia-smi --query-gpu=name,driver_version,memory.used,memory.total --format=csv,noheader || true
  df -h /workspace | tail -n 1 || true
  echo "SETUP_FAILED step=$1"
  sleep infinity
}
retry() { local n=0; until "$@"; do n=$((n+1)); [ "$n" -ge 3 ] && return 1; echo "retry $n: $*"; sleep 15; done; }

[ -n "${REPO_URL:-}" ] && [ -n "${REPO_BRANCH:-}" ] || fail repo-url-missing
export HF_HUB_ENABLE_HF_TRANSFER=0 CUDA_HOME=/usr/local/cuda HF_HOME=/workspace/hf_cache
export DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

echo "=== 1. checks ==="
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv || fail nvidia-smi
nvcc --version | tail -n 2 || fail nvcc
python --version

echo "=== 2. system packages ==="
retry apt-get update || fail apt-update
retry apt-get install -y git-lfs || fail apt-install
git lfs install

echo "=== 3. clone repo ==="
cd /workspace
if [ ! -d Grounding_Dino ]; then
  retry git clone -b "$REPO_BRANCH" --single-branch "$REPO_URL" Grounding_Dino || fail git-clone
fi
cd /workspace/Grounding_Dino
if [ -n "${REPO_REF:-}" ]; then git checkout "$REPO_REF" || fail git-checkout; fi   # optional: pin an exact commit
retry git lfs pull || fail git-lfs-pull
echo "repo at commit: $(git rev-parse --short HEAD)"

echo "=== 4. python dependencies ==="
retry pip install -r requirements.txt || fail pip-requirements
pip uninstall -y torchaudio || true

echo "=== 5. patch and build GroundingDINO ==="
cd /workspace/Grounding_Dino/GroundingDINO
sed -i 's/AT_DISPATCH_FLOATING_TYPES(value.type()/AT_DISPATCH_FLOATING_TYPES(value.scalar_type()/g' \
  groundingdino/models/GroundingDINO/csrc/MsDeformAttn/ms_deform_attn_cuda.cu
pip install -e . --no-build-isolation --no-deps || fail groundingdino-build
cd /workspace/Grounding_Dino
mkdir -p outputs

echo "=== 6. config.json from the cloned repo ==="
# Prints ONLY the database and bucket names. Never print any other config value.
python - <<'EOF' || fail config-missing
import json
try:
    with open("/workspace/Grounding_Dino/config.json") as f:
        cfg = json.load(f)
except Exception as e:
    raise SystemExit(f"config.json missing or not valid JSON: {type(e).__name__}")   # type only, never the content
for section, key in (("db_config", "database"), ("s3_config", "bucket_name")):
    part = cfg.get(section) if isinstance(cfg, dict) else None
    value = part.get(key) if isinstance(part, dict) else None
    print(f"{section}.{key} = {value if isinstance(value, str) else 'NOT FOUND'}")
EOF

echo "=== 7. final checks ==="
python -c "import torch; print('torch', torch.__version__, '| cuda', torch.version.cuda, '| gpu visible:', torch.cuda.is_available())" || fail torch-check
python -c "import torch; assert torch.cuda.is_available()" || fail gpu-not-visible
# torch must be imported first: _C links against libc10/libtorch, which only torch puts on the loader path.
python -c "import torch; from groundingdino import _C; print('GroundingDINO CUDA extension OK')" || {
  echo "--- GroundingDINO build artefacts ---"
  find /workspace/Grounding_Dino/GroundingDINO -name '_C*.so' 2>/dev/null || true
  fail groundingdino-import
}
df -h /workspace
du -sh /workspace
echo "SETUP_DONE"

if [ -n "${SETUP_ONLY:-}" ]; then
  echo "PIPELINE_DONE exit=0 (setup-only test, main.py not run)"
  sleep infinity
fi

echo "=== 8. pipeline ==="
python -u main.py "${WORKER_NAME:-pod-1}" ${BATCH_LIMIT:-}
rc=$?
du -sh /workspace
echo "PIPELINE_DONE exit=$rc"
df -h /workspace
sleep infinity
