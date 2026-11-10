#!/bin/bash
cd ~/workspace/quant || exit 1
git pull -q

echo "=== 拉新数据源 ==="
NO_PROXY='*' HTTP_PROXY= HTTPS_PROXY= PYTHONPATH=. \
  .venv/bin/python -m datahub.fin.update --only csi_all_amount,csi_all_turnover,breadth_adv 2>&1 | tail -6

echo "=== 重启 ==="
systemctl --user restart quant
sleep 5
for p in /diagnose /cards /data /ops ; do
  printf '%s ' "$p"
  curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8000$p"
done
