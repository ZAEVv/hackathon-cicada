#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Cap BLAS/OpenMP thread pools: on a loaded machine sklearn/numpy otherwise
# oversubscribe the CPUs and the suite can stall. Callers may override.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export VECLIB_MAXIMUM_THREADS="${VECLIB_MAXIMUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
if ! .venv/bin/python -c 'import pytest, httpx' 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements-dev.txt
fi
.venv/bin/python -m pytest -q
npm --prefix frontend run build
