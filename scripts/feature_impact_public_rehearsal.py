"""对四份冻结公开输入执行真实无模型业务 HTTP；凭据仅经过私有管道。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.feature_impact.knowledge import digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # 沿用既有隔离演练身份机制；仅新增一次启动身份/默认任务，不迁移或重置数据库。
    child = subprocess.Popen([sys.executable, '-u', str(ROOT / 'scripts/feature_impact_rehearsal_server.py')],
        cwd=ROOT, env={**os.environ, 'FEATURE_IMPACT_PORT': '0', 'PYTHONIOENCODING': 'utf-8'},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
    try:
        line = child.stdout.readline()
        if not line:
            raise RuntimeError('PUBLIC_BUSINESS_START_FAILED')
        live = json.loads(line)
        opener = build_opener(ProxyHandler({}))
        for code in ('T2', 'T3', 'T5', 'T6'):
            value = json.loads((ROOT / 'tests/fixtures/feature_impact_public' / f'{code}.json').read_text(encoding='utf-8'))
            body = dict(protocol='feature-impact-request.v1', input=value, project_id=str(uuid4()), run_id=str(uuid4()), attempt_id=str(uuid4()),
                        request_id=str(uuid4()), case_id=str(uuid4()), frozen_input_digest=digest(value))
            request = Request(live['base_url'] + '/api/feature-impact/quality/evaluate',
                data=json.dumps(body, ensure_ascii=False).encode('utf-8'), method='POST',
                headers={'Authorization': 'Bearer ' + live['service_token'], 'Content-Type': 'application/json'})
            with opener.open(request, timeout=30) as response:
                output = json.load(response)
                status = response.status
            # 仅保存公开请求/原始报告；不存在 token、隐藏参考或模拟执行结果。
            artifact = dict(task_type=code, transport='REAL_PYTHON_HTTP', http_status=status, request=body, output=output)
            (args.output / f'{code}.json').write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(f'{code}: HTTP {status}; RULE findings={len(output["report"]["findings"])}')
    finally:
        child.stdin.write('stop\n')
        child.stdin.flush()
        child.communicate(timeout=20)
    if child.returncode != 0:
        raise RuntimeError('PUBLIC_BUSINESS_SHUTDOWN_FAILED')


if __name__ == '__main__':
    main()
