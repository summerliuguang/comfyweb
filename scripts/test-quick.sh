#!/usr/bin/env bash
# 快速测试:日常改动后的默认验证,只跑核心链路的几条(约 3 秒)。
# 全量测试(用户要求时): python -m unittest discover -s tests
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-python3}"
if [ -x venv/bin/python ]; then PY=venv/bin/python; fi
exec "$PY" -m unittest discover -s tests \
    -k test_convert \
    -k test_build_and_seed \
    -k test_place_indexes_and_dedups \
    -k test_ingest_refresh \
    -k test_feature_toggle_from_settings \
    -k test_filter_by_category_and_batch \
    -v
